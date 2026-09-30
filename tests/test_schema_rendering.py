from __future__ import annotations

import unittest

from specdet.adapters.verus.native.schema_search.schemas import (
    SchemaBinding, SchemaKind, enumerate_schemas, render_schema_expression,
)
from specdet.adapters.verus.native.codegen.equal_policy import EqualPolicy
from specdet.adapters.verus.native.codegen.gen_det import build_det_check_spec
from specdet.adapters.verus.native.extract.extractor import extract_spec


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

    def test_ignored_output_errors_do_not_generate_payload_refinements(self):
        source = "verus! { fn f(x: Result<u8,u8>) -> (r: Result<u8,u8>) ensures true, { x } }"
        function = extract_spec(source, "f")
        for ignored in (True, False):
            with self.subTest(ignored=ignored):
                det = build_det_check_spec(function, equal_policy=EqualPolicy(errs_equivalent=ignored))
                before = det.to_json()
                schemas = enumerate_schemas(det)
                self.assertEqual(det.to_json(), before)
                self.assertTrue(any(schema.rust_var == "x->Err_0" for schema in schemas))
                self.assertEqual(
                    any(schema.rust_var == "r1->Err_0" for schema in schemas), not ignored,
                )
                self.assertTrue(any(schema.rust_var == "r1" and schema.variant == "Err" for schema in schemas))


if __name__ == "__main__":
    unittest.main()
