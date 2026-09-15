"""Lossless extraction serialization and explicit mechanical transformations."""
from __future__ import annotations

import json
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from specdet.adapters.verus.native.codegen.gen_det import (
    _generic_view_equal, build_det_check_spec, render_template,
)
from specdet.adapters.verus.native.extract import function_spec_from_dict, function_spec_to_dict
from specdet.adapters.verus.native.extract.extractor import (
    Unsupported, _extract_fn_chunk, _find_function_items, _function_item_name,
    _parser, extract_spec,
)
from specdet.adapters.verus.native.extract.narrow import _bisect_range, _full_int_range, AssumeNode
from specdet.adapters.verus.native.extract.predicates import AssumePred, DiscEqPred
from specdet.adapters.verus.native.extract.type_registry import TypeExpr, build_registry
from specdet.adapters.verus.native.extract.types import (
    FieldInfo, FunctionSpec, Param, TypeInfo, TypeKind, VariantInfo,
)
from specdet.adapters.verus.native.source import inject_into_source
from specdet.adapters.verus.native.spec_helpers import (
    closed_spec_fn_qualified_names, reachable_spec_fns, rewrite_closed_to_opaque,
)
from specdet.adapters.verus.native.proof_validation import scan_helper_lemmas, scan_proof_block
from specdet.adapters.verus.native.view.impl_scanner import ImplScan, scan_source
from specdet.adapters.verus.native.view.registry import AcceptedView, ViewRegistry


class SerializationTests(unittest.TestCase):
    def test_all_function_spec_fields_survive_json_round_trip(self):
        scalar = TypeInfo(TypeKind.U8, "u8")
        inner = TypeInfo(
            TypeKind.STRUCT, "Inner<T>",
            fields=[FieldInfo("value", scalar)],
            type_args=[TypeInfo(TypeKind.UNKNOWN, "T")],
            spec_view=scalar, is_opaque=True, is_ext_equal=True,
        )
        result = TypeInfo(
            TypeKind.ENUM, "Outcome<T>",
            variants=[VariantInfo("Data", inner, struct_form=True), VariantInfo("Code", discriminant=7)],
            type_args=[inner], is_ext_equal=True,
        )
        spec = FunctionSpec(
            name="target",
            params=[
                Param("self", inner, is_mut_ref=True, is_ref=True, is_self=True),
                Param("ghost_value", scalar, destructure_ctor="Ghost"),
            ],
            return_type=result, requires=["predicate(self)"], ensures=["bound(ret)"],
            type_defs={"Inner": inner, "Outcome": result}, result_binding="ret",
            generics_decl="<T: Trait, const N: usize>", where_decl="where T: Clone",
            self_type="Container<T, N>", trait_name="SomeTrait",
        )
        encoded = function_spec_to_dict(spec)
        self.assertEqual(set(encoded), {field.name for field in fields(FunctionSpec)})
        self.assertEqual(function_spec_from_dict(json.loads(json.dumps(encoded))), spec)

    def test_disc_eq_pred_is_part_of_the_public_predicate_union(self):
        self.assertIn(DiscEqPred, AssumePred.__args__)

    def test_native_bounds_and_nat_domain(self):
        self.assertEqual(_full_int_range(TypeInfo(TypeKind.U8, "u8")), (0, 256))
        self.assertEqual(_full_int_range(TypeInfo(TypeKind.I8, "i8")), (-128, 128))
        self.assertEqual(_full_int_range(TypeInfo(TypeKind.INT, "nat"))[0], 0)

    def test_bisection_does_not_report_a_rejected_singleton(self):
        class RejectingContext:
            def test_and_set(self, *args, **kwargs):
                return False
        self.assertIsNone(_bisect_range("x", 1, 1, AssumeNode("x"), RejectingContext()))

    def test_duplicate_function_requires_explicit_source_line(self):
        source = "mod a { fn f() {} }\nmod b { fn f() {} }\n"
        with self.assertRaisesRegex(Unsupported, "source_line"):
            extract_spec(source, "f")


