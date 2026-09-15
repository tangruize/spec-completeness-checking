"""Native search evidence, budgets and scoped transcript regressions."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import z3

from specdet.adapters.verus.native.extract.predicates import (
    BoolPred, NotEqualFnPred, SetContainsPred, SetLiteralPred,
)
from specdet.adapters.verus.native.extract.types import (
    Assume, DetCheckSpec, Symbol, TypeInfo, TypeKind,
)
from specdet.adapters.verus.native.schema_search.schemas import (
    SchemaBinding, SchemaKind, enumerate_schemas, render_guarded_template,
)
from specdet.adapters.verus.native.schema_search.search import (
    MissingSchemaBinding, SchemaCtx, SchemaSearchContext, UnsupportedTranscript,
    UnsupportedPredicate, build_schema_ctx, run_schema_search,
)


class ScriptedSolver:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.checks = []
        self.settings = []

    def set(self, **settings):
        self.settings.append(settings)

    def check(self, *constraints):
        self.checks.append(list(constraints))
        return next(self.statuses)

    def reason_unknown(self):
        return "scripted undecided query"


def det_spec(*names, kind=TypeKind.BOOL):
    return DetCheckSpec(
        function="demo", det_check_template="",
        symbols=[Symbol(name, TypeInfo(kind, kind.value), "input") for name in names],
    )


def scripted_context(spec, statuses, schemas=None):
    schemas = enumerate_schemas(spec) if schemas is None else schemas
    context = z3.Context()
    return SchemaCtx(
        z3_ctx=context,
        solver=ScriptedSolver(statuses),
        schemas=schemas,
        guard_consts={
            schema.guard_name: z3.Bool(schema.guard_name + "!", ctx=context)
            for schema in schemas
        },
        k_consts={
            name: z3.Int(name + "!", ctx=context)
            for schema in schemas for name, _ in schema.k_params
        },
    )


class SearchEvidenceTests(unittest.TestCase):
    def test_unknown_retains_candidates_not_witness_assumes(self):
        spec = det_spec("x", "y")
        context = scripted_context(spec, [z3.unknown, z3.unknown, z3.unknown])
        witness = run_schema_search(spec, context)
        self.assertEqual(witness.r0_z3, "unknown")
        self.assertEqual(witness.assumes, [])
        self.assertIsNone(witness.last_sat_round)
        self.assertEqual(
            [assume.expression for assume in witness.candidate_assumes],
            ["x == true", "y == true"],
        )
        self.assertTrue(all(entry["z3_raw"] == "unknown" for entry in witness.trace))

    def test_only_last_satisfiable_snapshot_is_a_witness(self):
        spec = det_spec("x", "y")
        context = scripted_context(spec, [z3.sat, z3.sat, z3.unknown])
        witness = run_schema_search(spec, context)
        self.assertEqual(witness.r0_z3, "sat")
        self.assertEqual([a.expression for a in witness.assumes], ["x == true"])
        self.assertEqual(
            [a.expression for a in witness.candidate_assumes],
            ["x == true", "y == true"],
        )
        self.assertEqual(witness.last_sat_round, 1)
        self.assertEqual(witness.trace[-1]["reason_unknown"], "scripted undecided query")

    def test_sat_slice_does_not_rewrite_unknown_baseline(self):
        spec = det_spec("x")
        witness = run_schema_search(spec, scripted_context(spec, [z3.unknown, z3.sat]))
        self.assertEqual(witness.r0_z3, "unknown")
        self.assertEqual([a.expression for a in witness.assumes], ["x == true"])
        self.assertEqual(witness.candidate_assumes, [])

    def test_unsat_refinements_do_not_prove_global_goal(self):
        spec = det_spec("x")
        witness = run_schema_search(
            spec, scripted_context(spec, [z3.unknown, z3.unsat, z3.unsat])
        )
        self.assertEqual(witness.r0_z3, "unknown")
        self.assertEqual(witness.assumes, [])
        self.assertEqual(witness.candidate_assumes, [])
        self.assertEqual([entry["scope"] for entry in witness.trace], ["baseline", "slice", "slice"])

    def test_unsat_only_rejects_the_current_slice(self):
        spec = det_spec("x")
        witness = run_schema_search(spec, scripted_context(spec, [z3.sat, z3.unsat, z3.sat]))
        self.assertEqual(witness.r0_z3, "sat")
        self.assertEqual([a.expression for a in witness.assumes], ["x == false"])

    def test_unsat_baseline_stops_without_narrowing(self):
        spec = det_spec("x")
        context = scripted_context(spec, [z3.unsat])
        witness = run_schema_search(spec, context)
        self.assertEqual(witness.r0_z3, "unsat")
        self.assertEqual(len(context.solver.checks), 1)
        self.assertEqual(witness.assumes, [])
        self.assertFalse(witness.search_exhausted)

    def test_all_guards_are_explicit_and_query_settings_are_per_query(self):
        spec = det_spec("x")
        context = scripted_context(spec, [z3.sat, z3.sat])
        witness = run_schema_search(spec, context, timeout_ms=73, seed=91)
        baseline, refinement = context.solver.checks
        self.assertEqual(
            {expression.sexpr() for expression in baseline},
            {z3.Not(guard).sexpr() for guard in context.guard_consts.values()},
        )
        active = next(schema for schema in context.schemas if schema.bool_value is True)
        self.assertEqual(
            {expression.sexpr() for expression in refinement},
            {
                (guard if name == active.guard_name else z3.Not(guard)).sexpr()
                for name, guard in context.guard_consts.items()
            },
        )
        self.assertEqual(context.solver.settings, [{"timeout": 73, "random_seed": 91}] * 2)
        self.assertEqual(witness.trace[0]["query_constraints"], [c.sexpr() for c in baseline])

    def test_total_budget_includes_baseline_and_stops_recursive_search(self):
        spec = det_spec("x", kind=TypeKind.INT)
        context = scripted_context(spec, [z3.sat] * 3)
        witness = run_schema_search(spec, context, max_rounds=3)
        self.assertEqual(len(context.solver.checks), 3)
        self.assertEqual(len(witness.trace), 3)
        self.assertTrue(witness.search_exhausted)
        self.assertEqual(witness.last_sat_round, 2)
        self.assertTrue(any("budget" in diagnostic for diagnostic in witness.diagnostics))

    def test_zero_budget_never_calls_solver(self):
        spec = det_spec("x")
        context = scripted_context(spec, [])
        witness = run_schema_search(spec, context, max_rounds=0)
        self.assertEqual(witness.r0_z3, "unknown")
        self.assertEqual(context.solver.checks, [])
        self.assertEqual(witness.trace, [])
        self.assertTrue(witness.search_exhausted)
        self.assertEqual(witness.assumes, [])

    def test_unsupported_predicates_are_not_proof_passes(self):
        spec = det_spec()
        context = scripted_context(spec, [z3.sat])
        search = SchemaSearchContext(spec, context)
        search.initial_check()
        candidate = Assume.from_pred("s", SetLiteralPred("s", "int", (1, 2)))
        with self.assertLogs("specdet.adapters.verus.native.schema_search.search", level="WARNING"):
            accepted = search.test_and_set(search.tree.get_or_create("s"), candidate)
        self.assertFalse(accepted)
        self.assertEqual(len(context.solver.checks), 1)
        self.assertEqual(search.trace[-1]["result"], "unsupported")
        self.assertIsNone(search.trace[-1]["z3_raw"])
        self.assertIsNone(search.trace[-1]["query_constraints"])
        self.assertEqual(search.tree.collect_assumes(), [])

    def test_repeated_schema_parameters_are_unsupported_not_artificial_unsat(self):
        schema = SchemaBinding(
            "contains", SchemaKind.SET_CONTAINS, "s", "s.contains({k})",
            "g_contains", [("k_contains", "int")],
        )
        spec = det_spec()
        context = scripted_context(spec, [z3.sat, z3.sat], [schema])
        search = SchemaSearchContext(spec, context)
        search.initial_check()
        first = Assume.from_pred("s", SetContainsPred("s", 1))
        self.assertTrue(search.test_and_set(search.tree.get_or_create("first"), first))
        with self.assertLogs("specdet.adapters.verus.native.schema_search.search", level="WARNING"):
            accepted = search.test_and_set(
                search.tree.get_or_create("second"),
                Assume.from_pred("s", SetContainsPred("s", 2)),
            )
        self.assertFalse(accepted)
        self.assertEqual(search.trace[-1]["result"], "unsupported")
        self.assertEqual(search.confirmed_assumes, [first])
        self.assertEqual(len(context.solver.checks), 2)

    def test_arbitrary_distinctness_call_cannot_change_the_fixed_goal(self):
        spec = det_spec()
        spec.equal_fn_name = "det_demo_equal"
        context = scripted_context(spec, [z3.sat])
        search = SchemaSearchContext(spec, context)
        search.initial_check()
        with self.assertLogs("specdet.adapters.verus.native.schema_search.search", level="WARNING"):
            accepted = search.test_and_set(
                search.tree.get_or_create("not_equal"),
                Assume.from_pred("tuple", NotEqualFnPred("some_other_claim()")),
            )
        self.assertFalse(accepted)
        self.assertEqual(search.trace[-1]["result"], "unsupported")

    def test_missing_bindings_fail_even_for_manually_created_context(self):
        spec = det_spec("x")
        context = scripted_context(spec, [])
        context.guard_consts.clear()
        with self.assertRaises(MissingSchemaBinding):
            run_schema_search(spec, context)

    def test_schema_cannot_omit_its_required_parameter_definition(self):
        spec = det_spec()
        schema = SchemaBinding("eq", SchemaKind.SCALAR_EQ, "x", "x == {k}", "g_eq")
        context = scripted_context(spec, [], [schema])
        with self.assertRaisesRegex(MissingSchemaBinding, "requires 1"):
            run_schema_search(spec, context)

    def test_real_z3_satisfiable_witness(self):
        spec = det_spec("x", "y")
        context = scripted_context(spec, [])
        solver = z3.Solver(ctx=context.z3_ctx)
        x = z3.Bool("x", ctx=context.z3_ctx)
        y = z3.Bool("y", ctx=context.z3_ctx)
        solver.add(x != y)
        for schema in context.schemas:
            if schema.kind == SchemaKind.BOOL_EQ:
                solver.add(z3.Implies(
                    context.guard_consts[schema.guard_name],
                    {"x": x, "y": y}[schema.rust_var] == schema.bool_value,
                ))
        context.solver = solver
        witness = run_schema_search(spec, context)
        self.assertEqual(witness.r0_z3, "sat")
        self.assertEqual([a.expression for a in witness.assumes], ["x == true", "y == false"])
        self.assertEqual(witness.candidate_assumes, [])

    def test_proof_assertions_are_not_added_to_the_search_goal(self):
        spec = det_spec()
        with self.assertRaisesRegex(ValueError, "separate artifact"):
            render_guarded_template(spec, [], proof_prelude="assert(false);")

    def test_numeric_parameter_sort_must_match_its_native_type(self):
        spec = det_spec("x", kind=TypeKind.INT)
        context = scripted_context(spec, [])
        name = next(iter(context.k_consts))
        context.k_consts[name] = z3.Bool(name, ctx=context.z3_ctx)
        with self.assertRaisesRegex(MissingSchemaBinding, "SMT integer"):
            run_schema_search(spec, context)


class ExplicitSchemaQueryTests(unittest.TestCase):
    def test_explicit_baseline_and_refinement_each_check_exactly_once(self):
        spec = det_spec("x")
        context = scripted_context(spec, [z3.sat, z3.unknown])
        baseline = context.check(timeout_ms=31, seed=7)
        schema = next(item for item in context.schemas if item.bool_value is True)
        refined = context.check({schema.id: {}}, timeout_ms=31, seed=7)
        self.assertEqual(len(context.solver.checks), 2)
        self.assertEqual(baseline["z3_raw"], "sat")
        self.assertEqual(baseline["scope"], "baseline")
        self.assertEqual(
            set(baseline["query_constraints"]),
            {z3.Not(guard).sexpr() for guard in context.guard_consts.values()},
        )
        self.assertEqual(refined["z3_raw"], "unknown")
        self.assertEqual(refined["scope"], "slice")
        self.assertEqual(refined["schema_bindings"], {schema.id: {}})
        self.assertEqual(refined["reason_unknown"], "scripted undecided query")

    def test_numeric_proposal_binds_only_declared_parameters(self):
        spec = det_spec("x", kind=TypeKind.INT)
        context = scripted_context(spec, [z3.sat])
        schema = next(item for item in context.schemas if item.kind == SchemaKind.SCALAR_EQ)
        parameter = schema.k_params[0][0]
        query = context.check({schema.id: {parameter: 7}})
        self.assertIn((context.k_consts[parameter] == 7).sexpr(), query["query_constraints"])
        self.assertIn(context.guard_consts[schema.guard_name].sexpr(), query["query_constraints"])
        for item in context.schemas:
            if item is not schema:
                self.assertIn(z3.Not(context.guard_consts[item.guard_name]).sexpr(), query["query_constraints"])

    def test_invalid_proposals_never_call_the_solver(self):
        spec = det_spec("x", kind=TypeKind.INT)
        context = scripted_context(spec, [])
        schema = next(item for item in context.schemas if item.kind == SchemaKind.SCALAR_EQ)
        parameter = schema.k_params[0][0]
        for bindings in [
            {"invented_schema": {}},
            {schema.id: {}},
            {schema.id: {parameter: "(assert false)"}},
            {schema.id: {parameter: True}},
            {schema.id: {parameter: 0, "extra": 1}},
        ]:
            with self.subTest(bindings=bindings), self.assertRaises(UnsupportedPredicate):
                context.check(bindings)
        self.assertEqual(context.solver.checks, [])

    def test_boolean_parameter_proposals_require_boolean_values(self):
        schema = SchemaBinding(
            "member", SchemaKind.SET_CONTAINS, "s", "s.contains({k})",
            "g_member", [("k_member", "bool")],
        )
        context = scripted_context(det_spec(), [z3.sat], [schema])
        context.k_consts["k_member"] = z3.Bool("k_member!", ctx=context.z3_ctx)
        query = context.check({"member": {"k_member": True}})
        self.assertEqual(query["z3_raw"], "sat")
        with self.assertRaises(UnsupportedPredicate):
            context.check({"member": {"k_member": 1}})
        self.assertEqual(len(context.solver.checks), 1)

    def test_explicit_checks_reject_unbounded_timeout_or_invalid_seed(self):
        context = scripted_context(det_spec(), [])
        for kwargs in [{"timeout_ms": 0}, {"seed": -1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                context.check(**kwargs)
        self.assertEqual(context.solver.checks, [])


SCHEMA = SchemaBinding(
    "x_eq", SchemaKind.SCALAR_EQ, "x", "x == {k}", "g_x", [("k_x", "int")],
)


def transcript(*, same_outputs=False, after_query="", future=""):
    second = "r1!" if same_outputs else "1"
    return f"""
