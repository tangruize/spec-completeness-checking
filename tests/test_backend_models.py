from __future__ import annotations

import json
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from specdet.adapters.verus.backend import VerusBackend
from specdet.config import Config
from specdet.domain.models import Stage, StageError, as_object, digest, text_digest
from specdet.domain.proposals import GenerationRequest, Proposal
from specdet.storage.workspace import PreparedProject


class NativeBackendModelTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / f"backend-models-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def project(self, declaration, *, directory="project", policy="verus-observable-v1"):
        root = self.root / directory
        root.mkdir()
        source = "use vstd::prelude::*;\nverus! {\n" + declaration + "\n}\nfn main() {}\n"
        (root / "source.rs").write_text(source)
        hashes = {"source.rs": text_digest(source)}
        project = PreparedProject(root, root, digest(hashes), hashes)
        backend = VerusBackend(Config(root, self.root / "out", observation_policy=policy))
        target = next(target for target in backend.discover(project)[0] if target.name == "f")
        return backend, project, target

    def lower(self, declaration, **options):
        backend, project, target = self.project(declaration, **options)
        contract = backend.extract(project, target)
        observations = backend.observations(project, contract)
        obligation = backend.lower(project, contract, observations)
        return backend, project, contract, observations, obligation

    def test_full_contract_and_obligation_are_json_roundtrippable(self):
        backend, _, contract, _, obligation = self.lower(
            "pub fn f(x: bool) -> (r: bool) ensures r == x, { x }",
        )
        serialized = json.loads(json.dumps(as_object(contract)))
        self.assertEqual(serialized["native"]["function_spec"]["ensures"], ["r == x"])
        self.assertIn("source", serialized["native"])
        self.assertEqual(obligation.problem_id, digest(obligation.native))
        self.assertEqual(json.loads(json.dumps(as_object(obligation)))["native"], obligation.native)
        self.assertIsNone(backend._executor)

    def test_problem_hash_ignores_project_and_output_locations(self):
        declaration = "pub fn f(x: bool) -> (r: bool) ensures r == x, { x }"
        first = self.lower(declaration, directory="first")[-1]
        second = self.lower(declaration, directory="second")[-1]
        self.assertEqual(first.problem_id, second.problem_id)

    def test_result_payload_policy_is_explicit_and_changes_problem(self):
        declaration = "pub fn f(x: u8) -> (r: Result<u8,u8>) ensures r is Err, { Err(x) }"
        default = self.lower(declaration, directory="default")
        strict = self.lower(declaration, directory="strict", policy="verus-strict-v1")
        self.assertIn("return.Err.payload", default[3].ignored_dimensions)
        self.assertNotIn("return.Err.payload", strict[3].ignored_dimensions)
        self.assertNotIn("Err_0", default[4].native["det_spec"]["equal_fn_def"])
        self.assertIn("Err_0", strict[4].native["det_spec"]["equal_fn_def"])
        self.assertNotEqual(default[4].problem_id, strict[4].problem_id)

    def test_pointer_opacity_is_recorded_and_strict_compares_identity(self):
        declaration = "pub fn f(x: *const u8) -> (r: *const u8) ensures r == x, { x }"
        default = self.lower(declaration, directory="default")
        strict = self.lower(declaration, directory="strict", policy="verus-strict-v1")
        self.assertIn("return.pointer_identity", default[3].ignored_dimensions)
        self.assertNotIn("return.pointer_identity", strict[3].ignored_dimensions)
        self.assertIn("return.pointee_heap", strict[3].ignored_dimensions)
        self.assertIn("opaque by default", default[4].native["det_spec"]["equal_fn_def"])
        self.assertIn("r1 == r2", strict[4].native["det_spec"]["equal_fn_def"])

    def test_mutable_poststate_is_part_of_frozen_equality(self):
        _, _, _, observations, obligation = self.lower(
            "pub fn f(x: &mut u64) ensures *x == old(*x), {}",
        )
        self.assertIn("post:x", [item["dimension"] for item in observations.native["comparisons"]])
        self.assertIn("post1_x == post2_x", obligation.native["det_spec"]["equal_fn_def"])

    def test_missing_contract_and_mutable_return_are_explicit(self):
        for index, (declaration, code) in enumerate((
            ("pub fn f() {}", "no_contract"),
            ("pub fn f(x: &mut u8) -> (r: &mut u8) ensures true, { x }", "unsupported_mutable_return"),
        )):
            backend, project, target = self.project(declaration, directory=str(index))
            with self.assertRaises(StageError) as raised:
                backend.extract(project, target)
            self.assertEqual(raised.exception.diagnostic.code, code)
            self.assertFalse(raised.exception.recoverable)

    def test_unknown_policy_and_unresolved_types_do_not_get_true_equality(self):
        declaration = "pub fn f(x: Foreign) -> (r: Foreign) ensures r == x, { x }"
        backend, project, target = self.project(declaration)
        contract = backend.extract(project, target)
        with self.assertRaises(StageError) as raised:
            backend.observations(project, contract)
        self.assertEqual(raised.exception.diagnostic.code, "observation_gap")
        backend.config = replace(backend.config, observation_policy="not-a-policy")
        with self.assertRaises(StageError):
            backend.observations(project, contract)

    def test_generic_equality_is_not_unresolved_type_opacity(self):
        _, _, _, _, obligation = self.lower(
            "pub fn f<T>(x: T) -> (r: T) ensures r == x, { x }",
        )
        self.assertIn("r1 == r2", obligation.native["det_spec"]["equal_fn_def"])

    def test_guarded_body_is_preserved_only_for_baseline(self):
        backend, project, contract, _, obligation = self.lower(
            "pub fn f(x: bool) -> (r: bool) ensures r == x, { x }",
        )
        candidate = backend.generate_proof(project, contract, obligation, "baseline")
        self.assertIn("if g_", backend._render_harness(obligation, candidate, guarded=True))
        self.assertNotIn("if g_", backend._render_harness(obligation, candidate, guarded=False))

    def test_mutating_native_contract_cannot_strengthen_original_postcondition(self):
        backend, project, target = self.project(
            "pub fn f(x: bool) -> (r: bool) ensures true, { x }",
        )
        contract = backend.extract(project, target)
        contract.native["function_spec"]["ensures"] = ["r == x"]
        with self.assertRaises(StageError) as raised:
            backend.observations(project, contract)
        self.assertEqual(raised.exception.diagnostic.code, "source_model_mismatch")

    def test_derived_type_cache_cannot_mutate_or_replace_the_frozen_contract(self):
        backend, project, target = self.project(
            "pub fn f(x: bool) -> (r: bool) ensures true, { x }",
        )
        contract = backend.extract(project, target)
        first = backend._resolved_function(contract)
        first.ensures[:] = ["false"]
        self.assertEqual(backend._resolved_function(contract).ensures, ["true"])
        plan = backend.observations(project, contract)
        self.assertEqual(backend.observations(project, contract).id, plan.id)
        contract.native["function_spec"]["ensures"] = ["false"]
        with self.assertRaises(StageError) as raised:
            backend.observations(project, contract)
        self.assertEqual(raised.exception.diagnostic.code, "source_model_mismatch")

    def test_same_name_methods_extract_by_exact_source_line(self):
        declaration = """struct A {}
struct B {}
impl A { pub fn f(&self)->(r:u8) ensures r==1, { 1 } }
impl B { pub fn f(&self)->(r:u8) ensures r==2, { 2 } }"""
        backend, project, _ = self.project(declaration)
        targets = [target for target in backend.discover(project)[0] if target.name == "f"]
        contracts = [backend.extract(project, target) for target in targets]
        self.assertEqual(
            [contract.native["function_spec"]["ensures"] for contract in contracts],
            [["r==1"], ["r==2"]],
        )
        self.assertNotEqual(contracts[0].target.id, contracts[1].target.id)

    def test_explicitly_approved_type_models_compose_by_target_and_slot(self):
        backend, project, target = self.project(
            "pub fn f(x: Foo, y: Bar) -> (r: Foo) ensures r == x, { x }",
        )
        contract = backend.extract(project, target)
        for slot, kind in (("param:x", "u8"), ("param:y", "u16")):
            request = GenerationRequest(
                Stage.EXTRACT, "verus", ("type_model",), contract.source_digest, "", {}, target.id,
            )
            proposal = Proposal(
                request.id, request.stage, "verus", "type_model", request.source_digest, "",
                {"slot": slot, "type": {"kind": kind, "name": kind}},
            )
            record = backend.validate_proposal(request, proposal, project, target, contract, None)
            self.assertEqual(record.status, "needs_approval")
            backend.adopt_proposal(proposal, project, target, contract, None)
            contract = backend.extract(project, target)
        self.assertEqual(
            [parameter["type"]["kind"] for parameter in contract.native["function_spec"]["params"]],
            ["u8", "u16"],
        )
        self.assertEqual(len(contract.native["accepted_semantics"]), 2)
        self.assertFalse(contract.trusted_translation)

    def test_native_proof_scanner_rejects_bypasses_and_accepts_real_lemmas(self):
        backend, _, _, _, obligation = self.lower(
            "pub fn f(x: bool) -> (r: bool) ensures r == x, { x }",
        )
        self.assertEqual(
            backend._scan_candidate(obligation, "lemma_simple();", "proof fn lemma_simple() ensures true {}"),
            ("lemma_simple",),
        )
        for proof, helpers in (
            ("assume(false);", ""), ("admit();", ""),
            ("", "#[verifier::external_body] proof fn lemma_bad() ensures false {}"),
            ("", "spec fn changed_equal() -> bool { true }"),
        ):
            with self.assertRaises(ValueError):
                backend._scan_candidate(obligation, proof, helpers)

    def test_static_trait_contract_declaration_is_lowered(self):
        _, _, contract, _, obligation = self.lower(
            "pub trait T { fn f()->(r:u8) ensures r == 3; }",
        )
        self.assertTrue(contract.native["source_context"]["declaration"])
        self.assertIn("contract_declaration", [item.code for item in contract.diagnostics])
        self.assertIn("r1 == 3", obligation.native["template"])

    def test_trait_receiver_context_is_lowered_with_a_bounded_self_type(self):
        _, _, contract, _, obligation = self.lower(
            "pub trait T { fn f(&self)->(r:u8) ensures r == 3; }",
        )
        self.assertIsNone(contract.native["function_spec"]["self_type"])
        self.assertIn("__DetSelf", obligation.native["template"])
        self.assertRegex(obligation.native["template"], r"__DetSelf\s*:\s*T")

    def test_rules_reveal_only_real_referenced_spec_functions(self):
        backend, project, contract, _, obligation = self.lower(
            "pub closed spec fn p(x:u8)->bool { x==0 }\n"
            "pub closed spec fn unused(x:u8)->bool { x==1 }\n"
            "pub fn f()->(r:u8) ensures p(r), {0}",
        )
        candidate = backend.generate_proof(project, contract, obligation, "rules")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.proof, "reveal(p);")
        self.assertEqual(candidate.origin, "rules")

    def test_multiline_visibility_keeps_public_line_and_native_extractor_line(self):
        backend, project, target = self.project(
            "pub\nfn f(x:bool)->(r:bool) ensures r == x, {x}",
        )
        self.assertEqual(target.line, 4)
        contract = backend.extract(project, target)
        self.assertEqual(contract.target.line, 4)
        self.assertEqual(contract.native["function_spec"]["ensures"], ["r == x"])

    def test_native_unsupported_is_a_recoverable_stage_diagnostic(self):
        from specdet.adapters.verus.native.extract.extractor import Unsupported

        backend, project, target = self.project(
            "pub fn f(x:bool)->(r:bool) ensures r == x, {x}",
        )
        with patch(
            "specdet.adapters.verus.native.extract.extractor.extract_spec",
            side_effect=Unsupported("unsupported parameter pattern"),
        ):
            with self.assertRaises(StageError) as error:
                backend.extract(project, target)
        self.assertEqual(error.exception.diagnostic.code, "extraction_gap")
        self.assertTrue(error.exception.recoverable)

    def test_old_confirmed_type_override_is_rechecked_after_feature_changes(self):
        backend, project, target = self.project(
            '#[cfg(feature="wide")] struct Word { value:u16 }\n'
            '#[cfg(not(feature="wide"))] struct Word { value:u8 }\n'
            "pub fn f(x:Word)->(r:Word) ensures r == x, {x}",
        )
        contract = backend.extract(project, target)
        original_type = contract.native["function_spec"]["return_type"]
        request = GenerationRequest(
            Stage.EXTRACT, "verus", ("type_model",), contract.source_digest, "", {}, target.id,
        )
        proposal = Proposal(
            request.id, request.stage, "verus", "type_model", request.source_digest, "",
            {"slot": "return", "type": original_type},
        )
        self.assertEqual(backend.validate_proposal(
            request, proposal, project, target, contract, None,
        ).status, "accepted")
        backend.adopt_proposal(proposal, project, target, contract, None)
        backend.config = replace(backend.config, build=replace(backend.config.build, features=("wide",)))
        updated = backend.extract(project, target)
        self.assertFalse(updated.trusted_translation)
        self.assertIn("unverified_translation", [diagnostic.code for diagnostic in updated.diagnostics])

    def test_lowering_helpers_use_active_source_without_changing_frozen_source(self):
        from specdet.adapters.verus.native.codegen.gen_det import build_det_check_spec

        backend, project, target = self.project(
            '#[cfg(feature="other")] pub open spec fn p(x:u8)->bool { x==1 }\n'
            '#[cfg(not(feature="other"))] pub open spec fn p(x:u8)->bool { x==0 }\n'
            "pub fn f()->(r:u8) ensures p(r), {0}",
        )
        contract = backend.extract(project, target)
        observations = backend.observations(project, contract)
        with patch(
            "specdet.adapters.verus.native.codegen.gen_det.build_det_check_spec",
            wraps=build_det_check_spec,
        ) as generated:
            obligation = backend.lower(project, contract, observations)
        lookup = generated.call_args.kwargs["source"]
        self.assertNotIn("x==1", lookup)
        self.assertIn("x==0", lookup)
        self.assertIn("x==1", obligation.native["source"])

    def test_attribute_only_contracts_preserve_their_return_binding(self):
        for index, attribute in enumerate((
            "#[verus_spec(ret => requires x < 255, ensures ret == x)]",
            "#[cfg_attr(verus_keep_ghost, verus_spec(ret => requires x < 255, ensures ret == x))]",
        )):
            backend, project, target = self.project(
                attribute + "\nfn f(x:u8)->u8 {x}", directory=f"attribute-{index}",
            )
            contract = backend.extract(project, target)
            native = contract.native["function_spec"]
            self.assertEqual(native["result_binding"], "ret")
            self.assertEqual(native["requires"], ["x < 255"])
            self.assertEqual(native["ensures"], ["ret == x"])
            self.assertTrue(any(
                diagnostic.code == "partial_discovery"
                for diagnostic in backend.discover(project)[1]
            ))


if __name__ == "__main__":
    unittest.main()