class SourcePreparationTests(unittest.TestCase):
    def test_injection_does_not_implicitly_rewrite_source(self):
        source = "verus! {\nfn f(&mut self) ensures self == old(self), {}\n}\n"
        injected = inject_into_source(source, "proof fn det_f() {}")
        self.assertIn("ensures self == old(self)", injected)
        self.assertIn("proof fn det_f()", injected)

    def test_compatibility_rewrite_requires_permission_and_reports_change(self):
        source = "verus! {\nfn f(&mut self) ensures self == old(self), {}\n}\n"
        diagnostics = []
        with self.assertLogs("specdet.adapters.verus.native.source", level="WARNING"):
            injected = inject_into_source(
                source, "proof fn det_f() {}",
                allowed_transformations=["self_reference_equality"],
                diagnostics=diagnostics,
            )
        self.assertIn("*self == *old(self)", injected)
        self.assertEqual(diagnostics, ["Applied source transformation: self_reference_equality"])

    def test_closed_specs_are_only_annotated_when_explicitly_named(self):
        source = "verus! {\npub closed spec fn f() -> bool { true }\n}\n"
        self.assertNotIn("verifier::opaque", inject_into_source(source, ""))
        diagnostics = []
        with self.assertLogs("specdet.adapters.verus.native.source", level="WARNING"):
            injected = inject_into_source(source, "", open_closed_specs=["f"], diagnostics=diagnostics)
        self.assertIn("#[verifier::opaque]\npub closed spec fn f", injected)
        self.assertNotIn("open spec fn", injected)
        self.assertEqual(len(diagnostics), 1)

    def test_no_macro_scope_is_not_guessed_from_an_arbitrary_closing_brace(self):
        with self.assertRaisesRegex(ValueError, "injection scope"):
            inject_into_source("fn main() {}", "proof fn det_f() {}")

    def test_unknown_transformation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown source transformations"):
            inject_into_source("verus! {}", "", allowed_transformations=["implicit_magic"])

    def test_only_reachable_spec_names_are_collected(self):
        source = """
spec fn first() -> bool { second() }
spec fn second() -> bool { true }
spec fn unused() -> bool { false }
"""
        self.assertEqual(reachable_spec_fns(["first()"], source), {"first", "second"})

    def test_commented_out_macro_does_not_capture_injection(self):
        source = '// verus! { misleading }\nverus! {\nspec fn f() -> bool { true }\n}\n'
        injected = inject_into_source(source, "proof fn det_f() {}")
        self.assertTrue(injected.startswith("// verus! { misleading }\nverus! {"))
        self.assertGreater(injected.index("proof fn det_f"), injected.index("spec fn f"))

    def test_reachable_scan_ignores_comments_and_balances_literal_braces(self):
        source = '''
spec fn real() -> bool { /* } unused() */ let text = "}"; true }
spec fn unused() -> bool { false }
'''
        self.assertEqual(reachable_spec_fns(["real() /* unused() */"], source), {"real"})

    def test_closed_rewrite_preserves_noncode_and_nonselected_headers(self):
        source = """
// pub closed spec fn selected() -> bool { false }
pub closed /* keep */ spec fn untouched() -> bool { true }
pub closed spec fn selected() -> bool { true }
"""
        rewritten = rewrite_closed_to_opaque(source, ["selected"])
        self.assertIn("// pub closed spec fn selected()", rewritten)
        self.assertIn("pub closed /* keep */ spec fn untouched()", rewritten)
        self.assertEqual(rewritten.count("#[verifier::opaque]"), 1)
        self.assertEqual(rewrite_closed_to_opaque(rewritten, ["selected"]), rewritten)

    def test_closed_spec_reveals_do_not_guess_between_same_named_methods(self):
        source = """
impl First {
    pub closed spec fn predicate() -> bool { true }
}
impl Second {
    pub closed spec fn predicate() -> bool { false }
}
"""
        with self.assertRaisesRegex(ValueError, "Ambiguous closed spec fn"):
            closed_spec_fn_qualified_names(source, ["predicate"])

    def test_unambiguous_closed_spec_method_keeps_its_owner(self):
        source = """
impl Second {
    pub closed spec fn predicate() -> bool { false }
}
"""
        self.assertEqual(
            closed_spec_fn_qualified_names(source, ["predicate"]),
            {"predicate": "Second::predicate"},
        )


