from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from specdet.adapters.verus.backend import VerusBackend
from specdet.config import BuildConfig, Config, Limits, ToolchainConfig
from specdet.domain.models import ProofCandidate, SolverStatus, Stage, StageError, digest, text_digest
from specdet.domain.proposals import GenerationRequest, Proposal
from specdet.storage.workspace import PreparedProject


@unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Set SPECDET_VERUS to an explicitly configured verifier")
class VerusBackendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / f"backend-integration-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.toolchain = ToolchainConfig(
            executable=os.environ["SPECDET_VERUS"],
            rust_toolchain=os.environ.get("SPECDET_RUST_TOOLCHAIN", ""),
        )

    def analyze_source(self, declaration, *, name="project"):
        root = self.root / name
        root.mkdir()
        source = "use vstd::prelude::*;\nverus! {\n" + declaration + "\n}\nfn main() {}\n"
        (root / "functions.rs").write_text(source)
        files = {"functions.rs": text_digest(source)}
        project = PreparedProject(root, root, digest(files), files)
        backend = VerusBackend(Config(
            root, self.root / "artifacts", toolchain=self.toolchain,
            limits=Limits(solver_timeout_ms=1000, max_search_rounds=8),
        ))
        target = next(target for target in backend.discover(project)[0] if target.name == "f")
        contract = backend.extract(project, target)
        observation = backend.observations(project, contract)
        obligation = backend.lower(project, contract, observation)
        candidate = backend.generate_proof(project, contract, obligation, "baseline")
        baseline = backend.check(project, obligation, candidate, self.root / (name + "-baseline"), baseline=True)
        return backend, project, contract, obligation, baseline

    def test_deterministic_boolean_and_modern_mutable_contracts(self):
        for index, declaration in enumerate((
            "pub fn f(x: bool) -> (r: bool) ensures r == x, { x }",
            "pub fn f(x: &mut u64, value: u64) ensures *final(x) == value, { *x = value; }",
        )):
            backend, project, _, obligation, baseline = self.analyze_source(declaration, name=str(index))
            self.assertEqual(baseline.status, "verified", baseline.diagnostics)
            evidence = backend.query(obligation, baseline)
            self.assertEqual(evidence.status, SolverStatus.UNSAT, evidence.reason)
            self.assertEqual(project.files["functions.rs"], text_digest(project.source_path("functions.rs").read_text()))

    def test_incomplete_contract_preserves_unknown_and_raw_search_evidence(self):
        backend, _, _, obligation, baseline = self.analyze_source(
            "pub fn f(x: bool) -> (r: bool) ensures true, { x }",
        )
        self.assertEqual(baseline.status, "unproved", baseline.diagnostics)
        initial = backend.query(obligation, baseline)
        self.assertIn(initial.status, {SolverStatus.UNKNOWN, SolverStatus.SAT})
        result = backend.search(obligation, baseline, self.root / "search", max_rounds=8)
        self.assertTrue(result.evidence)
        self.assertLessEqual(result.rounds, 8)
        if not any(item.status == SolverStatus.SAT for item in result.evidence):
            self.assertFalse(result.confirmed_constraints)
        self.assertIs(backend.query(obligation, baseline), initial)

    def test_unverified_helper_and_candidate_transcript_cannot_establish_goal(self):
        backend, project, _, obligation, baseline = self.analyze_source(
            "pub fn f(x: bool) -> (r: bool) ensures r == x, { x }",
        )
        initial = backend.query(obligation, baseline)
        candidate = ProofCandidate(
            obligation.problem_id, obligation.id, "",
            "proof fn lemma_candidate() ensures false {}", "accepted",
        )
        checked = backend.check(project, obligation, candidate, self.root / "candidate")
        self.assertEqual(checked.status, "unproved", checked.diagnostics)
        self.assertEqual(len(checked.native["invocations"]), 2)
        self.assertEqual(checked.native["invocations"][0]["status"], "verified")
        self.assertEqual(checked.native["invocations"][1]["status"], "unproved")
        self.assertNotIn("if g_", Path(checked.native["harness"]).read_text())
        with self.assertRaises(StageError):
            backend.query(obligation, checked)
        self.assertIs(backend.query(obligation, baseline), initial)

    def test_native_mode_preserves_external_module_context(self):
        root = self.root / "native"
        root.mkdir()
        sources = {
            "main.rs": "mod child;\nfn main() {}\n",
            "child.rs": "use vstd::prelude::*;\nverus! { pub fn f(x: bool)->(r: bool) ensures r == x, { x } }\n",
        }
        for name, source in sources.items():
            (root / name).write_text(source)
        files = {name: text_digest(source) for name, source in sources.items()}
        project = PreparedProject(root, root, digest(files), files)
        backend = VerusBackend(Config(
            root, self.root / "out", toolchain=self.toolchain,
            build=BuildConfig(
                adapter="verus.native", entrypoint="main.rs",
                injection_file="child.rs", verify_module="child",
            ),
            limits=Limits(solver_timeout_ms=1000),
        ))
        target = next(target for target in backend.discover(project)[0] if target.name == "f")
        self.assertEqual(target.module, "child")
        contract = backend.extract(project, target)
        obligation = backend.lower(project, contract, backend.observations(project, contract))
        candidate = backend.generate_proof(project, contract, obligation, "baseline")
        checked = backend.check(project, obligation, candidate, self.root / "native-check", baseline=True)
        self.assertEqual(checked.status, "verified", checked.diagnostics)
        evidence = backend.query(obligation, checked)
        self.assertEqual(evidence.status, SolverStatus.UNSAT, evidence.reason)
        self.assertEqual(checked.native["module"], "child")
        for name, source in sources.items():
            self.assertEqual((root / name).read_text(), source)

    def test_static_trait_declaration_is_a_real_source_contract(self):
        backend, _, contract, obligation, baseline = self.analyze_source(
            "pub trait T { fn f()->(r:u8) ensures r == 3; }",
        )
        self.assertTrue(contract.native["source_context"]["declaration"])
        self.assertEqual(baseline.status, "verified", baseline.diagnostics)
        self.assertEqual(backend.query(obligation, baseline).status, SolverStatus.UNSAT)

    def test_trait_receiver_declaration_is_a_real_source_contract(self):
        for index, declaration in enumerate((
            "pub trait T { fn f(&self)->(r:u8) requires true, ensures r == 3; }",
            "pub trait T: Sized { fn f(&mut self) ensures *final(self) == *old(self); }",
            "pub trait Sample<T> where T: Copy { fn f(&self, x:T)->(r:T) ensures r == x; }",
        )):
            backend, _, contract, obligation, baseline = self.analyze_source(
                declaration, name=f"trait-{index}",
            )
            self.assertTrue(contract.native["source_context"]["declaration"])
            self.assertEqual(baseline.status, "verified", baseline.diagnostics)
            self.assertEqual(backend.query(obligation, baseline).status, SolverStatus.UNSAT)

    def test_qualified_assume_specification_uses_its_exact_declaration(self):
        root = self.root / "declaration"
        root.mkdir()
        source = (
            "use vstd::prelude::*;\nmod other { pub fn f(x:u8)->u8 {x} }\n"
            "verus! { pub assume_specification[other::f](x:u8)->(r:u8) ensures r==x; }\n"
            "fn main() {}\n"
        )
        (root / "source.rs").write_text(source)
        files = {"source.rs": text_digest(source)}
        project = PreparedProject(root, root, digest(files), files)
        backend = VerusBackend(Config(root, self.root / "out", toolchain=self.toolchain))
        target = next(target for target in backend.discover(project)[0] if target.kind == "contract_declaration")
        contract = backend.extract(project, target)
        self.assertEqual(contract.native["function_spec"]["ensures"], ["r==x"])
        obligation = backend.lower(project, contract, backend.observations(project, contract))
        candidate = backend.generate_proof(project, contract, obligation, "baseline")
        checked = backend.check(project, obligation, candidate, self.root / "declaration-check", baseline=True)
        self.assertEqual(checked.status, "verified", checked.diagnostics)
        self.assertEqual(backend.query(obligation, checked).status, SolverStatus.UNSAT)

    def test_generic_assume_specification_uses_its_exact_turbofish_selector(self):
        root = self.root / "generic-declaration"
        root.mkdir()
        source = (
            "use vstd::prelude::*;\nmod external { pub fn identity<T>(x:T)->T {x} }\n"
            "verus! { pub assume_specification<T>[external::identity::<T>](x:T)->(r:T) requires true, ensures r==x; }\n"
            "fn main() {}\n"
        )
        (root / "source.rs").write_text(source)
        files = {"source.rs": text_digest(source)}
        project = PreparedProject(root, root, digest(files), files)
        backend = VerusBackend(Config(root, self.root / "out", toolchain=self.toolchain))
        target = next(target for target in backend.discover(project)[0] if target.kind == "contract_declaration")
        self.assertEqual(target.name, "identity")
        contract = backend.extract(project, target)
        self.assertEqual(contract.native["function_spec"]["ensures"], ["r==x"])
        self.assertIn("T", contract.native["function_spec"]["generics_decl"])
        obligation = backend.lower(project, contract, backend.observations(project, contract))
        candidate = backend.generate_proof(project, contract, obligation, "baseline")
        checked = backend.check(project, obligation, candidate, self.root / "generic-check", baseline=True)
        self.assertEqual(checked.status, "verified", checked.diagnostics)
        self.assertEqual(backend.query(obligation, checked).status, SolverStatus.UNSAT)

    def test_source_derived_reveal_candidate_proves_the_original_goal(self):
        backend, project, contract, obligation, _ = self.analyze_source(
            "pub closed spec fn p(x:u8)->bool { x==0 }\n"
            "pub fn f()->(r:u8) ensures p(r), {0}",
        )
        candidate = backend.generate_proof(project, contract, obligation, "rules")
        self.assertIsNotNone(candidate)
        checked = backend.check(project, obligation, candidate, self.root / "rule-check")
        self.assertEqual(checked.status, "verified", checked.diagnostics)
        self.assertNotIn("if g_", Path(checked.native["harness"]).read_text())

    def test_normalization_rediscovery_rechecks_the_new_frozen_target(self):
        backend, project, contract, obligation, _ = self.analyze_source(
            "pub fn f(x:bool)->(r:bool) ensures r==x, {x}",
        )
        original = contract.target
        original_bytes = project.source_path(original.file).read_bytes()
        request = GenerationRequest(
            Stage.EXTRACT, "verus", ("normalization",), contract.source_digest,
            obligation.problem_id, {}, original.id,
        )
        proposal = Proposal(
            request.id, request.stage, "verus", "normalization",
            request.source_digest, request.problem_id,
            {"edits": [{"file": original.file, "before": "pub fn f", "after": "\n\npub fn f"}]},
        )
        validation = backend.validate_proposal(request, proposal, project, original, contract, obligation)
        self.assertEqual(validation.status, "accepted")
        self.assertEqual(backend.adopt_proposal(
            proposal, project, original, contract, obligation,
        ), Stage.DISCOVER)
        target = next(target for target in backend.discover(project)[0] if target.name == "f")
        updated = backend.extract(project, target)
        frozen = backend.lower(project, updated, backend.observations(project, updated))
        self.assertNotEqual(target.id, original.id)
        self.assertNotEqual(frozen.problem_id, obligation.problem_id)
        candidate = backend.generate_proof(project, updated, frozen, "baseline")
        checked = backend.check(project, frozen, candidate, self.root / "normalized-check", baseline=True)
        self.assertEqual(checked.status, "verified", checked.diagnostics)
        self.assertEqual(backend.query(frozen, checked).status, SolverStatus.UNSAT)
        self.assertEqual(project.source_path(original.file).read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
