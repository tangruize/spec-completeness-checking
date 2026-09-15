from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from specdet.adapters.verus.backend import VerusBackend
from specdet.api import analyze
from specdet.adapters.verus.witness_replay import replay_witness
from specdet.adapters.verus.witness_values import candidate_bindings
from specdet.config import Config, Limits, ToolchainConfig
from specdet.storage.workspace import prepare_project
from specdet.storage.artifacts import ArtifactStore

FIXTURES = Path(__file__).parent / "fixtures/incomplete"
CASES = json.loads((FIXTURES / "manifest.json").read_text())["cases"]
COMPATIBILITY = b"#![verifier::deprecated_postcondition_mut_ref_style(true)]\n"


class IncompleteSourceProvenanceTests(unittest.TestCase):
    def test_complete_source_copies_only_have_the_declared_compatibility_prefix(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                data = (FIXTURES / case["fixture"]).read_bytes()
                if case["transformation"] == "prepend_legacy_mut_ref_compatibility":
                    self.assertTrue(data.startswith(COMPATIBILITY))
                    data = data[len(COMPATIBILITY):]
                else:
                    self.assertEqual(case["transformation"], "none")
                self.assertEqual(hashlib.sha256(data).hexdigest(), case["source_sha256"])


@unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Set SPECDET_VERUS for real source witness checks")
class RealIncompleteSourceWitnessTests(unittest.TestCase):
    def test_complete_pipeline_reports_source_confirmed_nondeterminism(self):
        for case in CASES:
            with self.subTest(case=case["id"]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                selectors = (
                    f"{case['fixture']}:{case['function']}@18",
                ) if case.get("exclude_owner") else (case["function"],)
                config = Config(
                    FIXTURES, root / "out", include=(case["fixture"],),
                    selectors=selectors,
                    counterexample_candidates=16,
                    limits=Limits(verifier_timeout_seconds=30, solver_timeout_ms=1000, max_search_rounds=8),
                    toolchain=ToolchainConfig(
                        executable=os.environ["SPECDET_VERUS"],
                        rust_toolchain=os.environ.get("SPECDET_RUST_TOOLCHAIN", ""),
                    ),
                )
                summary, exit_code = analyze(config)
                self.assertEqual(exit_code, 1, summary)
                self.assertEqual(len(summary["results"]), 1)
                report = summary["results"][0]
                self.assertEqual(report["verdict"], "nondeterministic")
                evidence = report["counterexample"]
                self.assertEqual(evidence["status"], "verified")
                self.assertEqual(evidence["kind"], "source_verified_constructive")
                self.assertEqual(evidence["problem_id"], report["problem_id"])
                certificate_path = Path(evidence["artifact"])
                certificate = ArtifactStore(certificate_path.parent).read_artifact(
                    certificate_path.name, expected_kind="source_witness_replay",
                )
                self.assertEqual(certificate["bindings"], evidence["bindings"])
                self.assertEqual(certificate["type_arguments"], evidence["type_arguments"])
                self.assertEqual(certificate["status"], "verified")
                self.assertEqual(certificate["raw_solver_status"], "not_reported")
                self.assertIn(report["baseline"]["status"], {"sat", "unknown"})
                self.assertGreater(evidence["verified_goals"], 0)

    def test_real_source_candidates_are_generated_and_replayed_without_seeded_outputs(self):
        for case in CASES:
            with self.subTest(case=case["id"]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = Config(
                    FIXTURES, root / "out", include=(case["fixture"],),
                    limits=Limits(verifier_timeout_seconds=30, solver_timeout_ms=1000, max_search_rounds=8),
                    toolchain=ToolchainConfig(
                        executable=os.environ["SPECDET_VERUS"],
                        rust_toolchain=os.environ.get("SPECDET_RUST_TOOLCHAIN", ""),
                    ),
                )
                project = prepare_project(config, root / "run")
                backend = VerusBackend(config)
                targets = [
                    target for target in backend.discover(project)[0]
                    if target.name == case["function"]
                    and case.get("exclude_owner", "\0") not in target.qualified_name
                ]
                self.assertEqual(len(targets), 1, targets)
                contract = backend.extract(project, targets[0])
                obligation = backend.lower(project, contract, backend.observations(project, contract))
                checked = []
                for index, bindings in enumerate(candidate_bindings(
                    obligation, max_candidates=24, type_arguments=case["type_arguments"],
                )):
                    result = replay_witness(
                        backend, project, obligation, bindings, root / f"candidate-{index}",
                        type_arguments=case["type_arguments"],
                    )
                    checked.append(result)
                    if result["status"] == "verified":
                        break
                    self.assertIn(result["status"], {"unproved"}, result["verifier"]["stderr"])
                self.assertTrue(checked, "No source-typed candidates generated")
                self.assertEqual(
                    checked[-1]["status"], "verified",
                    [(item["bindings"], item["status"], item["verifier"]["stderr"]) for item in checked],
                )
                self.assertGreater(checked[-1]["verified_goals"], 0)
                self.assertEqual(checked[-1]["problem_id"], obligation.problem_id)
                self.assertEqual(checked[-1]["raw_solver_status"], "not_reported")


if __name__ == "__main__":
    unittest.main()