class DeclarationExtractionTests(unittest.TestCase):
    def test_trait_signature_keeps_requires_and_sibling_ensures(self):
        source = """verus! {
trait Sample {
    fn get(&self, x: u8) -> (ret: u8) requires x < 10, ensures ret == x;
}
}"""
        spec = extract_spec(source, "get", source_line=3)
        self.assertEqual(spec.trait_name, "Sample")
        self.assertIsNone(spec.self_type)
        self.assertEqual(spec.result_binding, "ret")
        self.assertEqual(spec.requires, ["x < 10"])
        self.assertEqual(spec.ensures, ["ret == x"])
        self.assertTrue(spec.params[0].is_self)
        self.assertIn("__DetSelf: Sample", build_det_check_spec(spec).det_check_template)

    def test_external_spec_supports_qualified_and_short_target_names(self):
        source = """verus! {
pub assume_specification [external::x](a: u8) -> (ret: u8)
    requires a < 10, ensures ret == a;
}"""
        qualified = extract_spec(source, "external::x", source_line=2)
        short = extract_spec(source, "x", source_line=2)
        self.assertEqual(qualified.requires, ["a < 10"])
        self.assertEqual(qualified.ensures, ["ret == a"])
        self.assertEqual(qualified.result_binding, "ret")
        self.assertEqual(qualified.params, short.params)
        self.assertEqual(qualified.return_type.kind, TypeKind.U8)
        det = build_det_check_spec(qualified)
        self.assertEqual(det.check_fn_name, "det_external__x")
        self.assertFalse(_parser.parse(("verus! {" + render_template(det, []) + "}").encode()).root_node.has_error)

    def test_generic_external_spec_unwraps_target_turbofish(self):
        source = """verus! {
pub assume_specification<T: Clone> [external::x::<T>](a: T) -> (ret: T)
    ensures ret == a;
}"""
        spec = extract_spec(source, "external::x", source_line=2)
        self.assertEqual(spec.generics_decl, "<T: Clone>")
        self.assertEqual(spec.params[0].type.name, "T")
        self.assertEqual(spec.return_type.name, "T")
        self.assertEqual(spec.ensures, ["ret == a"])
        self.assertEqual(
            extract_spec(source, "external::x::<T>", source_line=2).ensures,
            ["ret == a"],
        )
        node = _find_function_items(_parser.parse(source.encode()))[0]
        self.assertEqual(_function_item_name(node), "external::x")

    def test_generic_trait_context_is_lifted_without_losing_trait_arguments(self):
        source = """verus! {
trait Sample<'a, T: View, const N: usize> where T: Clone {
    fn get(&'a self, x: T) -> (ret: T) ensures ret == x;
}
}"""
        spec = extract_spec(source, "get", source_line=3)
        self.assertEqual(spec.generics_decl, "<'a, T: View, const N: usize>")
        self.assertEqual(spec.where_decl, "where T: Clone")
        self.assertEqual(spec.trait_name, "Sample<'a, T, N>")
        before = function_spec_to_dict(spec)
        det = build_det_check_spec(spec)
        self.assertEqual(function_spec_to_dict(spec), before)
        self.assertIn("__DetSelf: Sample<'a, T, N>", det.det_check_template)
        self.assertIn("const N: usize", det.det_check_template)
        self.assertFalse(_parser.parse(("verus! {" + render_template(det, []) + "}").encode()).root_node.has_error)

    def test_duplicate_signatures_require_an_exact_source_line(self):
        source = """verus! {
trait A { fn get() -> (ret: u8) ensures ret == 1; }
trait B { fn get() -> (ret: u8) ensures ret == 2; }
}"""
        with self.assertRaisesRegex(Unsupported, "source_line"):
            extract_spec(source, "get")
        self.assertEqual(extract_spec(source, "get", source_line=3).ensures, ["ret == 2"])
        with self.assertRaisesRegex(Unsupported, "line 9"):
            extract_spec(source, "get", source_line=9)

    def test_same_line_impl_context_uses_the_selected_node_not_smallest_sibling(self):
        source = (
            "verus! { struct Bigger { value: u8 } struct Small; "
            "impl Bigger { fn get(&self) -> (ret: u8) ensures ret == self.value { 0 } } "
            "impl Small { fn other(&self) {} } }"
        )
        spec = extract_spec(source, "get", source_line=1)
        self.assertEqual(spec.self_type, "Bigger")

    def test_previous_declarations_attributes_do_not_override_own_contract(self):
        source = """verus! { trait Sample {
#[verus_spec(ret => ensures ret == a)]
fn first(a: u8) -> u8;
fn second(b: u8) -> (ret: u8) ensures ret == b;
} }"""
        self.assertEqual(extract_spec(source, "first", source_line=3).ensures, ["ret == a"])
        self.assertEqual(extract_spec(source, "second", source_line=4).ensures, ["ret == b"])

    def test_source_recovery_never_selects_the_nearest_other_line(self):
        self.assertEqual(_extract_fn_chunk("\nfn f() {}\n", "f", source_line=9), ("", None))
        self.assertEqual(_extract_fn_chunk('// fn f() {}\nlet s = "fn f() {}";', "f"), ("", None))


