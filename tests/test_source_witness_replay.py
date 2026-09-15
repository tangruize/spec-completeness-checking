from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from examples.verusage.run import case_config, load_manifest
from specdet.adapters.verus.backend import VerusBackend
from specdet.adapters.verus.witness_replay import (
    UnsupportedWitness, render_witness_replay, replay_witness,
)
from specdet.adapters.verus.witness_values import candidate_bindings
from specdet.analysis.pipeline import select_targets
from specdet.config import ToolchainConfig
from specdet.storage.workspace import prepare_project


class SourceWitnessReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        case = next(case for case in load_manifest()["cases"] if case["id"] == "vest.init-vec")
        config = case_config(case, "concrete", self.root, ToolchainConfig(
            executable=os.environ.get("SPECDET_VERUS", ""),
            rust_toolchain=os.environ.get("SPECDET_RUST_TOOLCHAIN", ""),
        ))
        self.project = prepare_project(config, self.root)
        self.backend = VerusBackend(config)
        self.target = select_targets(self.backend.discover(self.project)[0], config.selectors)[0]
        contract = self.backend.extract(self.project, self.target)
        self.obligation = self.backend.lower(
            self.project, contract, self.backend.observations(self.project, contract),
        )
        self.bindings = {
            "n": 1,
            "r1": {"kind": "vec", "items": [0]},
            "r2": {"kind": "vec", "items": [1]},
        }

    def test_replay_copies_the_original_conditions_not_the_implementation_result(self):
        _, code, goal = render_witness_replay(self.obligation, self.bindings)
        self.assertIn("r1@.len()", goal.posts)
        self.assertIn("r2@.len()", goal.posts)
        self.assertIn("!(", goal.distinctness)
        self.assertIn("assert(" + goal.posts + ")", code)
        self.assertIn("assert(" + goal.distinctness + ")", code)
        self.assertNotIn("assume(", code)
        with self.assertRaises(UnsupportedWitness):
            render_witness_replay(self.obligation, {"n": 1})

    def test_constructor_search_does_not_take_expected_witness_values_as_input(self):
        candidates = list(candidate_bindings(self.obligation, max_candidates=4))
        self.assertEqual(len(candidates), 4)
        self.assertIn(self.bindings, candidates)

    @unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Configure a real verifier for source replay")
    def test_real_verifier_accepts_only_a_valid_distinct_output_pair(self):
        for name, bindings, expected in (
            ("valid", self.bindings, "verified"),
            ("equal", {**self.bindings, "r2": {"kind": "vec", "items": [0]}}, "unproved"),
            ("wrong_length", {**self.bindings, "r2": {"kind": "vec", "items": []}}, "unproved"),
        ):
            with self.subTest(case=name):
                result = replay_witness(
                    self.backend, self.project, self.obligation, bindings, self.root / name,
                )
                self.assertEqual(result["status"], expected, result["verifier"]["stderr"])
                self.assertEqual(result["problem_id"], self.obligation.problem_id)
                self.assertEqual(result["raw_solver_status"], "not_reported")


if __name__ == "__main__":
    unittest.main()