(set-option :auto_config false)
(declare-fun det_demo_equal (Int Int) Bool)
;; Function-Def crate::earlier
(push 1)
(assert false)
(check-sat)
(pop 1)
;; Function-Axioms crate::det_demo_equal
(assert (forall ((a Int) (b Int)) (= (det_demo_equal a b) (= a b))))
;; Function-Def crate::det_demo
(push)
(declare-const r1! Int)
(declare-const r2! Int)
(declare-const g_x! Bool)
(declare-const k_x! Int)
(assert (= r1! 0))
(assert (= r2! {second}))
(assert (=> g_x! (= r1! k_x!)))
;; postcondition not satisfied
(declare-const %%location_label%%0 Bool)
(assert (not (=> %%location_label%%0 (det_demo_equal r1! r2!))))
(check-sat)
{after_query}
(pop)
{future}
"""


def build_transcript(text, schemas=None):
    with patch.object(Path, "read_text", return_value=text):
        return build_schema_ctx(Path("in-memory.smt2"), "det_demo", [SCHEMA] if schemas is None else schemas, "crate")


class TranscriptTests(unittest.TestCase):
    def test_future_axioms_and_popped_earlier_queries_do_not_leak(self):
        context = build_transcript(transcript(after_query="(assert false)", future="(assert false)"))
        self.assertEqual(context.solver.check(z3.Not(context.guard_consts["g_x"])), z3.sat)
        self.assertNotIn("(assert false)", context.query_smt2)

    def test_source_equal_function_axioms_between_queries_are_preserved(self):
        context = build_transcript(transcript(same_outputs=True))
        self.assertIn("(forall ((a Int) (b Int))", context.query_smt2)
        self.assertEqual(context.solver.check(z3.Not(context.guard_consts["g_x"])), z3.unsat)

    def test_missing_guard_is_not_silently_accepted(self):
        with self.assertRaisesRegex(MissingSchemaBinding, "g_x"):
            build_transcript(transcript().replace("(declare-const g_x! Bool)", ""))

    def test_missing_parameter_is_not_silently_accepted(self):
        with self.assertRaisesRegex(MissingSchemaBinding, "k_x"):
            build_transcript(transcript().replace("(declare-const k_x! Int)", ""))

    def test_future_parameter_declaration_cannot_supply_a_missing_binding(self):
        text = transcript(future="(declare-const k_x! Int)").replace(
            "(declare-const k_x! Int)", "", 1,
        )
        with self.assertRaisesRegex(MissingSchemaBinding, "k_x"):
            build_transcript(text)

    def test_multiple_target_vcs_are_rejected(self):
        target = transcript().split(";; Function-Def crate::det_demo\n", 1)[1]
        text = transcript() + ";; Function-Def crate::det_demo\n" + target
        with self.assertRaisesRegex(UnsupportedTranscript, "Multiple target VCs"):
            build_transcript(text)

    def test_different_check_constraints_are_not_guessed_to_be_diagnostic(self):
        with self.assertRaisesRegex(UnsupportedTranscript, "Multiple target VCs"):
            build_transcript(transcript(after_query="(assert false)\n(check-sat)"))

    def test_recognized_diagnostic_recheck_keeps_original_goal(self):
        context = build_transcript(transcript(
            after_query="(assert (not %%location_label%%0))\n(check-sat)",
        ))
        self.assertEqual(context.solver.check(z3.Not(context.guard_consts["g_x"])), z3.sat)
        self.assertNotIn("(assert (not %%location_label%%0))", context.query_smt2)
        self.assertEqual(len(context.diagnostics), 1)

    def test_arbitrary_assertion_failure_is_not_a_determinism_query(self):
        with self.assertRaisesRegex(UnsupportedTranscript, "postcondition VC"):
            build_transcript(transcript().replace("postcondition not satisfied", "assertion failed"))

    def test_multiple_assertion_labels_are_unsupported_even_in_one_check(self):
        text = transcript().replace(
            ";; postcondition not satisfied",
            ";; assertion failed\n(declare-const %%location_label%%1 Bool)\n;; postcondition not satisfied",
        )
        with self.assertRaisesRegex(UnsupportedTranscript, "postcondition VC"):
            build_transcript(text)

    def test_unrelated_goal_cannot_use_the_determinism_verdict(self):
        text = transcript().replace(
            "(=> %%location_label%%0 (det_demo_equal r1! r2!))",
            "(=> %%location_label%%0 false)",
        )
        with self.assertRaisesRegex(UnsupportedTranscript, "generated det_demo_equal"):
            build_transcript(text)

    def test_quoted_parentheses_and_comments_do_not_change_scope(self):
        text = transcript().replace(
            ";; Function-Axioms",
            '(declare-const |name(with;punctuation)| Int)\n'
            '(echo "text (push) ;; Function-Def crate::det_demo")\n'
            ";; Function-Axioms",
        )
        context = build_transcript(text)
        self.assertEqual(context.solver.check(z3.Not(context.guard_consts["g_x"])), z3.sat)

    def test_empty_schema_list_does_not_break_constant_resolution(self):
        context = build_transcript(transcript(), schemas=[])
        self.assertEqual(context.guard_consts, {})
        self.assertEqual(context.k_consts, {})

    def test_target_is_selected_by_crate_not_only_bare_function_name(self):
        foreign = """