class ModernMutableStateTests(unittest.TestCase):
    def test_both_final_dereference_spellings_become_post_state_values(self):
        for expression in ("final(*slot)", "*final(slot)"):
            with self.subTest(expression=expression):
                source = (
                    "verus! { fn store(slot: &mut u8, value: u8) ensures "
                    + expression + " == value, { *slot = value; } }"
                )
                spec = extract_spec(source, "store")
                self.assertEqual(spec.ensures, [expression + " == value"])
                generated = build_det_check_spec(spec).det_check_template
                self.assertIn("post1_slot == value", generated)
                self.assertIn("post2_slot == value", generated)
                self.assertNotIn("final(", generated)

    def test_old_value_and_final_value_use_distinct_state_parameters(self):
        source = """verus! {
fn add(slot: &mut u8, value: u8)
    requires old(*slot) < 100,
    ensures final(*slot) == old(*slot) + value,
{ *slot = value; }
}"""
        generated = build_det_check_spec(extract_spec(source, "add")).det_check_template
        self.assertIn("pre_slot < 100", generated)
        self.assertIn("post1_slot == pre_slot + value", generated)
        self.assertIn("post2_slot == pre_slot + value", generated)
        self.assertNotIn("old(", generated)
        self.assertNotIn("final(", generated)

    def test_final_self_projection_uses_each_output_self(self):
        source = """verus! {
struct Counter { value: u8 }
impl Counter {
    fn store(&mut self, value: u8) ensures final(*self).value == value,
    { self.value = value; }
}
}"""
        generated = build_det_check_spec(extract_spec(source, "store")).det_check_template
        self.assertIn("post1_self_.value == value", generated)
        self.assertIn("post2_self_.value == value", generated)
        self.assertNotIn("final(", generated)

    def test_final_value_rewrite_keeps_binary_multiplication(self):
        source = """verus! {
fn store(slot: &mut u8, value: u8) ensures 2 * final(*slot) == value,
{ *slot = value; }
}"""
        generated = build_det_check_spec(extract_spec(source, "store")).det_check_template
        self.assertIn("2 * post1_slot == value", generated)
        self.assertIn("2 * post2_slot == value", generated)


