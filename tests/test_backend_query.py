from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import z3

from specdet.adapters.verus.backend import VerusBackend
from specdet.adapters.verus.execution import ProcessResult
from specdet.adapters.verus.native.schema_search.search import SchemaCtx
from specdet.adapters.verus.native.extract.narrow import AssumeNode
from specdet.adapters.verus.native.extract.predicates import EqPred
from specdet.adapters.verus.native.extract.types import Assume, Witness
from specdet.adapters.verus.native.schema_search import MissingSchemaBinding
from specdet.adapters.verus.native.schema_search.search import SchemaSearchContext
from specdet.config import Config, Limits
from specdet.domain.models import (
    AnalysisReport, CheckEvidence, SolverStatus, Stage, StageError, Verdict, digest, text_digest,
)
from specdet.domain.proposals import GenerationRequest, Proposal
from specdet.analysis.verdicts import classify
from specdet.storage.workspace import PreparedProject


class QueryExecutor:
    def __init__(self):
        self.calls = 0

    def verify(self, source, project_root, artifact_dir, function, module="", *, all_functions=False):
        self.calls += 1
        (artifact_dir / "logs").mkdir(parents=True)
        return ProcessResult(("mock-verifier",), 1, "0 verified, 1 errors", "postcondition not satisfied", 1)


class BackendQueryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / f"backend-query-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        source_root = self.root / "source"
        source_root.mkdir()
        source = "use vstd::prelude::*;\nverus! { pub fn f(x:bool)->(r:bool) ensures true, {x} }\nfn main() {}"
        (source_root / "f.rs").write_text(source)
        files = {"f.rs": text_digest(source)}
        self.project = PreparedProject(source_root, source_root, digest(files), files)
        self.backend = VerusBackend(Config(
            source_root, self.root / "out", limits=Limits(max_search_rounds=10),
        ))
        self.executor = QueryExecutor()
        self.backend._executor = self.executor
        self.target = self.backend.discover(self.project)[0][0]
        self.contract = self.backend.extract(self.project, self.target)
        observations = self.backend.observations(self.project, self.contract)
        self.obligation = self.backend.lower(self.project, self.contract, observations)
        candidate = self.backend.generate_proof(self.project, self.contract, self.obligation, "baseline")
        self.baseline = self.backend.check(self.project, self.obligation, candidate, self.root / "baseline", baseline=True)
        det, schemas = self.backend._schema_objects(self.obligation)
        context = z3.Context()
        solver = z3.Solver(ctx=context)
        symbols = {name: z3.Bool(name, ctx=context) for name in ("x", "r1", "r2")}
        solver.add(symbols["r1"] != symbols["r2"])
        guards = {schema.guard_name: z3.Bool(schema.guard_name, ctx=context) for schema in schemas}
        for schema in schemas:
            expression = symbols["r1"] != symbols["r2"] if schema.id == "neq_tuple" else (
                symbols[schema.rust_var] == schema.bool_value
            )
            solver.add(z3.Implies(guards[schema.guard_name], expression))
        self.ctx = SchemaCtx(
            context, solver, schemas, guards, {},
            target_function=f"f::{det.check_fn_name}", query_smt2=solver.sexpr(),
        )
        self.path = Path(self.baseline.native["logs"]) / "root.smt2"
        self.path.write_text(f";; Function-Def f::{det.check_fn_name}\n" + solver.sexpr())
        self.key = self.backend._baseline_key(self.obligation, self.baseline)
        self.bundle = {
            "ctx": self.ctx, "path": self.path, "file_digest": text_digest(self.path.read_text()),
            "query_digest": text_digest(self.ctx.query_smt2),
            "det": det, "schemas": schemas, "role": "originalC",
        }
        self.backend._contexts[self.key] = self.bundle

    def adopt_plan(self, constraints):
        context = self.backend.generation_context(
            Stage.SEARCH, self.project, self.target, self.contract, self.obligation, (),
        )
        request = GenerationRequest(
            Stage.SEARCH, "verus", ("search_plan",), self.contract.source_digest,
            self.obligation.problem_id, context, self.target.id,
        )
        proposal = Proposal(
            request.id, request.stage, "verus", "search_plan",
            request.source_digest, request.problem_id, {"constraints": constraints},
        )
        validation = self.backend.validate_proposal(
            request, proposal, self.project, self.target, self.contract, self.obligation,
        )
        self.assertEqual(validation.status, "accepted_for_check", validation.diagnostics)
        self.assertEqual(self.backend.adopt_proposal(
            proposal, self.project, self.target, self.contract, self.obligation,
        ), Stage.SEARCH)
        return proposal

    def test_baseline_disables_every_guard_and_is_immutable(self):
        with patch.object(self.ctx.solver, "check", wraps=self.ctx.solver.check) as checked:
            baseline = self.backend.query(self.obligation, self.baseline)
        self.assertEqual(baseline.status, SolverStatus.SAT)
        self.assertEqual(baseline.role, "baseline")
        self.assertEqual(
            {argument.sexpr() for argument in checked.call_args.args},
            {f"(not {guard.sexpr()})" for guard in self.ctx.guard_consts.values()},
        )
        result = self.backend.search(self.obligation, self.baseline, self.root / "search", max_rounds=4)
        self.assertLessEqual(result.rounds, 4)
        self.assertIs(self.backend.query(self.obligation, self.baseline), baseline)
        self.assertEqual(self.executor.calls, 1)
        self.assertTrue(result.confirmed_constraints)
        self.assertTrue(all("predicate" in item and "expression" in item for item in result.confirmed_constraints))

    def test_unknown_constraints_remain_candidates_not_witnesses(self):
        with patch(
            "specdet.adapters.verus.native.schema_search.search.SchemaSearchContext._check",
            return_value=("unknown", 0.01, "test incomplete quantifiers"),
        ):
            result = self.backend.search(self.obligation, self.baseline, self.root / "unknown", max_rounds=4)
        self.assertEqual(result.confirmed_constraints, ())
        self.assertTrue(result.candidate_constraints)
        self.assertTrue(all(item.status == SolverStatus.UNKNOWN for item in result.evidence))
        report = AnalysisReport(self.target, "run", self.obligation.problem_id, baseline=CheckEvidence(
            self.obligation.problem_id, SolverStatus.UNKNOWN, "baseline",
        ), search=result)
        self.assertEqual(classify(report), Verdict.INCONCLUSIVE)

    def test_adopted_plan_executes_an_actual_sat_slice_with_remaining_budget(self):
        self.adopt_plan([
            {"schema_id": "r1_is_true", "bindings": {}},
            {"variable": "r2", "predicate": {"kind": "bool", "value": False}},
        ])
        with patch.object(self.ctx.solver, "check", wraps=self.ctx.solver.check) as checked:
            result = self.backend.search(self.obligation, self.baseline, self.root / "plan", max_rounds=1)
        self.assertEqual(checked.call_count, 1)
        self.assertEqual(result.rounds, 1)
        self.assertEqual(result.evidence[0].status, SolverStatus.SAT)
        self.assertEqual(result.evidence[0].role, "refinement")
        self.assertEqual(len(result.confirmed_constraints), 2)
        self.assertTrue(result.exhausted)
        self.assertEqual(self.executor.calls, 1)
        report = AnalysisReport(self.target, "run", self.obligation.problem_id, baseline=CheckEvidence(
            self.obligation.problem_id, SolverStatus.UNKNOWN, "baseline",
        ), search=result)
        self.assertEqual(classify(report), Verdict.NONDETERMINISTIC)

    def test_unsat_slice_does_not_prove_global_determinism(self):
        self.adopt_plan([
            {"schema_id": "r1_is_true", "bindings": {}},
            {"schema_id": "r2_is_true", "bindings": {}},
        ])
        result = self.backend.search(self.obligation, self.baseline, self.root / "unsat", max_rounds=1)
        self.assertEqual(result.evidence[0].status, SolverStatus.UNSAT)
        self.assertEqual(result.confirmed_constraints, ())
        report = AnalysisReport(self.target, "run", self.obligation.problem_id, baseline=CheckEvidence(
            self.obligation.problem_id, SolverStatus.UNKNOWN, "baseline",
        ), search=result)
        self.assertEqual(classify(report), Verdict.INCONCLUSIVE)

    def test_zero_remaining_budget_does_not_restart_native_search(self):
        self.adopt_plan([{"schema_id": "r1_is_true", "bindings": {}}])
        with patch.object(self.ctx.solver, "check", wraps=self.ctx.solver.check) as checked:
            result = self.backend.search(self.obligation, self.baseline, self.root / "zero", max_rounds=0)
        self.assertEqual(checked.call_count, 0)
        self.assertEqual(result.rounds, 0)
        self.assertFalse(result.evidence)
        self.assertTrue(result.candidate_constraints)
        self.assertFalse(result.confirmed_constraints)

    def test_search_plan_cannot_inject_uncompiled_expressions(self):
        for constraint in (
            {"schema_id": "not_compiled", "bindings": {}},
            {"schema_id": "r1_is_true", "bindings": {"payload": "assume(false)"}},
            {"expression": "assume(false)"},
            {"variable": "r1", "predicate": {"kind": "expression", "text": "false"}},
        ):
            with self.assertRaises(ValueError):
                self.backend._plan_constraints(self.obligation, {"constraints": [constraint]})

    def test_unsupported_attempts_consume_budget_and_preserve_raw_records(self):
        context = SchemaSearchContext(self.bundle["det"], self.ctx, max_rounds=2)
        initial = context.initial_check()
        node = AssumeNode("x")
        context.tree.children["x"] = node
        with self.assertLogs(level="WARNING"):
            self.assertFalse(context.test_and_set(node, Assume.from_pred("x", EqPred("x", 1))))
        witness = Witness(
            self.bundle["det"].function, trace=context.trace, r0_z3=initial,
            diagnostics=context.diagnostics,
        )
        with patch(
            "specdet.adapters.verus.native.schema_search.search.run_schema_search",
            return_value=witness,
        ):
            result = self.backend.search(self.obligation, self.baseline, self.root / "unsupported", max_rounds=2)
        self.assertEqual(result.rounds, 2)
        self.assertTrue(result.exhausted)
        self.assertEqual(result.evidence[-1].status, SolverStatus.UNSUPPORTED)
        self.assertEqual(result.confirmed_constraints, ())
        self.assertTrue((self.root / "unsupported/checks/000001.json").is_file())

    def test_lost_native_context_is_fatal_not_a_budget_refund(self):
        with patch(
            "specdet.adapters.verus.native.schema_search.search.run_schema_search",
            side_effect=MissingSchemaBinding("missing fixed guard"),
        ):
            with self.assertRaises(StageError) as error:
                self.backend.search(self.obligation, self.baseline, self.root / "lost-context", max_rounds=2)
        self.assertEqual(error.exception.diagnostic.code, "native_search_error")
        self.assertFalse(error.exception.recoverable)
        self.assertTrue((self.root / "lost-context/failure.json").is_file())


if __name__ == "__main__":
    unittest.main()
