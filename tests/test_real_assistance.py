from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from specdet.adapters.verus.proof_hints import sequence_extensionality
from specdet.api import analyze
from specdet.assistance.workflow import AssistanceSettings, RequestBudget, run_assisted
from specdet.config import Config, Limits, ToolchainConfig
from specdet.domain.models import Stage
from specdet.domain.proposals import RawResponse

TOOL = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Set SPECDET_VERUS for real proof checking")
class RealProofAssistanceTests(unittest.TestCase):
    def test_source_specific_offline_candidate_closes_range_constructor(self):
        from specdet.adapters.verus.backend import VerusBackend
        from specdet.domain.proposals import GenerationRequest, Proposal
        from specdet.storage.workspace import PreparedProject
        from specdet.domain.models import digest, text_digest

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = TOOL / "tests/fixtures/verusage/atmosphere/va_range_new.rs"
            source = source_path.read_text()
            project_root = root / "source"
            project_root.mkdir()
            (project_root / source_path.name).write_text(source)
            files = {source_path.name: text_digest(source)}
            project = PreparedProject(project_root, project_root, digest(files), files)
            config = Config(
                project_root, root / "out", include=(source_path.name,),
                toolchain=ToolchainConfig(
                    executable=os.environ["SPECDET_VERUS"],
                    rust_toolchain=os.environ.get("SPECDET_RUST_TOOLCHAIN", ""),
                ),
                limits=Limits(verifier_timeout_seconds=30, solver_timeout_ms=1000, max_search_rounds=8),
            )
            backend = VerusBackend(config)
            target = next(t for t in backend.discover(project)[0] if t.name == "new")
            contract = backend.extract(project, target)
            observation = backend.observations(project, contract)
            obligation = backend.lower(project, contract, observation)
            request = GenerationRequest(
                Stage.PROOF_GENERATION, "verus", ("proof",),
                contract.source_digest, obligation.problem_id, {}, target_id=target.id,
            )
            proposal = Proposal(
                request.id, request.stage, request.language, "proof",
                request.source_digest, request.problem_id,
                {"proof": (TOOL / "tests/fixtures/proof_candidates/va_range_new.rs").read_text(), "helpers": ""},
            )
            validation = backend.validate_proposal(request, proposal, project, target, contract, obligation)
            self.assertEqual(validation.status, "accepted_for_check", validation.diagnostics)
            backend.adopt_proposal(proposal, project, target, contract, obligation)
            candidate = backend.generate_proof(project, contract, obligation, "accepted")
            checked = backend.check(project, obligation, candidate, root / "candidate")
            self.assertEqual(checked.status, "verified", checked.diagnostics)

    def test_offline_proof_candidate_is_checked_by_real_verus(self):
        """Exercise the LLM-response boundary without pretending to call a live model."""
        with tempfile.TemporaryDirectory() as directory:
            source = TOOL / "tests/fixtures/verusage/storage"
            config = Config(
                source, Path(directory) / "out",
                include=("region_sizes.rs",), selectors=("get_region_sizes",),
                toolchain=ToolchainConfig(
                    executable=os.environ["SPECDET_VERUS"],
                    rust_toolchain=os.environ.get("SPECDET_RUST_TOOLCHAIN", ""),
                ),
                limits=Limits(verifier_timeout_seconds=30, solver_timeout_ms=1000, max_search_rounds=8),
                proof_strategies=("baseline", "accepted", "assisted"),
                assistance={"mode": "replay"}, offline=True,
            )
            settings = AssistanceSettings(mode="replay", excluded_stages=("counterexample",))
            budget = RequestBudget(settings)
            calls = []

            def driver(session):
                class OfflineCandidate:
                    mode = "replay"

                    def generate(self, request):
                        calls.append(request)
                        self_outer.assertEqual(request.stage, Stage.PROOF_GENERATION)
                        proof = sequence_extensionality(session.obligation)
                        self_outer.assertIsNotNone(proof)
                        return RawResponse(request.id, json.dumps({
                            "schema_version": 1, "request_id": request.id,
                            "stage": request.stage.value, "language": request.language,
                            "kind": "proof", "base_source_digest": request.source_digest,
                            "base_problem_id": request.problem_id,
                            "payload": {"proof": proof, "helpers": ""},
                            "origin": "llm",
                        }), "offline-proof-fixture")

                self_outer = self
                run_assisted(session, OfflineCandidate(), settings, budget=budget)

            summary, exit_code = analyze(config, driver=driver)
            self.assertEqual(exit_code, 0, summary)
            report = summary["results"][0]
            self.assertEqual(report["verdict"], "deterministic")
            self.assertEqual(len(calls), 1)
            self.assertEqual(report["baseline"]["status"], "unknown")
            self.assertEqual([item["status"] for item in report["proofs"]], ["unproved", "verified"])
            self.assertNotEqual(report["proofs"][0]["candidate_id"], report["proofs"][1]["candidate_id"])


if __name__ == "__main__":
    unittest.main()
