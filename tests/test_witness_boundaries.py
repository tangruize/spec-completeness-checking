from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path

from specdet.adapters.verus.backend import VerusBackend
from specdet.adapters.verus.witness_replay import UnsupportedWitness, render_witness_replay, replay_witness
from specdet.adapters.verus.witness_values import candidate_bindings
from specdet.analysis.pipeline import AnalysisSession
from specdet.config import Config, ToolchainConfig
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
        backend = VerusBackend(Config(
            project_root, self.root / "out",
            toolchain=ToolchainConfig(executable=os.environ.get("SPECDET_VERUS", "")),
        ))
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

    def test_replay_renders_verus_math_integers_in_nested_source_types(self):
        _, _, _, obligation = self.lower("""
struct Position { value: int }
enum Status { Missing(Position), Present { index: int }, Empty }
pub fn f() -> (r: Status) ensures true, { Status::Empty }
""")
        candidates = list(candidate_bindings(obligation, max_candidates=8))
        rendered = [
            render_witness_replay(obligation, bindings)[1]
            for bindings in candidates
        ]
        self.assertTrue(any("Status::Missing(Position { value: 0int })" in code for code in rendered))
        self.assertTrue(any("Status::Present { index: 0int }" in code for code in rendered))

    def test_top_level_verus_math_integer_uses_typed_literal(self):
        _, _, _, obligation = self.lower(
            "pub fn f(x: int) -> (r: int) ensures true, { x }"
        )
        _, code, _ = render_witness_replay(
            obligation, {"x": 0, "r1": 1, "r2": -1},
        )
        self.assertIn("let ghost x: int = 0int;", code)
        self.assertIn("let ghost r1: int = 1int;", code)
        self.assertIn("let ghost r2: int = (-1int);", code)

    def test_mutable_poststates_are_separate_witness_bindings(self):
        _, _, _, obligation = self.lower(
            "pub fn f(x: &mut u8) requires *old(x) == 1, ensures *final(x) < 3, { *x = 0; }"
        )
        candidates = list(candidate_bindings(obligation, max_candidates=8))
        self.assertTrue(candidates)
        self.assertTrue(all(
            set(binding) == {"pre_x", "post1_x", "post2_x", "r1", "r2"}
            for binding in candidates
        ))
        self.assertTrue(any(
            binding["pre_x"] == 1 and binding["post1_x"] != binding["post2_x"]
            for binding in candidates
        ))

    def test_catalog_interleaves_inputs_and_output_pairs(self):
        _, _, _, obligation = self.lower(
            "pub fn f(x: u8, y: u8) -> (r: u8) ensures true, { x }"
        )
        candidates = list(candidate_bindings(obligation, max_candidates=8))
        self.assertGreater(len({(row["r1"], row["r2"]) for row in candidates}), 1)
        self.assertGreater(len({row["x"] for row in candidates}), 1)
        self.assertGreater(len({row["y"] for row in candidates}), 1)

    def test_invalid_enum_payloads_and_executable_generic_types_are_rejected(self):
        _, _, _, obligation = self.lower(
            "enum E { A, B } pub fn f() -> (r: E) ensures true, { E::A }"
        )
        for first in (
            {"kind": "variant", "name": "Some", "items": []},
            {"kind": "variant", "type": "E", "name": "A"},
            {"kind": "variant", "type": "E<{ panic!() }>", "name": "A", "items": []},
            {"kind": "variant_struct", "type": "E<{ panic!() }>", "name": "A", "fields": {}},
        ):
            with self.subTest(first=first), self.assertRaises(UnsupportedWitness):
                render_witness_replay(obligation, {
                    "r1": first,
                    "r2": {"kind": "variant", "type": "E", "name": "B", "items": []},
                })

    @unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Configure Verus for mode-sensitive replay")
    def test_real_custom_enum_witness_uses_ghost_mathematical_fields(self):
        backend, project, _, obligation = self.lower("""
struct Position { ghost value: int }
enum Status { Missing(Position), Present { index: u8 }, Empty }
pub fn f(Ghost(x): Ghost<int>) -> (r: Status) ensures true, { Status::Empty }
""")
        bindings = next(candidate_bindings(obligation, max_candidates=1))
        result = replay_witness(backend, project, obligation, bindings, self.root / "math-enum")
        self.assertEqual(result["status"], "verified", result["verifier"]["stderr"])
        self.assertEqual(result["verified_goals"], 1)

    @unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Configure Verus for state witness replay")
    def test_real_mutable_witness_rechecks_preconditions_and_both_poststates(self):
        backend, project, _, obligation = self.lower(
            "pub fn f(x: &mut u8) requires *old(x) == 1, ensures *final(x) < 3, { *x = 0; }"
        )
        bindings = {"pre_x": 1, "post1_x": 0, "post2_x": 1, "r1": None, "r2": None}
        for name, values, expected in (
            ("valid", bindings, "verified"),
            ("bad-pre", {**bindings, "pre_x": 0}, "unproved"),
            ("bad-post", {**bindings, "post2_x": 3}, "unproved"),
            ("same-state", {**bindings, "post2_x": 0}, "unproved"),
        ):
            with self.subTest(case=name):
                result = replay_witness(backend, project, obligation, values, self.root / name)
                self.assertEqual(result["status"], expected, result["verifier"]["stderr"])

    @unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Configure Verus for finite map/set replay")
    def test_real_finite_map_and_set_values_are_constructed_not_assumed(self):
        backend, project, _, obligation = self.lower("""
struct State { values: Map<u8, u8>, members: Set<u8> }
pub fn f(Ghost(x): Ghost<State>) -> (r: Ghost<State>) ensures true, { Ghost(x) }
""")
        candidates = list(candidate_bindings(obligation, max_candidates=8))
        self.assertTrue(candidates)
        for index, bindings in enumerate(candidates[:3]):
            with self.subTest(candidate=index):
                _, code, _ = render_witness_replay(obligation, bindings)
                self.assertNotIn("assume(", code)
                result = replay_witness(
                    backend, project, obligation, bindings, self.root / f"finite-{index}",
                )
                self.assertEqual(result["status"], "verified", result["verifier"]["stderr"])

    @unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Configure Verus for frame-aware replay")
    def test_unchanged_state_candidates_find_return_omissions_without_relaxing_the_frame(self):
        backend, project, _, obligation = self.lower("""
struct State { capacity: u8, first: u8, second: u8 }
fn f(state: &mut State, key: u8) -> (r: Option<u8>)
    requires old(state).capacity > 0,
    ensures *final(state) == *old(state),
{ None }
""")
        found = None
        for index, bindings in enumerate(candidate_bindings(obligation, max_candidates=16)):
            result = replay_witness(
                backend, project, obligation, bindings, self.root / f"frame-{index}",
            )
            self.assertIn(result["status"], {"verified", "unproved"}, result["verifier"]["stderr"])
            if result["status"] == "verified":
                found = bindings
                break
        self.assertIsNotNone(found, "Frame-preserving return alternatives were starved by invalid states")
        self.assertEqual(found["pre_state"], found["post1_state"])
        self.assertEqual(found["pre_state"], found["post2_state"])

    @unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Configure Verus for container-frame replay")
    def test_generated_facts_prove_container_invariants_and_original_equality_clauses(self):
        backend, project, _, obligation = self.lower("""
struct Store { capacity: u8, values: Ghost<Map<u8,u8>>, order: Ghost<Seq<u8>> }
impl Store {
    spec fn wf(&self) -> bool {
        self.capacity > 0 && self.values@.dom() == self.order@.to_set()
    }
}
spec fn without(order: Seq<u8>, key: u8) -> Seq<u8> {
    order.filter(|value: u8| value != key)
}
fn f(store: &mut Store, key: u8) -> (r: Option<u8>)
    requires old(store).wf(),
    ensures
        final(store).wf(),
        final(store).capacity == old(store).capacity,
        final(store).values@ == old(store).values@.remove(key),
        final(store).order@ == without(old(store).order@, key),
{
    store.values = Ghost(store.values@.remove(key));
    store.order = Ghost(without(store.order@, key));
    None
}
""")
        result = None
        for index, bindings in enumerate(candidate_bindings(obligation, max_candidates=16)):
            result = replay_witness(
                backend, project, obligation, bindings, self.root / f"container-frame-{index}",
            )
            self.assertIn(result["status"], {"verified", "unproved"}, result["verifier"]["stderr"])
            if result["status"] == "verified":
                break
        self.assertEqual(result["status"], "verified", result["verifier"]["stderr"])

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
