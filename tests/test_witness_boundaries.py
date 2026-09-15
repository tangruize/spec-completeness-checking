from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from specdet.adapters.verus.backend import VerusBackend
from specdet.adapters.verus.witness_replay import UnsupportedWitness, render_witness_replay
from specdet.analysis.pipeline import AnalysisSession
from specdet.config import Config
from specdet.domain.models import (
    CounterexampleSearchResult, Diagnostic, Stage, TargetRef, digest, text_digest,
)
from specdet.domain.proposals import GenerationRequest, Proposal
from specdet.storage.workspace import PreparedProject
from test_pipeline import FakeBackend


class WitnessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def lower(self, declaration):
        source = f"use vstd::prelude::*;\nverus! {{ {declaration} }}\nfn main() {{}}\n"
        project_root = self.root / "project"
        project_root.mkdir()
        (project_root / "source.rs").write_text(source)
        files = {"source.rs": text_digest(source)}
        project = PreparedProject(project_root, project_root, digest(files), files)
        backend = VerusBackend(Config(project_root, self.root / "out"))
        target = next(item for item in backend.discover(project)[0] if item.name == "f")
        contract = backend.extract(project, target)
        obligation = backend.lower(project, contract, backend.observations(project, contract))
        return backend, project, contract, obligation

    def test_constructor_text_cannot_inject_assumptions_or_other_items(self):
        backend, project, contract, obligation = self.lower(
            "struct Unit; pub fn f() -> (r: Unit) ensures true, { Unit }"
        )
        for constructor in (
            "Unit; proof { assume(false); } Unit",
            "Unit; fn injected() {} type Other = Unit",
            "Unit; const TRUST: bool = true; type Other = Unit",
        ):
            with self.subTest(constructor=constructor):
                bindings = {
                    "r1": {"kind": "struct", "type": constructor, "fields": {}},
                    "r2": {"kind": "struct", "type": "Unit", "fields": {}},
                }
                with self.assertRaises(UnsupportedWitness):
                    render_witness_replay(obligation, bindings)
                request = GenerationRequest(
                    Stage.COUNTEREXAMPLE, "verus", ("counterexample",),
                    contract.source_digest, obligation.problem_id, {}, target_id=contract.target.id,
                )
                proposal = Proposal(
                    request.id, request.stage, request.language, "counterexample",
                    request.source_digest, request.problem_id, {"bindings": bindings},
                )
                validation = backend.validate_proposal(
                    request, proposal, project, contract.target, contract, obligation,
                )
                self.assertEqual(validation.status, "rejected")

    def test_generic_replay_rejects_unrebased_super_paths_in_contracts(self):
        _, _, _, obligation = self.lower("""
pub const LIMIT: u8 = 1;
mod inner {
    pub const LIMIT: u8 = 2;
    pub fn f<T>(value: T) -> (r: u8)
        ensures r < super::LIMIT,
    { 0 }
}""")
        with self.assertRaisesRegex(UnsupportedWitness, "source-namespace"):
            render_witness_replay(
                obligation, {"value": False, "r1": 0, "r2": 1},
                type_arguments={"T": "bool"},
            )

    def test_generic_replay_rejects_unrebased_paths_in_original_bounds(self):
        _, _, _, obligation = self.lower("""
pub trait Gate {}
mod inner {
    pub trait Gate {}
    pub fn f<T: super::Gate>(value: T) -> (r: u8)
        ensures true,
    { 0 }
}""")
        with self.assertRaisesRegex(UnsupportedWitness, "source-namespace"):
            render_witness_replay(
                obligation, {"value": False, "r1": 0, "r2": 1},
                type_arguments={"T": "bool"},
            )

    def test_unsupported_constructor_catalog_requests_assistance_without_refunding_budget(self):
        class LimitedBackend(FakeBackend):
            def find_counterexample(self, project, obligation, artifact_dir, *, max_candidates):
                return CounterexampleSearchResult(attempts=2, diagnostics=(
                    Diagnostic(Stage.COUNTEREXAMPLE, "unsupported_witness", "Needs a candidate", "warning"),
                ))

        project_root = self.root / "project"
        project_root.mkdir()
        project = PreparedProject(project_root, project_root, "snapshot", {})
        config = Config(project_root, self.root / "out", language="test", counterexample_candidates=4)
        target = TargetRef("test", "f.formal", "f", 1, source_digest="source")
        session = AnalysisSession(LimitedBackend(), config, project, target, self.root / "run", "run")
        for _ in range(12):
            outcome = session.advance()
            if outcome.status == "needs_assistance":
                break
        self.assertEqual(outcome.stage, Stage.COUNTEREXAMPLE)
        self.assertEqual(outcome.status, "needs_assistance")
        self.assertEqual(session._witness_attempts, 2)
        request = session.assistance_request(outcome)
        self.assertIsNotNone(request)
        self.assertEqual(request.allowed_kinds, ("counterexample",))
        session.decline_assistance(outcome, "test decline")
        self.assertEqual(session.stage, Stage.PROOF_GENERATION)
        self.assertEqual(session._witness_attempts, 2)


if __name__ == "__main__":
    unittest.main()

