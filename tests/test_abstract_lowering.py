"""Native paired-input lowering and hygienic expression substitution."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import os
from pathlib import Path
import subprocess
import unittest
from uuid import uuid4

from specdet.adapters.verus.native.codegen.equal_policy import EqualPolicy
from specdet.adapters.verus.native.codegen.expressions import free_identifiers
from specdet.adapters.verus.native.codegen.gen_det import (
    Unsupported, _rename_idents_in_expr, _strip_unary_deref, _substitute_input,
    _substitute_run, _substitute_self_type, build_det_check_spec, render_template,
)
from specdet.adapters.verus.native.codegen.pairing import InputPair, pair_identifiers
from specdet.adapters.verus.native.extract.extractor import _parser, extract_spec
from specdet.adapters.verus.native.extract.types import (
    FieldInfo, FunctionSpec, Param, TypeInfo, TypeKind,
)
from specdet.adapters.verus.native.schema_search.schemas import (
    enumerate_schemas, render_guarded_template,
)
from specdet.adapters.verus.native.source import inject_into_source


def scalar_spec() -> FunctionSpec:
    scalar = TypeInfo(TypeKind.INT, "int")
    return FunctionSpec(
        "identity", [Param("x", scalar), Param("limit", scalar)], scalar,
        ["x <= limit"], ["ret == x"], result_binding="ret",
    )


def pairs_for(spec: FunctionSpec, *parameters: str, projection: str = "") -> tuple[InputPair, ...]:
    names = pair_identifiers(spec)
    return tuple(
        InputPair(parameter, *names[parameter],
                  f"{names[parameter][0]}{projection} == {names[parameter][1]}{projection}")
        for parameter in parameters
    )


class PairValidationTests(unittest.TestCase):
    def test_api_is_frozen_and_names_do_not_select_any_input(self):
        spec = scalar_spec()
        before = deepcopy(spec)
        names = pair_identifiers(spec)
        self.assertEqual(names, {
            "x": ("__specdet_in1_x", "__specdet_in2_x"),
            "limit": ("__specdet_in1_limit", "__specdet_in2_limit"),
        })
        self.assertEqual(names, pair_identifiers(spec))
        pair = pairs_for(spec, "x")[0]
        with self.assertRaises(FrozenInstanceError):
            pair.left = "changed"
        self.assertEqual(spec, before)
        self.assertNotIn("__specdet_in", build_det_check_spec(spec).det_check_template)

    def test_allocator_avoids_binders_paths_params_and_clause_identifiers(self):
        spec = scalar_spec()
        spec.params.append(Param("__specdet_in1_x", spec.return_type))
        spec.requires = [
            "forall|__specdet_in2_x: int| x <= __specdet_in2_x",
            "module::__specdet_in1_limit(x)",
        ]
        spec.ensures = ["ret == __specdet_in2_limit(x)"]
        names = pair_identifiers(spec)
        flattened = [name for pair in names.values() for name in pair]
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(names["x"], ("__specdet_in1_x_1", "__specdet_in2_x_1"))
        self.assertEqual(names["limit"], ("__specdet_in1_limit_1", "__specdet_in2_limit_1"))

    def test_unicode_and_raw_original_identifiers_are_supported(self):
        spec = scalar_spec()
        spec.params[0].name = "r#type"
        spec.params[1].name = "値"
        spec.requires = ["r#type <= 値"]
        spec.ensures = ["ret == r#type"]
        names = pair_identifiers(spec)
        self.assertEqual(names["r#type"], ("__specdet_in1_type", "__specdet_in2_type"))
        self.assertEqual(names["値"], ("__specdet_in1_値", "__specdet_in2_値"))
        generated = build_det_check_spec(spec, input_pairs=pairs_for(spec, "r#type", "値"))
        self.assertIn("__specdet_in2_type <= __specdet_in2_値", generated.det_check_template)

    def test_invalid_duplicate_unknown_and_capturing_pairs_are_rejected(self):
        spec = scalar_spec()
        pair = pairs_for(spec, "x")[0]
        cases = [
            (replace(pair, parameter="missing"),),
            (pair, pair),
            (replace(pair, left="x"),),
            (replace(pair, left="limit"),),
            (replace(pair, left="r1"),),
            (replace(pair, left=pair.right),),
            (replace(pair, left="x.field"),),
            (replace(pair, left="self"),),
            (replace(pair, left="_"),),
            (replace(pair, relation=""),),
            (replace(pair, relation=f"{pair.left} == {pair.left}"),),
            (replace(pair, relation="true"),),
            (replace(pair, relation=f"{pair.left} == {pair.right}, false"),),
            (replace(pair, relation=f"{pair.left} == {pair.right}; assume(false)"),),
            (replace(pair, relation=f"{pair.left} == {pair.right} && r1 == 0"),),
            (replace(pair, relation=f"{pair.left} == {pair.right} && x == 0"),),
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_det_check_spec(spec, input_pairs=invalid)

    def test_collisions_with_other_pairs_and_custom_check_names_are_rejected(self):
        spec = scalar_spec()
        first, second = pairs_for(spec, "x", "limit")
        with self.assertRaisesRegex(ValueError, "collision"):
            build_det_check_spec(spec, input_pairs=(first, replace(second, left=first.left)))
        with self.assertRaisesRegex(ValueError, "collision"):
            build_det_check_spec(spec, check_name=first.left, input_pairs=(first,))
        spec.requires.append("forall|taken: int| taken >= x")
        with self.assertRaisesRegex(ValueError, "collision"):
            build_det_check_spec(spec, input_pairs=(replace(first, left="taken"),))

    def test_relation_must_use_free_not_shadowed_pair_names(self):
        spec = scalar_spec()
        pair = pairs_for(spec, "x")[0]
        relation = f"forall|{pair.right}: int| {pair.left} == {pair.right}"
        with self.assertRaisesRegex(ValueError, "both paired"):
            build_det_check_spec(spec, input_pairs=(replace(pair, relation=relation),))

    def test_shared_inputs_cannot_collide_with_fixed_output_parameters(self):
        spec = scalar_spec()
        spec.params.append(Param("r1", spec.return_type))
        with self.assertRaisesRegex(ValueError, "collision"):
            build_det_check_spec(spec, input_pairs=pairs_for(spec, "x"))
        # Pairing the conflicting input removes its old shared binding.
        det = build_det_check_spec(spec, input_pairs=pairs_for(spec, "x", "r1"))
        self.assertEqual([s.name for s in det.symbols].count("r1"), 1)

    def test_duplicate_raw_or_unicode_equivalent_parameter_names_are_rejected(self):
        spec = scalar_spec()
        spec.params.append(Param("r#x", spec.return_type))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            pair_identifiers(spec)

    def test_blank_or_malformed_clauses_are_not_silently_dropped(self):
        for requires, ensures in (([""], ["ret == x"]), (["x <= limit"], [" "]),
                                  (["let x = 0; true"], ["ret == x"])):
            spec = replace(scalar_spec(), requires=requires, ensures=ensures)
            with self.subTest(requires=requires, ensures=ensures), self.assertRaises(Unsupported):
                build_det_check_spec(spec, input_pairs=pairs_for(spec, "x"))


class PairedLoweringTests(unittest.TestCase):
    def assert_parses(self, det):
        code = "verus! {\n" + render_template(det, []) + "\n}"
        self.assertFalse(_parser.parse(code.encode()).root_node.has_error, code)

    def test_concrete_template_remains_byte_for_byte_unchanged(self):
        spec = scalar_spec()
        expected = """proof fn det_identity(x: int, limit: int, r1: int, r2: int)
    requires (x <= limit),
    ensures
        ({
            &&& (r1 == x)
            &&& (r2 == x)
        }) ==> det_identity_equal(r1, r2),
{
{ASSUMES}}"""
        concrete = build_det_check_spec(spec)
        self.assertEqual(concrete.det_check_template, expected)
        self.assertEqual(concrete, build_det_check_spec(spec, input_pairs=()))
        self.assertEqual([s.name for s in concrete.symbols], ["x", "limit", "r1", "r2"])

    def test_empty_selection_never_implicitly_pairs_view_bearing_inputs(self):
        spec = scalar_spec()
        spec.params[0].type.spec_view = TypeInfo(TypeKind.INT, "int")
        det = build_det_check_spec(spec, input_pairs=())
        self.assertNotIn("__specdet_in", det.det_check_template)
        self.assertEqual(det.det_check_template.count("x: int"), 1)
        self.assertEqual(det.det_check_template.count("requires (x <= limit)"), 1)

    def test_shared_self_gets_two_references_and_fixed_output_equality(self):
        source = """verus! {
struct Record { value: u8 }
impl Record {
fn read(&self, limit: u8) -> (ret: u8)
    requires self.value <= limit,
    ensures ret == self.value,
{ self.value }
}
}"""
        spec = extract_spec(source, "read")
        before = deepcopy(spec)
        pair = pairs_for(spec, "self", projection=".value")[0]
        det = build_det_check_spec(spec, input_pairs=(pair,))
        concrete = build_det_check_spec(spec)
        for side, run in ((pair.left, 1), (pair.right, 2)):
            self.assertIn(f"{side}: &Record", det.det_check_template)
            self.assertIn(f"{side}.value <= limit", det.det_check_template)
            self.assertIn(f"r{run} == {side}.value", det.det_check_template)
        self.assertNotRegex(det.det_check_template, r"\bself_\b")
        self.assertIn(pair.relation, det.det_check_template)
        self.assertEqual(det.equal_fn_def, concrete.equal_fn_def)
        self.assertEqual(det.equal_arg_pairs, concrete.equal_arg_pairs)
        self.assertEqual(spec, before)
        self.assert_parses(det)

    def test_whole_joint_precondition_is_instantiated_per_run(self):
        scalar = TypeInfo(TypeKind.INT, "int")
        record = TypeInfo(TypeKind.STRUCT, "Record", fields=[FieldInfo("value", scalar)])
        spec = FunctionSpec(
            "select", [Param("first", record, is_ref=True), Param("second", record, is_ref=True),
                       Param("limit", scalar), Param("take_first", TypeInfo(TypeKind.BOOL, "bool"))],
            scalar, ["first.value + second.value <= limit", "limit >= 0"],
            ["ret == if take_first { first.value } else { second.value }"],
            result_binding="ret",
        )
        first, second = pairs_for(spec, "first", "second", projection=".value")
        det = build_det_check_spec(spec, input_pairs=(first, second))
        template = det.det_check_template
        for side, run in (("left", 1), ("right", 2)):
            one, two = getattr(first, side), getattr(second, side)
            self.assertIn(f"({one}.value + {two}.value <= limit)", template)
            self.assertIn(f"r{run} == if take_first {{ {one}.value }} else {{ {two}.value }}", template)
        self.assertNotIn(f"{first.left}.value + {second.right}.value", template)
        self.assertEqual(template.count("(limit >= 0)"), 2)
        self.assertEqual(template.count("limit: int"), 1)
        self.assertEqual(template.count("take_first: bool"), 1)
        self.assertEqual(template.count(first.relation), 1)
        self.assertEqual(template.count(second.relation), 1)
        self.assert_parses(det)

    def test_mutable_inputs_keep_run_specific_pre_states_and_fixed_post_states(self):
        scalar = TypeInfo(TypeKind.INT, "int")
        spec = FunctionSpec(
            "swap", [Param("x", scalar, is_mut_ref=True, is_ref=True),
                     Param("y", scalar, is_mut_ref=True, is_ref=True),
                     Param("scratch", scalar, is_mut_ref=True, is_ref=True),
                     Param("offset", scalar)],
            scalar, ["old(*x) <= old(*y)", "old(*scratch) >= 0"],
            ["final(*x) == old(*y) + offset", "*final(y) == *old(x)",
             "final(*scratch) == old(*scratch)", "ret == *x + *y"],
            result_binding="ret",
        )
        x, y = pairs_for(spec, "x", "y")
        det = build_det_check_spec(spec, input_pairs=(x, y))
        for side, run in (("left", 1), ("right", 2)):
            pre_x, pre_y = getattr(x, side), getattr(y, side)
            template = det.det_check_template
            self.assertIn(f"({pre_x} <= {pre_y})", template)
            self.assertIn(f"post{run}_x == {pre_y} + offset", template)
            self.assertIn(f"post{run}_y == {pre_x}", template)
            self.assertIn(f"post{run}_scratch == pre_scratch", template)
            self.assertIn(f"r{run} == post{run}_x + post{run}_y", template)
        self.assertNotRegex(template, r"\bpre_[xy]\b")
        self.assertNotIn("old(", template)
        self.assertNotIn("final(", template)
        self.assertEqual(template.count("pre_scratch: int"), 1)
        self.assertEqual(det.equal_arg_pairs, build_det_check_spec(spec).equal_arg_pairs)
        self.assertEqual(det.equal_fn_def, build_det_check_spec(spec).equal_fn_def)
        self.assert_parses(det)

    def test_mutable_self_old_final_forms_and_reference_arguments(self):
        spec = extract_spec("""verus! {
struct Record { value: int }
impl Record {
fn update(&mut self, offset: int)
    requires helper(old(self)), old(*self).value >= 0,
    ensures final(*self).value == old(*self).value + offset,
            helper(final(self)), helper(*final(self)),
{}
}
}""", "update")
        pair = pairs_for(spec, "self", projection=".value")[0]
        det = build_det_check_spec(spec, input_pairs=(pair,))
        for side, run in ((pair.left, 1), (pair.right, 2)):
            self.assertIn(f"helper(&{side})", det.det_check_template)
            self.assertIn(f"post{run}_self_.value == {side}.value + offset", det.det_check_template)
            self.assertIn(f"helper(&post{run}_self_)", det.det_check_template)
            self.assertIn(f"helper(post{run}_self_)", det.det_check_template)
        self.assertNotRegex(det.det_check_template, r"\bpre_self_\b")
        self.assert_parses(det)

    def test_unsized_borrows_stay_borrowed_for_both_input_copies(self):
        spec = extract_spec("""verus! {
fn length(buf: &mut [u8], text: &str) -> (ret: usize)
    requires old(buf).len() <= text.len(),
    ensures final(buf).len() == old(buf).len(), ret == final(buf).len(),
{}
}""", "length")
        buf, text = pairs_for(spec, "buf", "text", projection=".len()")
        det = build_det_check_spec(spec, input_pairs=(buf, text))
        for name in (buf.left, buf.right, "post1_buf", "post2_buf"):
            self.assertIn(f"{name}: &[u8]", det.det_check_template)
        for name in (text.left, text.right):
            self.assertIn(f"{name}: &str", det.det_check_template)
        self.assertEqual(
            _substitute_run("final(*buf) == old(*buf)", spec, 1,
                            input_bindings={"buf": buf.left}),
            f"*post1_buf == *{buf.left}",
        )
        self.assert_parses(det)

    def test_generics_and_impl_type_are_not_value_renamed(self):
        source = """verus! {
struct Container<T> { value: T }
impl<T: View> Container<T> where T: Copy {
fn same(&self, other: &T) -> (ret: bool)
    requires self.value@ == other@,
    ensures ret == (self.value@ == other@),
{ true }
}
}"""
        spec = extract_spec(source, "same")
        own, other = pairs_for(spec, "self", "other")
        own = replace(own, relation=f"{own.left}.value@ == {own.right}.value@")
        other = replace(other, relation=f"{other.left}@ == {other.right}@")
        det = build_det_check_spec(spec, input_pairs=(own, other))
        self.assertIn("<T: View>", det.det_check_template)
        self.assertIn("where T: Copy", det.det_check_template)
        self.assertIn(f"{own.left}: &Container<T>", det.det_check_template)
        self.assertIn(f"{other.right}: &T", det.det_check_template)
        self.assert_parses(det)

    def test_ghost_and_tracked_destructuring_uses_the_extracted_inner_type(self):
        source = """verus! {
fn wrapped(Ghost(x): Ghost<int>, Tracked(y): Tracked<int>)
    requires x <= y, ensures x <= y,
{}
}"""
        spec = extract_spec(source, "wrapped")
        self.assertEqual([p.destructure_ctor for p in spec.params], ["Ghost", "Tracked"])
        self.assertEqual([p.type.name for p in spec.params], ["int", "int"])
        x, y = pairs_for(spec, "x", "y")
        det = build_det_check_spec(spec, input_pairs=(x, y))
        for pair in (x, y):
            for name in (pair.left, pair.right):
                self.assertIn(f"{name}: int", det.det_check_template)
        self.assertIn(f"{x.right} <= {y.right}", det.det_check_template)
        self.assert_parses(det)

    def test_borrowed_ghost_and_tracked_inputs_keep_reference_annotations(self):
        source = """verus! {
fn wrapped(Ghost(x): Ghost<&int>, Tracked(y): Tracked<&mut int>)
    requires *x <= old(*y), ensures final(*y) == *x,
{}
}"""
        spec = extract_spec(source, "wrapped")
        x, y = pairs_for(spec, "x", "y")
        det = build_det_check_spec(spec, input_pairs=(x, y))
        self.assertIn(f"{x.left}: &int", det.det_check_template)
        self.assertIn(f"{y.left}: int", det.det_check_template)
        self.assertIn(f"post2_y == *{x.right}", det.det_check_template)
        self.assert_parses(det)

    def test_ambiguous_nested_wrapper_model_is_explicitly_unsupported(self):
        scalar = TypeInfo(TypeKind.INT, "int")
        wrapper = TypeInfo(TypeKind.GHOST, "Ghost<int>", type_args=[scalar])
        spec = FunctionSpec(
            "wrapped", [Param("x", wrapper, destructure_ctor="Ghost")], scalar,
            ["x >= 0"], ["ret == x"], result_binding="ret",
        )
        with self.assertRaisesRegex(Unsupported, "inner binding type"):
            build_det_check_spec(spec, input_pairs=pairs_for(spec, "x"))

    def test_absent_postconditions_in_paired_mode_mean_true_not_an_empty_clause(self):
        spec = replace(scalar_spec(), ensures=[])
        det = build_det_check_spec(spec, input_pairs=pairs_for(spec, "x"))
        self.assertEqual(det.det_check_template.count("&&& true"), 2)
        self.assert_parses(det)

    def test_symbols_and_search_projections_include_both_copies_only(self):
        scalar = TypeInfo(TypeKind.INT, "int")
        view = TypeInfo(TypeKind.SEQ, "Seq<int>", type_args=[scalar])
        values = TypeInfo(TypeKind.SEQ, "Vec<int>", type_args=[scalar], spec_view=view)
        record = TypeInfo(TypeKind.STRUCT, "Record", fields=[FieldInfo("values", values)])
        spec = FunctionSpec(
            "observe", [Param("record", record, is_ref=True)], scalar,
            ["record.values@.len() >= 0"], ["ret == record.values@.len()"], result_binding="ret",
        )
        before = deepcopy(spec)
        pair = pairs_for(spec, "record", projection=".values@")[0]
        det = build_det_check_spec(spec, input_pairs=(pair,))
        self.assertEqual([s.name for s in det.symbols if s.phase == "input"], [pair.left, pair.right])
        schemas = enumerate_schemas(det)
        expressions = {schema.rust_var for schema in schemas}
        self.assertIn(f"{pair.left}.values@", expressions)
        self.assertIn(f"{pair.right}.values@", expressions)
        self.assertIn(f"{pair.left}.values@[0]", expressions)
        self.assertIn(f"{pair.right}.values@[0]", expressions)
        self.assertNotIn("record.values@", expressions)
        guarded = render_guarded_template(det, schemas)
        self.assertIn(pair.relation, guarded)
        self.assertIn(f"{pair.left}.values@", guarded)
        self.assertIn(f"{pair.right}.values@", guarded)
        self.assertEqual(spec, before)
        self.assertIs(spec.params[0].type.fields[0].type.spec_view, view)

    def test_relation_source_and_equality_helpers_are_preserved(self):
        source = """verus! {
closed spec fn project(x: int) -> int { x }
fn identity(x: int) -> (ret: int) ensures ret == project(x), {}
}"""
        spec = extract_spec(source, "identity")
        pair = pairs_for(spec, "x")[0]
        relation = f"project( {pair.left} )\n        == project( {pair.right} )"
        pair = replace(pair, relation=relation)
        policy = EqualPolicy(custom_body="r1 == r2")
        concrete = build_det_check_spec(spec, source=source, equal_policy=policy)
        det = build_det_check_spec(spec, source=source, equal_policy=policy, input_pairs=(pair,))
        self.assertIn(f"({relation})", det.det_check_template)
        self.assertEqual(det.equal_fn_def, concrete.equal_fn_def)
        self.assertEqual(det.opened_closed_specs, ["project"])
        self.assertIn("reveal(project);", det.det_check_template)


class HygienicSubstitutionTests(unittest.TestCase):
    def test_unicode_offsets_literals_comments_fields_and_namespaces(self):
        text = 'é.x == x && path::x(é) && text("é x self old(x)", r##"x"##) /* x */ && x.é == é'
        expected = 'left.x == right && path::x(left) && text("é x self old(x)", r##"x"##) /* x */ && right.é == left'
        self.assertEqual(_rename_idents_in_expr(text, {"é": "left", "x": "right"}), expected)
        self.assertEqual(_rename_idents_in_expr("x::f(x)", {"x": "left"}), "x::f(left)")
        self.assertEqual(_rename_idents_in_expr("x is Some", {"x": "left", "Some": "right"}),
                         "left is Some")

    def test_raw_and_normalized_unicode_identifiers_share_binding_scope(self):
        self.assertEqual(
            _rename_idents_in_expr("r#value + value", {"value": "left"}), "left + left",
        )
        self.assertEqual(
            _rename_idents_in_expr("forall|e\u0301: int| é == x", {"é": "left", "x": "right"}),
            "forall|e\u0301: int| é == right",
        )

    def test_quantifier_binders_and_triggers_are_scoped(self):
        self.assertEqual(
            _rename_idents_in_expr(
                "(forall|x: int| #![trigger f(x, y)] x <= y) && x <= y",
                {"x": "left", "y": "right"},
            ),
            "(forall|x: int| #![trigger f(x, right)] x <= right) && left <= right",
        )
        self.assertEqual(
            _rename_idents_in_expr("choose|x: int| x >= y", {"x": "left", "y": "right"}),
            "choose|x: int| x >= right",
        )

    def test_closure_and_destructuring_parameters_shadow_only_the_body(self):
        text = "(|(x, y): (int, int)| x + y + z)(x, y)"
        self.assertEqual(
            _rename_idents_in_expr(text, {"x": "left", "y": "right", "z": "shared"}),
            "(|(x, y): (int, int)| x + y + shared)(left, right)",
        )

    def test_match_guards_patterns_and_other_arms_have_independent_scopes(self):
        text = "match x { Some(x) if x > y => x + y, None => x }"
        self.assertEqual(
            _rename_idents_in_expr(text, {"x": "left", "y": "right"}),
            "match left { Some(x) if x > right => x + right, None => left }",
        )
        spec = scalar_spec()
        self.assertEqual(_substitute_run("ret matches None", spec, 1), "r1 matches None")
        self.assertEqual(
            _rename_idents_in_expr("x matches Some(x) ==> x > y", {"x": "left", "y": "right"}),
            "left matches Some(x) ==> x > right",
        )

    def test_if_let_condition_does_not_shadow_the_else_branch(self):
        self.assertEqual(
            _rename_idents_in_expr(
                "if let Some(x) = x && x > y { x } else { x }", {"x": "left", "y": "right"},
            ),
            "if let Some(x) = left && x > right { x } else { left }",
        )

    def test_sequential_let_initializers_and_nested_scopes(self):
        text = "{ let x = x + y; { let y = x; y }; let (y, z) = (y, x); x + y + z }"
        self.assertEqual(
            _rename_idents_in_expr(text, {"x": "left", "y": "right", "z": "other"}),
            "{ let x = left + right; { let y = x; y }; let (y, z) = (right, x); x + y + z }",
        )

    def test_struct_shorthand_initializers_are_expanded_without_changing_fields(self):
        self.assertEqual(
            _rename_idents_in_expr("S { x, y: x, ..base }", {"x": "left", "y": "right", "base": "other"}),
            "S { x: left, y: left, ..other }",
        )
        self.assertEqual(
            _rename_idents_in_expr(
                "{ let S { x, field: y, .. } = x; x + y + z }", {"x": "left", "y": "right", "z": "other"},
            ),
            "{ let S { x, field: y, .. } = left; x + y + other }",
        )

    def test_substitution_avoids_capture_in_quantifiers_and_struct_patterns(self):
        self.assertEqual(
            _rename_idents_in_expr("forall|y: int| x > y", {"x": "y"}),
            "forall|__specdet_bound_y: int| y > __specdet_bound_y",
        )
        self.assertEqual(
            _rename_idents_in_expr("{ let S { ref mut y } = other; x == y }", {"x": "y"}),
            "{ let S { y: ref mut __specdet_bound_y } = other; y == __specdet_bound_y }",
        )

    def test_state_substitution_does_not_touch_shadowed_refs_or_literals(self):
        scalar = TypeInfo(TypeKind.INT, "int")
        spec = FunctionSpec(
            "mutate", [Param("x", scalar, is_ref=True, is_mut_ref=True)], scalar, [], [],
            result_binding="ret",
        )
        text = 'text("old(x) final(*x) *x ret") && (forall|x: &int| *x > 0) && ret == final(*x) + old(*x)'
        self.assertEqual(
            _substitute_run(text, spec, 1, input_bindings={"x": "left"}),
            'text("old(x) final(*x) *x ret") && (forall|x: &int| *x > 0) && r1 == post1_x + left',
        )
        self.assertEqual(
            _substitute_run("{ let x = old(*x); x == ret }", spec, 2,
                            input_bindings={"x": "right"}),
            "{ let x = right; x == r2 }",
        )

    def test_dereferences_and_multiplication_are_not_confused(self):
        spec = scalar_spec()
        spec.params[0].is_ref = spec.params[0].is_mut_ref = True
        self.assertEqual(
            _substitute_run("ret == 2 * final(*x) + 3 * *old(x)", spec, 1,
                            input_bindings={"x": "left"}),
            "r1 == 2 * post1_x + 3 * left",
        )
        self.assertEqual(_strip_unary_deref('text("*x") && (*x).f == 2 * x.f', "x", "left"),
                         'text("*x") && (left).f == 2 * x.f')
        self.assertEqual(_substitute_input("helper((old(x)))", spec), "helper((&pre_x))")

    def test_self_type_substitution_preserves_literals_and_uses_turbofish(self):
        self.assertEqual(
            _substitute_self_type('Self::f("Self", x) == Self::g(x)', "m::Record<T>"),
            'm::Record::<T>::f("Self", x) == m::Record::<T>::g(x)',
        )
        source = """verus! {
struct Record { value: int }
impl Record {
fn read(&self) -> (ret: bool) ensures ret == text("Self self old(self)"), {}
}
}"""
        spec = extract_spec(source, "read")
        pair = pairs_for(spec, "self", projection=".value")[0]
        template = build_det_check_spec(spec, input_pairs=(pair,)).det_check_template
        self.assertIn('"Self self old(self)"', template)
        self.assertIn(f"{pair.left}: &Record", template)

    def test_opaque_macros_unsupported_grammar_and_malformed_source_are_not_rewritten(self):
        for text in ("opaque!(x)", "seq![x]", "x +", '"unterminated x',
                     "{ fn nested(x: int) {} x }", "{ loop { x; } }"):
            with self.subTest(text=text), self.assertRaises(Unsupported):
                _rename_idents_in_expr(text, {"x": "left"})
        spec = scalar_spec()
        spec.ensures = ["ret == x", "opaque!(x)"]
        with self.assertRaises(Unsupported):
            build_det_check_spec(spec, input_pairs=pairs_for(spec, "x"))

    def test_unsupported_old_expressions_do_not_get_mapped_to_post_state(self):
        spec = scalar_spec()
        spec.params[0].is_ref = spec.params[0].is_mut_ref = True
        with self.assertRaisesRegex(Unsupported, "old"):
            _substitute_run("ret == old(helper(x))", spec, 1)
        with self.assertRaisesRegex(Unsupported, "final"):
            _substitute_input("final(*x) > 0", spec)


@unittest.skipUnless(os.environ.get("SPECDET_NATIVE_VERUS"), "Set SPECDET_NATIVE_VERUS to the native Verus ELF")
class NativeCompilerTests(unittest.TestCase):
    SOURCE = """use vstd::prelude::*;
