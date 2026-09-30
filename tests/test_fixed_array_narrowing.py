"""Fixed-array refinements use element slots, not impossible length guesses."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import z3

from specdet.adapters.verus.native.extract.array_bounds import fixed_array_extent
from specdet.adapters.verus.native.extract.narrow import AssumeNode, narrow
from specdet.adapters.verus.native.extract.types import (
    DetCheckSpec, FieldInfo, Symbol, TypeInfo, TypeKind,
)
from specdet.adapters.verus.native.schema_search.schemas import (
    SchemaKind, enumerate_schemas,
)
from specdet.adapters.verus.native.schema_search.search import SchemaCtx, run_schema_search


def array_type(name="[usize; 8]"):
    return TypeInfo(TypeKind.SEQ, name, type_args=[TypeInfo(TypeKind.USIZE, "usize")])


class RecordingContext:
    def __init__(self):
        self.tree = AssumeNode("root")
        self.det_spec = DetCheckSpec(function="example", det_check_template="", symbols=[])
        self.trace = []
        self.diagnostics = []
        self.assumes = []

    def test_and_set(self, node, assume, phase=""):
        self.assumes.append(assume)
        return False


class FixedArrayExtentTests(unittest.TestCase):
    def test_literal_extents_are_read_from_the_outer_array_ast(self):
        for text, expected in [
            ("[u8; 8]", 8),
            ("[u8; 0]", 0),
            ("[[u8; 2]; 8]", 8),
            ("[Result<u8, u16>; 0x8_usize]", 8),
            ("[u8; 0o10]", 8),
            ("[u8; 0b1000]", 8),
            ("[u8; 1_024]", 1024),
            ("[u8; 008]", 8),
            ("[u8; (8)]", 8),
            ("[u8; 8 /* ; misleading length 0 */]", 8),
        ]:
            with self.subTest(text=text):
                self.assertEqual(fixed_array_extent(text), (True, expected))

    def test_dynamic_sequences_and_outer_slices_are_not_fixed_arrays(self):
        for text in ("Seq<u8>", "Vec<u8>", "[u8]", "[[u8; 8]]", "Seq<[u8; 8]>"):
            with self.subTest(text=text):
                self.assertEqual(fixed_array_extent(text), (False, None))

    def test_constant_expressions_are_not_evaluated_or_guessed(self):
        for text in ("[u8; N]", "[u8; crate::N]", "[u8; { 4 + 4 }]", "[u8; 2 * 4]"):
            with self.subTest(text=text):
                self.assertEqual(fixed_array_extent(text), (True, None))

    def test_invalid_type_fragments_do_not_supply_an_extent(self):
        for text in ("[u8; 8", "[u8; 8]; type Other = [u8; 0]", "[u8;]"):
            with self.subTest(text=text):
                self.assertEqual(fixed_array_extent(text), (False, None))


class FixedArrayNarrowingTests(unittest.TestCase):
    def test_fixed_extent_skips_length_and_reaches_every_compiled_element(self):
        ctx = RecordingContext()
        node = ctx.tree.get_or_create("values")
        narrow(array_type(), "values", node, ctx)
        expressions = [assume.expression for assume in ctx.assumes]
        self.assertTrue(expressions)
        self.assertFalse(any(".len()" in expression for expression in expressions))
        self.assertNotIn("len", node.children)
        for index in range(8):
            self.assertTrue(any(f"values[{index}]" in expression for expression in expressions))
        self.assertFalse(any("values[8]" in expression for expression in expressions))

    def test_short_and_empty_arrays_never_probe_out_of_bounds_elements(self):
        for length in (0, 1, 2):
            with self.subTest(length=length):
                ctx = RecordingContext()
                narrow(array_type(f"[usize; {length}]"), "values", ctx.tree, ctx)
                self.assertEqual(
                    {assume.var_name for assume in ctx.assumes},
                    {f"values[{index}]" for index in range(length)},
                )

    def test_large_extents_are_bounded_to_precompiled_element_slots(self):
        ctx = RecordingContext()
        narrow(array_type("[usize; 1000000000000]"), "values", ctx.tree, ctx)
        self.assertEqual(len(ctx.assumes), 16)
        self.assertEqual(
            {assume.var_name for assume in ctx.assumes},
            {f"values[{index}]" for index in range(8)},
        )

    def test_symbolic_extents_skip_guesses_and_report_the_limitation(self):
        ctx = RecordingContext()
        narrow(array_type("[usize; N]"), "values", ctx.tree, ctx)
        self.assertEqual(ctx.assumes, [])
        self.assertTrue(any("unresolved constant extent" in note for note in ctx.diagnostics))

    def test_struct_field_uses_its_fixed_array_extent(self):
        ctx = RecordingContext()
        ty = TypeInfo(
            TypeKind.STRUCT, "Buffer",
            fields=[FieldInfo("words", array_type())],
        )
        narrow(ty, "before", ctx.tree, ctx)
        variables = {assume.var_name for assume in ctx.assumes}
        self.assertEqual(variables, {f"before.words[{index}]" for index in range(8)})

    def test_variable_sequences_slices_and_vectors_keep_length_search(self):
        types = [array_type("Seq<usize>"), array_type("[usize]")]
        vector = array_type("Vec<usize>")
        vector.spec_view = array_type("Seq<usize>")
        types.append(vector)
        for ty in types:
            with self.subTest(type=ty.name):
                ctx = RecordingContext()
                narrow(ty, "values", ctx.tree, ctx)
                accessor = "values@" if ty.spec_view else "values"
                self.assertEqual(ctx.assumes[0].expression, f"{accessor}.len() == 0")

    def test_four_round_budget_reaches_element_refinements_not_length_guesses(self):
        ty = TypeInfo(
            TypeKind.STRUCT, "Buffer",
            fields=[FieldInfo("words", array_type())],
        )
        spec = DetCheckSpec(
            function="example", det_check_template="",
            symbols=[Symbol("before", ty, "input")],
        )
        schemas = enumerate_schemas(spec)
        z3_ctx = z3.Context()
        context = SchemaCtx(
            z3_ctx=z3_ctx, solver=z3.Solver(ctx=z3_ctx), schemas=schemas,
            guard_consts={
                schema.guard_name: z3.Bool(schema.guard_name, ctx=z3_ctx)
                for schema in schemas
            },
            k_consts={
                name: z3.Int(name, ctx=z3_ctx)
                for schema in schemas for name, _ in schema.k_params
            },
        )
        result = {
            "result": "unknown", "z3_raw": "unknown", "query_constraints": [],
            "z3_ms": 0.0, "reason_unknown": "scripted", "timeout_ms": 10, "seed": 0,
        }
        with patch(
            "specdet.adapters.verus.native.schema_search.search._check_constraints",
            return_value=result,
        ) as check:
            witness = run_schema_search(spec, context, max_rounds=4, timeout_ms=10)
        self.assertEqual(check.call_count, 4)
        self.assertEqual(witness.r0_z3, "unknown")
        self.assertEqual(witness.assumes, [])
        self.assertTrue(witness.search_exhausted)
        for entry in witness.trace[1:]:
            self.assertIn("before.words[0]", entry["new_assume"])
            self.assertNotIn(".len()", entry["new_assume"])
            self.assertEqual(entry["result"], "unknown")
        length_guards = {
            schema.guard_name for schema in schemas
            if schema.kind in {SchemaKind.SEQ_LEN_EQ, SchemaKind.SEQ_LEN_RANGE}
        }
        for call in check.call_args_list:
            constraints = {constraint.sexpr() for constraint in call.args[1]}
            for name in length_guards:
                self.assertIn(z3.Not(context.guard_consts[name]).sexpr(), constraints)


if __name__ == "__main__":
    unittest.main()