class ExplicitViewsTests(unittest.TestCase):
    def test_mechanical_registry_from_explicit_source_strings_uses_no_disk(self):
        source = "struct Packet { value: u8 }"
        additional = """verus! {
impl View for Packet {
    type V = int;
    open spec fn view(&self) -> Self::V { self.value as int }
}
}"""
        with patch.object(Path, "read_text", side_effect=AssertionError("Unexpected disk lookup")):
            registry = ViewRegistry.from_sources(source, [additional])
            result = registry.resolve(TypeExpr("leaf", "Packet", raw="Packet"))
        self.assertEqual(result.layer, "L3")
        self.assertEqual(result.view_type_text, "int")
        self.assertEqual(result.view_expr("packet"), "(packet).view()")
        self.assertEqual(registry.diagnostics, [])

    def test_additional_source_parse_failures_are_exposed(self):
        with self.assertLogs("specdet.adapters.verus.native", level="WARNING"):
            registry = ViewRegistry.from_sources("struct Packet;", ["struct Broken {"])
        self.assertTrue(any("<source:1>" in message for message in registry.diagnostics))

    def test_no_accepted_view_is_looked_up_implicitly(self):
        registry = ViewRegistry({}, ImplScan("<memory>"))
        expression = TypeExpr("leaf", "Opaque", raw="Opaque")
        self.assertEqual(registry.resolve(expression).layer, "uncovered")
        self.assertFalse(hasattr(registry, "llm_cache"))

    def test_explicit_mapping_resolves_only_supplied_entries(self):
        declaration = "impl View for Opaque { type V = int; }"
        registry = ViewRegistry(
            {}, ImplScan("<memory>"),
            accepted_views={"Opaque": AcceptedView("int", declaration)},
        )
        resolution = registry.resolve(TypeExpr("leaf", "Opaque", raw="Opaque"))
        self.assertEqual(resolution.layer, "L4")
        self.assertEqual(resolution.prelude_decl, declaration)
        self.assertEqual(resolution.view_expr("x"), "(x).view()")
        self.assertEqual(registry.resolve(TypeExpr("leaf", "Other", raw="Other")).layer, "uncovered")

    def test_unaccepted_mapping_entry_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not accepted"):
            ViewRegistry({}, ImplScan("<memory>"), accepted_views={
                "Opaque": {"viewed_type": "int", "view_decl": "impl View for Opaque {}", "accepted": False},
            })

    def test_from_project_works_without_a_cache_or_accepted_entries(self):
        with patch.object(Path, "is_dir", return_value=True), patch.object(Path, "rglob", return_value=[]):
            registry = ViewRegistry.from_project(Path("project"))
        self.assertEqual(registry.accepted_views, {})
        self.assertEqual(registry.diagnostics, [])

    def test_from_project_records_file_parse_exceptions(self):
        with (
            patch.object(Path, "is_dir", return_value=True),
            patch.object(Path, "rglob", return_value=[Path("broken.rs")]),
            patch(
                "specdet.adapters.verus.native.view.registry.build_registry_from_file",
                side_effect=ValueError("malformed source"),
            ),
            self.assertLogs("specdet.adapters.verus.native.view.registry", level="WARNING"),
        ):
            registry = ViewRegistry.from_project(Path("project"))
        self.assertTrue(any("malformed source" in message for message in registry.diagnostics))

    def test_source_parse_failures_are_logged_and_exposed(self):
        source = "struct Broken { field: "
        with self.assertLogs("specdet.adapters.verus.native", level="WARNING"):
            registry = build_registry(source)
            scan = scan_source(source)
        self.assertTrue(registry.diagnostics)
        self.assertTrue(scan.diagnostics)
        self.assertTrue(ViewRegistry({}, scan).diagnostics)

    def test_project_specific_trait_names_do_not_imply_view(self):
        ty = TypeInfo(TypeKind.UNKNOWN, "T")
        self.assertIsNone(_generic_view_equal(ty, "a", "b", "<T: PersistentMemoryRegion>"))
        self.assertEqual(_generic_view_equal(ty, "a", "b", "<T: View>"), "(a)@ == (b)@")

    def test_view_bound_on_parent_does_not_imply_associated_type_view(self):
        ty = TypeInfo(TypeKind.UNKNOWN, "T::Item")
        self.assertIsNone(_generic_view_equal(ty, "a", "b", "<T: View>"))
        self.assertEqual(
            _generic_view_equal(ty, "a", "b", "<T> where T::Item: View"),
            "(a)@ == (b)@",
        )


class ProofLexicalTests(unittest.TestCase):
    def test_comment_markers_in_strings_cannot_hide_following_assumes(self):
        for literal in ['"//"', '"/*"', 'r##"// /*"##']:
            with self.subTest(literal=literal):
                violations = scan_proof_block(f"let text = {literal}; assume(false);")
                self.assertTrue(any("assume" in violation.pattern for violation in violations))

    def test_nested_comments_and_raw_string_contents_do_not_trigger(self):
        block = '''/* outer /* nested */ assume(false); */
let s = r###"a "## assume(false); //#"###;
let c = '}'; assert(true);
'''
        self.assertEqual(scan_proof_block(block), [])

    def test_helper_prefix_does_not_allow_an_exec_function(self):
        self.assertTrue(scan_helper_lemmas("fn lemma_not_a_proof() {}"))
        self.assertEqual(scan_helper_lemmas("proof fn lemma_checked() {}"), [])

    def test_malformed_literals_are_rejected(self):
        for block in ['let s = "unterminated', '/* unterminated', 'let s = r##"unterminated']:
            with self.subTest(block=block):
                self.assertTrue(scan_proof_block(block))
                self.assertTrue(scan_helper_lemmas(block))

    def test_violation_offsets_refer_to_original_source(self):
        violations = scan_proof_block('let s = "text";\n  assume(false);')
        violation = next(item for item in violations if "assume" in item.pattern)
        self.assertEqual((violation.line, violation.col), (2, 3))


if __name__ == "__main__":
    unittest.main()