verus! {
pub struct Record { pub value: u8, pub cache: u8 }
impl View for Record {
    type V = int;
    open spec fn view(&self) -> int { self.value as int }
}
impl Record {
fn value(&self) -> (ret: u8) ensures ret == self.value, { self.value }
fn leak(&self) -> (ret: u8) ensures ret == self.cache, { self.cache }
fn set(&mut self, value: u8)
    ensures (*final(self)).value == value, (*final(self)).cache == 0,
{ self.value = value; self.cache = 0; }
}
fn select(first: &Record, second: &Record, take_first: bool) -> (ret: u8)
    requires first.value <= second.value,
    ensures ret == if take_first { first.value } else { second.value },
{ if take_first { first.value } else { second.value } }
fn wrapped(Ghost(x): Ghost<int>, Tracked(y): Tracked<int>)
    requires x <= y, ensures x <= y,
{}
}
"""

    def check(self, targets: tuple[str, ...]) -> subprocess.CompletedProcess:
        executable = Path(os.environ["SPECDET_NATIVE_VERUS"])
        with executable.open("rb") as stream:
            self.assertEqual(stream.read(4), b"\x7fELF", "Use the ELF, not source-mutating shell wrappers")
        harnesses = []
        for target in targets:
            spec = extract_spec(self.SOURCE, target)
            names = ("x", "y") if target == "wrapped" else (
                ("first", "second") if target == "select" else ("self",)
            )
            projection = "" if target == "wrapped" else ".view()"
            pairs = pairs_for(spec, *names, projection=projection)
            harnesses.append(render_template(build_det_check_spec(spec, input_pairs=pairs), []))
        source = inject_into_source(self.SOURCE, "\n\n".join(harnesses))
        path = Path(f".native_pairing_{os.getpid()}_{uuid4().hex}.rs")
        try:
            path.write_text(source)
            completed = subprocess.run(
                [str(executable), "--crate-type=lib", "--crate-name=native_pairing",
                 "--edition=2021", "--num-threads=2", str(path)],
                capture_output=True, text=True, timeout=120,
                env=dict(os.environ, TMPDIR=str(Path.cwd())),
            )
            self.assertEqual(path.read_text(), source, "The native compiler must not rewrite the fixture")
            return completed
        finally:
            path.unlink(missing_ok=True)

    def test_view_determined_shared_mutable_joint_and_ghost_contracts_verify(self):
        result = self.check(("value", "set", "select", "wrapped"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 errors", result.stdout + result.stderr)

    def test_representation_leak_does_not_verify_under_input_view_equality(self):
        result = self.check(("leak",))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("postcondition not satisfied", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
