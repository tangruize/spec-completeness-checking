from __future__ import annotations

import unittest

from specdet.adapters.verus.native.schema_search.schemas import (
    SchemaBinding, SchemaKind, render_schema_expression,
)


class SchemaRenderingTests(unittest.TestCase):
    def test_compiled_parameter_names_and_candidate_values_share_mapping(self):
        schema = SchemaBinding(
            "len", SchemaKind.SEQ_LEN_EQ, "r1@", "r1@.len() == {k}", "g_len",
            k_params=[("k_r1_view_len", "int")],
        )
        self.assertEqual(render_schema_expression(schema), "r1@.len() == k_r1_view_len")
        self.assertEqual(
            render_schema_expression(schema, bindings={"k_r1_view_len": 4}),
            "r1@.len() == 4",
        )

    def test_range_aliases_are_not_python_format_keys(self):
        schema = SchemaBinding(
            "range", SchemaKind.SCALAR_RANGE, "x", "{ x >= {k_lo} && x <= {k_hi} }", "g_range",
            k_params=[("bound_low", "int"), ("bound_high", "int")],
        )
        self.assertEqual(
            render_schema_expression(schema, bindings={"bound_low": -2, "bound_high": 8}),
            "{ x >= -2 && x <= 8 }",
        )
        with self.assertRaises(ValueError):
            render_schema_expression(schema, bindings={"k_lo": 1, "k_hi": 2})

    def test_distinctness_uses_only_the_frozen_equal_call(self):
        schema = SchemaBinding("neq", SchemaKind.NOT_EQUAL_FN, "__tuple__", "!{equal_fn_call}", "g_neq")
        self.assertEqual(render_schema_expression(schema, "goal_equal(r1, r2)", bindings={}), "!goal_equal(r1, r2)")


if __name__ == "__main__":
    unittest.main()
