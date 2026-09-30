from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from specdet.analysis.pipeline import AnalysisSession, ProjectSession, run_mechanical, select_targets
from specdet.api import analyze
from specdet.config import Config, Limits
from specdet.domain.models import (
    CheckEvidence, Contract, Diagnostic, Obligation, ObservationPlan, ProofCandidate,
    ProofCheckEvidence, SearchResult, SolverStatus, Stage, StageError, TargetRef, Verdict,
    CounterexampleSearchResult,
    digest, text_digest,
)
from specdet.domain.proposals import GenerationRequest, Proposal, ValidationRecord
from specdet.storage.artifacts import ArtifactStore
from specdet.storage.workspace import PreparedProject


class FakeBackend:
    def __init__(self, status=SolverStatus.UNKNOWN):
        self.status = status
        self.checked = []
        self.accepted = None
        self.query_calls = 0
        self.search_budgets = []
        self.fail_extract = False

    def toolchain_identity(self):
        return {"language": "test"}

    def discover(self, project):
        path = project.source_path("input.formal")
        return [TargetRef("test", "input.formal", "f", 1, source_digest=text_digest(path.read_text()))], []

    def extract(self, project, target):
        if self.fail_extract:
            raise StageError(Diagnostic(Stage.EXTRACT, "no_contract", "No contract"))
        return Contract(target, target.source_digest, {"input": "p", "post": "q"})

    def observations(self, project, contract, *, analysis_kind=None):
        return ObservationPlan("test-policy", {"equal": "value"}, analysis_kind=analysis_kind or "concrete_determinism")

    def lower(self, project, contract, observations):
        return Obligation(digest([contract, observations]), contract.target, {"goal": "p && q ==> equal"})

    def generate_proof(self, project, contract, obligation, strategy, feedback=()):
        if strategy == "baseline":
            return ProofCandidate(obligation.problem_id, obligation.id)
        if (
            strategy == "accepted" and self.accepted is not None
            and self.accepted.base_problem_id == obligation.problem_id
        ):
            return ProofCandidate(
                obligation.problem_id, obligation.id, proof="checked hint",
                origin="llm", proposal_id=self.accepted.id,
            )
        return None

    def check(self, project, obligation, candidate, attempt_dir, *, baseline=False):
        self.checked.append(candidate)
        return ProofCheckEvidence(
            obligation.problem_id, candidate.id,
            "unproved" if baseline else "verified", 0 if baseline else 1,
        )

    def query(self, obligation, baseline):
        self.query_calls += 1
        return CheckEvidence(obligation.problem_id, self.status, "baseline")

    def search(self, obligation, baseline, artifact_dir, *, max_rounds=None):
        self.search_budgets.append(max_rounds)
        evidence = CheckEvidence(
            obligation.problem_id, self.status, "refinement", constraints=({"value": 1},),
        )

        return SearchResult(
            evidence=(evidence,), rounds=1,
            confirmed_constraints=({"value": 1},) if self.status == SolverStatus.SAT else (),
            candidate_constraints=({"value": 1},) if self.status == SolverStatus.UNKNOWN else (),
        )

    def find_counterexample(self, project, obligation, artifact_dir, *, max_candidates):
        return CounterexampleSearchResult(attempts=max_candidates, exhausted=True)

    def generation_context(self, stage, project, target, contract, obligation, diagnostics):
        return {"source": "formal source"}

    def validate_proposal(self, request, proposal, project, target, contract, obligation):
        return ValidationRecord(proposal.id, "accepted_for_check")

    def adopt_proposal(self, proposal, project, target, contract, obligation):
        self.accepted = proposal
        return Stage.PROOF_GENERATION


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        source = root / "source"
        source.mkdir()
        (source / "input.formal").write_text("contract f")
        self.config = Config(project_root=source, output_dir=root / "out", language="test")
        self.project = PreparedProject(source, source, "snapshot", {"input.formal": "input-digest"})
        self.target = TargetRef("test", "input.formal", "f", 1, source_digest="source-digest")
        self.run_dir = root / "out" / "target"

    def tearDown(self):
        self.temp.cleanup()

    def session(self, backend):
        return AnalysisSession(backend, self.config, self.project, self.target, self.run_dir, "run")

    def test_fake_non_verus_backend_proves_pipeline_is_language_neutral(self):
        backend = FakeBackend(SolverStatus.UNSAT)
        session = self.session(backend)
        run_mechanical(session)
        self.assertEqual(session.report.verdict, Verdict.DETERMINISTIC)
        self.assertEqual(session.report.baseline.status, SolverStatus.UNSAT)
        self.assertEqual(len(backend.checked), 1)
        self.assertTrue((self.run_dir / "report.json").is_file())
        self.assertEqual(session.report.coverage["observation_policy"], "test-policy")
        self.assertEqual(session.report.resources["proof_attempts"]["used"], 1)
        self.assertEqual(session.report.resources["counterexample_candidates"]["used"], 0)
        self.assertIsNone(session.report.resources["wall_time_limit_seconds"])
        self.assertGreaterEqual(session.report.duration_ms, 0)

    def test_public_api_accepts_a_non_verus_backend(self):
        before = self.project.source_path("input.formal").read_bytes()
        summary, code = analyze(self.config, backend=FakeBackend(SolverStatus.UNSAT))
        self.assertEqual(code, 0)
        self.assertEqual(summary["counts"], {"deterministic": 1})
        self.assertGreaterEqual(summary["duration_ms"], summary["results"][0]["duration_ms"])
        self.assertEqual(self.project.source_path("input.formal").read_bytes(), before)
        store = ArtifactStore(Path(summary["run_dir"]))
        self.assertEqual(store.read_artifact("summary.json")["duration_ms"], summary["duration_ms"])
        self.assertEqual(store.read_artifact("manifest.json")["duration_ms"], summary["duration_ms"])
        target_report = summary["results"][0]
        self.assertTrue((Path(target_report["artifact_dir"]) / "report.json").is_file())

    def test_unsupported_target_still_records_whole_run_and_target_elapsed_time(self):
        backend = FakeBackend()
        backend.fail_extract = True
        summary, code = analyze(self.config, backend=backend)
        self.assertEqual(code, 2)
        self.assertEqual(summary["results"][0]["status"], "unsupported")
        self.assertGreaterEqual(summary["duration_ms"], summary["results"][0]["duration_ms"])
        self.assertGreater(summary["duration_ms"], 0)

    def test_interruption_and_unexpected_failure_retain_elapsed_time_and_completed_targets(self):
        class TwoTargetsBackend(FakeBackend):
            def discover(self, project):
                targets, diagnostics = super().discover(project)
                return [*targets, replace(targets[0], name="g", line=2)], diagnostics

        for error, status in ((KeyboardInterrupt(), "interrupted"), (RuntimeError("failure"), "failed")):
            with self.subTest(status=status):
                before = set(self.config.output_dir.glob("*"))

                def drive(session):
                    if isinstance(session, AnalysisSession) and session.target.name == "g":
                        raise error
                    run_mechanical(session)

                with self.assertRaises(type(error)):
                    analyze(self.config, backend=TwoTargetsBackend(SolverStatus.UNSAT), driver=drive)
                run_dir, = set(self.config.output_dir.glob("*")) - before
                store = ArtifactStore(run_dir)
                manifest = store.read_artifact("manifest.json")
                partial = store.read_artifact("partial-summary.json")
                self.assertEqual(manifest["status"], status)
                self.assertEqual(manifest["completed_targets"], 1)
                self.assertGreaterEqual(manifest["duration_ms"], partial["duration_ms"])
                self.assertGreater(partial["duration_ms"], 0)
                self.assertEqual(partial["results"][0]["target"]["name"], "f")
                self.assertFalse((run_dir / "summary.json").exists())

    def test_unknown_does_not_become_witness(self):
        session = self.session(FakeBackend())
        run_mechanical(session)
        self.assertEqual(session.report.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(session.report.search.confirmed_constraints, ())
        self.assertTrue(session.report.search.candidate_constraints)

    def test_sat_and_confirmed_constraints_are_separate(self):
        session = self.session(FakeBackend(SolverStatus.SAT))
        run_mechanical(session)
        self.assertEqual(session.report.verdict, Verdict.NONDETERMINISTIC)
        self.assertTrue(session.report.search.confirmed_constraints)

    def test_proof_generation_fallback_resumes_checking_and_preserves_r0(self):
        backend = FakeBackend()
        session = self.session(backend)
        for _ in range(20):
            outcome = session.advance()
            if outcome.status == "needs_assistance":
                break
        self.assertEqual(outcome.stage, Stage.PROOF_GENERATION)
        self.assertIsNotNone(session.obligation)
        request = GenerationRequest(
            Stage.PROOF_GENERATION, "test", ("proof",),
            self.target.source_digest, session.obligation.problem_id, {"goal": "frozen"},
        )
        proposal = Proposal(
            request.id, request.stage, request.language, "proof",
            request.source_digest, request.problem_id, {"proof": "hint", "helpers": ""},
        )
        validation = session.validate_proposal(request, proposal)
        session.adopt_proposal(request, proposal, validation)
        self.assertEqual(session.report.verdict, Verdict.NOT_EVALUATED)
        run_mechanical(session)
        self.assertEqual(len(backend.checked), 2)
        self.assertEqual(backend.query_calls, 1)
        self.assertEqual(session.report.baseline.status, SolverStatus.UNKNOWN)
        self.assertEqual(session.report.verdict, Verdict.DETERMINISTIC)

    def test_missing_contract_does_not_look_complete(self):
        backend = FakeBackend()
        backend.fail_extract = True
        session = self.session(backend)
        run_mechanical(session)
        self.assertEqual(session.report.status, "unsupported")
        self.assertEqual(session.report.verdict, Verdict.NOT_EVALUATED)
        self.assertEqual(backend.checked, [])

    def test_ambiguous_function_requires_source_line(self):
        targets = [
            self.target, TargetRef("test", "input.formal", "f", 8),
        ]
        with self.assertRaises(StageError):
            select_targets(targets, ("f",))
        self.assertEqual(select_targets(targets, ("input.formal:f@8",)), [targets[1]])

    def test_discovery_does_not_require_verifier(self):
        backend = FakeBackend()
        backend.toolchain_identity = lambda: self.fail("Discovery must not invoke verifier")
        session = ProjectSession(backend, self.config, self.run_dir, require_toolchain=False)
        run_mechanical(session)
        self.assertFalse(session.failed)
        self.assertEqual(len(session.targets), 1)

    def test_partial_discovery_can_request_help_without_losing_known_targets(self):
        class PartialBackend(FakeBackend):
            def discover(self, project):
                targets, _ = super().discover(project)
                return targets, [Diagnostic(
                    Stage.DISCOVER, "partial_discovery", "Unknown macro remains", "warning",
                )]

        project = ProjectSession(PartialBackend(), self.config, self.run_dir, require_toolchain=False)
        project.advance()
        project.advance()
        outcome = project.advance()
        self.assertEqual(outcome.status, "needs_assistance")
        project.decline_assistance(outcome, "assistance_off")
        self.assertTrue(project.finished)
        self.assertFalse(project.failed)
        self.assertEqual(len(project.targets), 1)

    def test_stale_proposal_cannot_be_adopted(self):
        backend = FakeBackend()
        session = self.session(backend)
        request = GenerationRequest(Stage.EXTRACT, "test", ("extraction",), "current", "", {})
        proposal = Proposal(
            request.id, Stage.EXTRACT, "test", "extraction", "stale", "", {},
        )
        validation = session.validate_proposal(request, proposal)
        self.assertEqual(validation.status, "rejected")
        with self.assertRaises(ValueError):
            session.adopt_proposal(request, proposal, validation)

    def test_normalization_reidentifies_target_without_resetting_its_budget_identity(self):
        original = self.target

        class NormalizingBackend(FakeBackend):
            def extract(self, project, target):
                if self.accepted is None:
                    raise StageError(Diagnostic(
                        Stage.EXTRACT, "unsupported_syntax", "Need normalization",
                    ), recoverable=True)
                return super().extract(project, target)

            def discover(self, project):
                return [TargetRef(
                    original.language, original.file, original.name, 6,
                    source_digest="normalized-source",
                )], []

            def adopt_proposal(self, proposal, project, target, contract, obligation):
                self.accepted = proposal
                return Stage.DISCOVER

        session = self.session(NormalizingBackend(SolverStatus.UNSAT))
        outcome = session.advance()
        self.assertEqual(outcome.status, "needs_assistance")
        request = GenerationRequest(
            Stage.EXTRACT, "test", ("normalization",), self.target.source_digest, "", {},
        )
        proposal = Proposal(
            request.id, request.stage, request.language, "normalization",
            request.source_digest, "", {"edits": []},
        )
        session.adopt_proposal(request, proposal, session.validate_proposal(request, proposal))
        run_mechanical(session)
        self.assertEqual(session.report.verdict, Verdict.DETERMINISTIC)
        self.assertEqual(session.target.line, 6)
        self.assertEqual(session._budget_target_id, original.id)
        self.assertNotEqual(session.target.id, original.id)


if __name__ == "__main__":
    unittest.main()
