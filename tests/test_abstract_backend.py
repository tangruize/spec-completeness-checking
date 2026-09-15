from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from specdet.adapters.verus.backend import VerusBackend
from specdet.api import analyze
from specdet.config import Config, Limits, ToolchainConfig
from specdet.domain.models import SolverStatus, Stage, StageError, digest, text_digest
from specdet.domain.proposals import GenerationRequest, Proposal
from specdet.storage.workspace import PreparedProject

TOOL = Path(__file__).resolve().parents[1]


class AbstractBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project = self.root / "project"
        project.mkdir()
        self.source = (TOOL / "examples/abstract/contracts.rs").read_text()
        (project / "contracts.rs").write_text(self.source)
        files = {"contracts.rs": text_digest(self.source)}
        self.project = PreparedProject(project, project, digest(files), files)
        self.config = Config(
            project, self.root / "out", analysis_kind="abstract_determinism",
            limits=Limits(solver_timeout_ms=1000, max_search_rounds=8),
            toolchain=ToolchainConfig(
                executable=os.environ.get("SPECDET_VERUS", ""),
                rust_toolchain=os.environ.get("SPECDET_RUST_TOOLCHAIN", ""),
            ),
        )
        self.backend = VerusBackend(self.config)

    def contract(self, name):
        target = next(item for item in self.backend.discover(self.project)[0] if item.name == name)
        return self.backend.extract(self.project, target)

    def lower(self, name):
        contract = self.contract(name)
        observations = self.backend.observations(self.project, contract)
        return contract, observations, self.backend.lower(self.project, contract, observations)

    def test_same_output_relation_but_different_input_problem(self):
        contract, abstract, abstract_goal = self.lower("value")
        concrete = self.backend.observations(self.project, contract, analysis_kind="concrete_determinism")
        concrete_goal = self.backend.lower(self.project, contract, concrete)
        self.assertEqual(abstract.native["comparisons"], concrete.native["comparisons"])
        self.assertEqual(abstract.analysis_kind, "abstract_determinism")
        self.assertNotEqual(abstract_goal.problem_id, concrete_goal.problem_id)
        pairs = abstract.native["input_relation"]["pairs"]
        self.assertEqual(len(pairs), 1)
        self.assertIn(".view()", pairs[0]["relation"])
        self.assertIn(pairs[0]["left"], abstract_goal.native["template"])
        self.assertIn(pairs[0]["right"], abstract_goal.native["template"])
        self.assertIn("not_checked", abstract.native["coverage"]["domain_preservation"])

    def test_multiple_view_inputs_share_only_unselected_arguments(self):
        _, observations, obligation = self.lower("select")
        pairs = {entry["parameter"]: entry for entry in observations.native["input_relation"]["pairs"]}
        self.assertEqual(set(pairs), {"first", "second"})
        template = obligation.native["template"]
        for run in ("left", "right"):
            self.assertIn(f"{pairs['first'][run]}@ <= {pairs['second'][run]}@", template)
        self.assertEqual(template.count("take_first: bool"), 1)
        self.assertEqual(len(observations.inputs), 3)

    def test_mutable_inputs_keep_independent_pre_and_post_states(self):
        _, observations, obligation = self.lower("set_value")
        pair = observations.native["input_relation"]["pairs"][0]
        template = obligation.native["template"]
        self.assertIn(pair["left"], template)
        self.assertIn(pair["right"], template)
        self.assertNotRegex(template, r"\bpre_self_\b")
        self.assertIn("post1_self_", template)
        self.assertIn("post2_self_", template)
        self.assertEqual(observations.inputs[0]["state"], "pre")

    def test_source_defined_input_selection_can_be_proposed_but_not_silently_weakened(self):
        contract, _, obligation = self.lower("select")
        request = GenerationRequest(
            Stage.OBSERVATIONS, "verus", ("observation",), contract.source_digest,
            obligation.problem_id, {}, target_id=contract.target.id,
        )
        same = Proposal(
            request.id, request.stage, request.language, "observation",
            request.source_digest, request.problem_id, {"abstract_inputs": ["first", "second"]},
        )
        changed = replace(same, payload={"abstract_inputs": ["first"]})
        self.assertEqual(
            self.backend.validate_proposal(request, same, self.project, contract.target, contract, obligation).status,
            "accepted",
        )
        self.assertEqual(
            self.backend.validate_proposal(request, changed, self.project, contract.target, contract, obligation).status,
            "needs_approval",
        )

    def test_proof_prompt_contains_frozen_input_views_and_their_source_bodies(self):
        contract, _, obligation = self.lower("value")
        context = self.backend.generation_context(
            Stage.PROOF_GENERATION, self.project, contract.target, contract, obligation, (),
        )
        self.assertEqual(context["analysis_kind"], "abstract_determinism")
        self.assertTrue(context["input_relation"]["pairs"])
        self.assertTrue(context["input_relation"]["definitions"])
        self.assertIn("self.observed", context["input_relation"]["definitions"][0]["body"])

    @unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Set SPECDET_VERUS for actual proof checking")
    def test_real_default_workflow_checks_concrete_before_abstract(self):
        config = replace(self.config, selectors=("value",))
        summary, exit_code = analyze(config)
        self.assertEqual(exit_code, 0, summary)
        report = summary["results"][0]
        self.assertEqual(report["analysis_kind"], "abstract_determinism")
        self.assertEqual(report["verdict"], "deterministic")
        self.assertEqual(report["concrete_result"]["verdict"], "deterministic")
        self.assertNotEqual(report["problem_id"], report["concrete_result"]["problem_id"])

    @unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Set SPECDET_VERUS for actual proof checking")
    def test_real_input_view_determinism_and_representation_leak(self):
        for name in ("value", "set_value", "select", "cached_value"):
            with self.subTest(name=name):
                contract, _, obligation = self.lower(name)
                candidate = self.backend.generate_proof(self.project, contract, obligation, "baseline")
                checked = self.backend.check(
                    self.project, obligation, candidate, self.root / f"check-{name}", baseline=True,
                )
                if name == "cached_value":
                    self.assertEqual(checked.status, "unproved", checked.diagnostics)
                    self.assertIn(self.backend.query(obligation, checked).status, {SolverStatus.SAT, SolverStatus.UNKNOWN})
                else:
                    self.assertEqual(checked.status, "verified", checked.diagnostics)
                    self.assertEqual(self.backend.query(obligation, checked).status, SolverStatus.UNSAT)
        self.assertEqual(self.project.source_path("contracts.rs").read_text(), self.source)


if __name__ == "__main__":
    unittest.main()