;; Function-Def foreign::det_demo
(push)
(assert false)
(check-sat)
(pop)
"""
        context = build_transcript(foreign + transcript())
        self.assertEqual(context.target_function, "crate::det_demo")
        self.assertEqual(context.solver.check(z3.Not(context.guard_consts["g_x"])), z3.sat)


class SolverOptionIsolationTests(unittest.TestCase):
    GLOBAL_KEYS = (
        "auto_config", "smt.mbqi", "rlimit", "timeout", "smt.case_split",
        "smt.qi.eager_threshold", "smt.delay_units", "smt.arith.solver",
        "smt.arith.nl", "pi.enabled", "rewriter.sort_disjunctions",
    )
    VERUS_OPTIONS = """
(set-option :auto_config false)
(set-option :smt.mbqi false)
(set-option :smt.case_split 3)
(set-option :smt.qi.eager_threshold 100.0)
(set-option :smt.delay_units true)
(set-option :smt.arith.solver 2)
(set-option :smt.arith.nl false)
(set-option :pi.enabled false)
(set-option :rewriter.sort_disjunctions false)
(set-option :rlimit 1)
"""

    def global_options(self):
        return {key: z3.get_param(key) for key in self.GLOBAL_KEYS}

    def constrained_transcript(self):
        return self.VERUS_OPTIONS + transcript().replace(
            "(forall ((a Int) (b Int)) (= (det_demo_equal a b) (= a b)))",
            "(forall ((a Int) (b Int)) (! (= (det_demo_equal a b) (= a b)) "
            ":pattern ((det_demo_equal a b))))",
        )

    def test_verus_options_do_not_leak_to_existing_or_future_solvers(self):
        before_options = self.global_options()
        existing = build_transcript(transcript(same_outputs=True))
        limited = build_transcript(self.constrained_transcript())
        future = build_transcript(transcript())

        self.assertEqual(self.global_options(), before_options)
        self.assertIn("(set-option :smt.mbqi false)", limited.query_smt2)
        self.assertIn("(set-option :rlimit 1)", limited.query_smt2)
        self.assertEqual(
            limited.solver.check(z3.Not(limited.guard_consts["g_x"])), z3.unknown,
        )
        self.assertIn(
            limited.solver.reason_unknown(), {"canceled", "max. resource limit exceeded"},
        )
        self.assertEqual(
            existing.solver.check(z3.Not(existing.guard_consts["g_x"])), z3.unsat,
        )
        self.assertEqual(
            future.solver.check(z3.Not(future.guard_consts["g_x"])), z3.sat,
        )
        self.assertEqual(self.global_options(), before_options)

    def test_invalid_transcript_does_not_leave_changed_global_options(self):
        before_options = self.global_options()
        invalid = self.constrained_transcript().replace(
            "(assert (= r1! 0))", "(assert undeclared_symbol)",
        )
        with self.assertRaises(UnsupportedTranscript):
            build_transcript(invalid)
        self.assertEqual(self.global_options(), before_options)

    def test_unknown_solver_option_is_rejected_not_ignored_or_globalized(self):
        before_options = self.global_options()
        with self.assertRaisesRegex(UnsupportedTranscript, "solver-local equivalent"):
            build_transcript("(set-option :not_a_solver_option true)\n" + transcript())
        self.assertEqual(self.global_options(), before_options)

    def test_unisolatable_pattern_inference_is_explicitly_unsupported(self):
        before_options = self.global_options()
        different = "false" if before_options["pi.enabled"] == "true" else "true"
        with self.assertRaisesRegex(UnsupportedTranscript, "unannotated quantifier"):
            build_transcript(f"(set-option :pi.enabled {different})\n" + transcript())
        self.assertEqual(self.global_options(), before_options)


if __name__ == "__main__":
    unittest.main()
