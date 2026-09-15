from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from specdet.analysis.pipeline import AnalysisSession, run_mechanical
from specdet.assistance.workflow import AssistanceSettings, run_assisted
from specdet.config import Config
from specdet.domain.models import Diagnostic, SolverStatus, Stage, StageError, TargetRef, Verdict
from specdet.storage.workspace import PreparedProject
from test_assisted_pipeline import ProofFixtureProvider
from test_pipeline import FakeBackend


class ModeBackend(FakeBackend):
    def __init__(self, statuses):
        super().__init__()
        self.statuses = statuses
        self.kinds = {}
        self.observed = []
        self.no_views = False

    def observations(self, project, contract, *, analysis_kind=None):
        self.observed.append(analysis_kind)
        if analysis_kind == "abstract_determinism" and self.no_views:
            raise StageError(Diagnostic(Stage.OBSERVATIONS, "no_abstract_inputs", "Only scalar inputs"))
        return super().observations(project, contract, analysis_kind=analysis_kind)

    def lower(self, project, contract, observations):
        obligation = super().lower(project, contract, observations)
        self.kinds[obligation.problem_id] = observations.analysis_kind
        return obligation

    def query(self, obligation, baseline):
        self.status = self.statuses[self.kinds[obligation.problem_id]]
        return super().query(obligation, baseline)


class AbstractPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "source"
        source.mkdir()
        self.project = PreparedProject(source, source, "snapshot", {})
        self.config = Config(
            source, self.root / "out", language="test", analysis_kind="abstract_determinism",
        )
        self.target = TargetRef("test", "source.formal", "f", 1, source_digest="source")

    def session(self, backend, **changes):
        return AnalysisSession(
            backend, replace(self.config, **changes), self.project, self.target,
            self.root / "run", "run",
        )

    def test_concrete_then_abstract_keep_two_distinct_evidence_sets(self):
        backend = ModeBackend({
            "concrete_determinism": SolverStatus.UNSAT,
            "abstract_determinism": SolverStatus.UNSAT,
        })
        session = self.session(backend)
        run_mechanical(session)
        self.assertEqual(backend.observed, ["concrete_determinism", "abstract_determinism"])
        self.assertEqual(session.report.analysis_kind, "abstract_determinism")
        self.assertEqual(session.report.verdict, Verdict.DETERMINISTIC)
        self.assertEqual(session.report.concrete_result["verdict"], "deterministic")
        self.assertNotEqual(session.report.problem_id, session.report.concrete_result["problem_id"])
        self.assertEqual(len(session.report.proofs), 1)
        self.assertEqual(len(session.report.concrete_result["proofs"]), 1)

    def test_unproved_concrete_result_prevents_abstract_claim(self):
        backend = ModeBackend({"concrete_determinism": SolverStatus.UNKNOWN})
        session = self.session(backend)
        run_mechanical(session)
        self.assertEqual(backend.observed, ["concrete_determinism"])
        self.assertEqual(session.report.status, "skipped")
        self.assertEqual(session.report.verdict, Verdict.NOT_EVALUATED)
        self.assertEqual(session.report.concrete_result["verdict"], "inconclusive")

    def test_direct_abstract_mode_is_explicit(self):
        backend = ModeBackend({"abstract_determinism": SolverStatus.UNSAT})
        session = self.session(backend, abstract_require_concrete=False)
        run_mechanical(session)
        self.assertEqual(backend.observed, ["abstract_determinism"])
        self.assertIsNone(session.report.concrete_result)

    def test_no_view_is_not_an_abstract_proof(self):
        backend = ModeBackend({"concrete_determinism": SolverStatus.UNSAT})
        backend.no_views = True
        session = self.session(backend)
        run_mechanical(session)
        self.assertEqual(session.report.status, "not_applicable")
        self.assertEqual(session.report.verdict, Verdict.NOT_EVALUATED)
        self.assertEqual(session.report.concrete_result["verdict"], "deterministic")

    def test_assistance_is_bound_to_each_problem_not_reused_as_a_proof(self):
        backend = ModeBackend({
            "concrete_determinism": SolverStatus.UNKNOWN,
            "abstract_determinism": SolverStatus.UNKNOWN,
        })
        session = self.session(backend)
        provider = ProofFixtureProvider()
        telemetry = run_assisted(session, provider, AssistanceSettings(mode="replay"))
        self.assertEqual(telemetry.adopted, 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertNotEqual(provider.requests[0].problem_id, provider.requests[1].problem_id)
        self.assertEqual(provider.requests[0].target_id, provider.requests[1].target_id)
        self.assertEqual(session.report.verdict, Verdict.DETERMINISTIC)
        self.assertEqual(session.report.baseline.status, SolverStatus.UNKNOWN)
        self.assertEqual(session.report.concrete_result["baseline"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()

