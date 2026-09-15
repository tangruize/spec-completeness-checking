from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from specdet.adapters.verus.abstract import resolve_inputs
from specdet.adapters.verus.backend import VerusBackend
from specdet.config import Config, Limits, ToolchainConfig
from specdet.domain.models import SolverStatus, digest, text_digest
from specdet.storage.workspace import PreparedProject


class ArchitectureRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def setup_source(self, declaration, *, name="source", abstract=False, inputs=()):
        root = self.root / name
        root.mkdir()
        source = f"use vstd::prelude::*;\nverus! {{\n{declaration}\n}}\nfn main() {{}}\n"
        (root / "source.rs").write_text(source)
        files = {"source.rs": text_digest(source)}
        project = PreparedProject(root, root, digest(files), files)
        backend = VerusBackend(Config(
            root, self.root / "out",
            analysis_kind="abstract_determinism" if abstract else "concrete_determinism",
            abstract_inputs=inputs, abstract_require_concrete=False,
            limits=Limits(verifier_timeout_seconds=30, solver_timeout_ms=1000, max_search_rounds=4),
            toolchain=ToolchainConfig(
                executable=os.environ.get("SPECDET_VERUS", ""),
                rust_toolchain=os.environ.get("SPECDET_RUST_TOOLCHAIN", ""),
            ),
        ))
        target = next(item for item in backend.discover(project)[0] if item.name == "f")
        contract = backend.extract(project, target)
        observations = backend.observations(project, contract)
        obligation = backend.lower(project, contract, observations)
        return backend, project, contract, observations, obligation

    def test_alias_names_never_override_resolved_payload_fields(self):
        for name in ("PResult", "SResult", "UnrelatedName"):
            with self.subTest(alias=name):
                _, _, _, _, obligation = self.setup_source(
                    f"""type {name}<T, E> = Result<(usize, T, bool), E>;
#[verifier::external_body]
pub fn f(x: u8) -> (r: {name}<u8, ()>)
    ensures r is Ok, r->Ok_0.0 == 0, r->Ok_0.1 == x,
{{ unimplemented!() }}""", name=name,
                )
                equality = obligation.native["det_spec"]["equal_fn_def"]
                self.assertIn(".2", equality)
                self.assertIn(".0", equality)
                self.assertIn(".1", equality)

    def test_exact_self_underscore_parameter_wins_over_receiver_alias(self):
        declaration = """pub struct Item { visible: u8, cache: u8 }
impl View for Item {
    type V = u8;
    open spec fn view(&self) -> u8 { self.visible }
}
impl Item {
    pub fn f(&self, self_: &Item) -> (ret: u8)
        ensures ret == self_.cache,
    { self_.cache }
}"""
        _, _, _, observations, obligation = self.setup_source(
            declaration, abstract=True, inputs=("self_",),
        )
        pairs = observations.native["input_relation"]["pairs"]
        self.assertEqual([pair["parameter"] for pair in pairs], ["self_"])
        template = obligation.native["template"]
        self.assertIn(pairs[0]["left"] + ".cache", template)
        self.assertIn(pairs[0]["right"] + ".cache", template)
        self.assertNotIn("(self_.cache)", template)

    def test_raw_pointer_input_uses_pointer_data_view_not_pointee(self):
        for pointer in ("*const u8", "*mut u8"):
            with self.subTest(pointer=pointer):
                _, _, _, observations, obligation = self.setup_source(
                    f"""#[verifier::external_body]
pub fn f(p: {pointer}) -> (r: u8) ensures r == 7, {{ unimplemented!() }}""",
                    name=pointer.replace(" ", "_"), abstract=True,
                )
                pair = observations.native["input_relation"]["pairs"][0]
                self.assertEqual(pair["relation"], f"({pair['left']})@ == ({pair['right']})@")
                self.assertIn("PtrData<u8>", observations.inputs[0]["view_type"])
                self.assertNotIn(f"*({pair['left']})", obligation.native["template"])

    @unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Set SPECDET_VERUS for real compiler regressions")
    def test_new_goals_do_not_prove_omitted_fields_and_pointer_views_compile(self):
        cases = [
            ("alias", """type PResult<T, E> = Result<(usize, T, bool), E>;
#[verifier::external_body]
pub fn f(x: u8) -> (r: PResult<u8, ()>)
    ensures r is Ok, r->Ok_0.0 == 0, r->Ok_0.1 == x,
{ unimplemented!() }""", False, "unproved"),
            ("pointer", """#[verifier::external_body]
pub fn f(p: *const u8) -> (r: u8) ensures r == 7, { unimplemented!() }""", True, "verified"),
        ]
        for name, declaration, abstract, expected in cases:
            with self.subTest(name=name):
                backend, project, contract, _, obligation = self.setup_source(
                    declaration, name=name, abstract=abstract,
                )
                candidate = backend.generate_proof(project, contract, obligation, "baseline")
                checked = backend.check(project, obligation, candidate, self.root / f"check-{name}", baseline=True)
                self.assertEqual(checked.status, expected, checked.diagnostics)
                if name == "alias":
                    self.assertNotEqual(backend.query(obligation, checked).status, SolverStatus.UNSAT)


if __name__ == "__main__":
    unittest.main()

