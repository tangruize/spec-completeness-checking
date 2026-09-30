"""One compiled, fixed determinism query with bounded Z3-only refinement."""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import z3

from specdet.runtime import RunDeadlineExceeded, bounded_timeout_ms, check_deadline

from ..extract.narrow import AssumeNode, _add_distinctness_witnesses, narrow
from ..extract.predicates import NotEqualFnPred
from ..extract.types import Assume, DetCheckSpec, Witness
from .schemas import SchemaBinding, SchemaKind, translate_assume
from .transcript import (
    UnsupportedTranscript, apply_solver_options, declaration_name, select_query,
    validate_fixed_goal, validate_pattern_inference,
)

logger = logging.getLogger(__name__)


class MissingSchemaBinding(UnsupportedTranscript):
    """A required guard or schema parameter is absent from the selected VC."""


class UnsupportedPredicate(ValueError):
    """A candidate cannot be represented by the compiled schema parameters."""


class _SearchBudgetExhausted(Exception):
    pass


def _validate_query_settings(timeout_ms: int, seed: int) -> None:
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
        raise ValueError("timeout_ms must be a positive integer")
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2**32:
        raise ValueError("seed must be an unsigned 32-bit integer")


def _check_constraints(
    solver: z3.Solver,
    constraints: list[z3.BoolRef],
    *,
    timeout_ms: int,
    seed: int,
) -> dict:
    _validate_query_settings(timeout_ms, seed)
    started = time.monotonic()
    effective_timeout = None
    try:
        effective_timeout = bounded_timeout_ms(timeout_ms)
        solver.set(timeout=effective_timeout, random_seed=seed)
        result = solver.check(*constraints)
    except RunDeadlineExceeded:
        return {
            "result": "interrupted", "z3_raw": None,
            "query_constraints": [constraint.sexpr() for constraint in constraints],
            "z3_ms": (time.monotonic() - started) * 1000,
            "reason_unknown": "Whole-analysis budget exhausted before the solver returned a result",
            "timeout_ms": effective_timeout, "seed": seed,
        }
    elapsed = (time.monotonic() - started) * 1000
    status = str(result)
    if status not in {"sat", "unsat", "unknown"}:
        raise RuntimeError(f"Unexpected Z3 status: {status}")
    return {
        "result": status,
        "z3_raw": status,
        "query_constraints": [constraint.sexpr() for constraint in constraints],
        "z3_ms": elapsed,
        "reason_unknown": solver.reason_unknown() if status == "unknown" else "",
        "timeout_ms": effective_timeout,
        "seed": seed,
    }


@dataclass
class SchemaCtx:
    """Loaded, fixed VC and the complete schema constant bindings."""
    z3_ctx: z3.Context
    solver: z3.Solver
    schemas: list[SchemaBinding]
    guard_consts: dict[str, z3.BoolRef]
    k_consts: dict[str, z3.ExprRef]
    diagnostics: list[str] = field(default_factory=list)
    target_function: str = ""
    query_smt2: str = ""

    def constraints(
        self,
        active_bindings: Mapping[str, Mapping[str, int | bool]] | None = None,
    ) -> list[z3.BoolRef]:
        """Instantiate only declared schema IDs/parameters, disabling all others."""
        _validate_bindings(self)
        active = {} if active_bindings is None else active_bindings
        if not isinstance(active, Mapping):
            raise UnsupportedPredicate("Schema activations must be a mapping")
        known_ids = {schema.id for schema in self.schemas}
        unknown = set(active) - known_ids
        if unknown:
            raise UnsupportedPredicate(f"Unknown schema IDs: {sorted(unknown, key=str)}")
        constraints: list[z3.BoolRef] = []
        for schema in self.schemas:
            guard = self.guard_consts[schema.guard_name]
            if schema.id not in active:
                constraints.append(z3.Not(guard))
                continue
            bindings = active[schema.id]
            if not isinstance(bindings, Mapping):
                raise UnsupportedPredicate(f"Parameter bindings for {schema.id} must be a mapping")
            required = {name for name, _ in schema.k_params}
            if set(bindings) != required:
                raise UnsupportedPredicate(f"Incomplete parameter bindings for schema {schema.id}")
            constraints.append(guard)
            for name, value in bindings.items():
                constant = self.k_consts[name]
                if z3.is_int(constant):
                    valid = isinstance(value, int) and not isinstance(value, bool)
                elif z3.is_bool(constant):
                    valid = isinstance(value, bool)
                else:
                    valid = False
                if not valid:
                    raise UnsupportedPredicate(f"Unsupported parameter binding for {name}: {value!r}")
                constraints.append(constant == value)
        return constraints

    def check(
        self,
        active_bindings: Mapping[str, Mapping[str, int | bool]] | None = None,
        *,
        timeout_ms: int = 10000,
        seed: int = 0,
    ) -> dict:
        """Run exactly one fixed-goal query, without caching or replacing R0.

        Empty activations query the all-disabled baseline. The caller owns
        stage/round budgets and persistence when using this lower-level API.
        """
        constraints = self.constraints(active_bindings)
        result = _check_constraints(
            self.solver, constraints, timeout_ms=timeout_ms, seed=seed,
        )
        result["scope"] = "slice" if active_bindings else "baseline"
        result["schema_bindings"] = {
            schema_id: dict(bindings)
            for schema_id, bindings in (active_bindings or {}).items()
        }
        return result


def _required_names(schemas: list[SchemaBinding]) -> tuple[list[str], list[str]]:
    single_parameter = {
        SchemaKind.SCALAR_EQ, SchemaKind.ENUM_DISC_EQ, SchemaKind.SET_LEN_EQ,
        SchemaKind.SET_CONTAINS, SchemaKind.SEQ_LEN_EQ,
    }
    range_parameters = {
        SchemaKind.SCALAR_RANGE, SchemaKind.SET_LEN_RANGE, SchemaKind.SEQ_LEN_RANGE,
    }
    for schema in schemas:
        if not isinstance(schema.kind, SchemaKind):
            raise MissingSchemaBinding(f"Unsupported schema kind for {schema.id}: {schema.kind!r}")
        required_count = 2 if schema.kind in range_parameters else int(schema.kind in single_parameter)
        if len(schema.k_params) != required_count:
            raise MissingSchemaBinding(
                f"Schema {schema.id} requires {required_count} parameter bindings"
            )
    ids = [schema.id for schema in schemas]
    guards = [schema.guard_name for schema in schemas]
    params = [name for schema in schemas for name, _ in schema.k_params]
    for label, names in (("schema IDs", ids), ("guards", guards), ("parameters", params)):
        if len(names) != len(set(names)):
            raise MissingSchemaBinding(f"Duplicate {label} in schema definitions")
    if set(guards) & set(params):
        raise MissingSchemaBinding("Guard and parameter names overlap")
    return guards, params


def _validate_bindings(context: SchemaCtx) -> None:
    guards, params = _required_names(context.schemas)
    missing = set(guards) - context.guard_consts.keys()
    missing.update(set(params) - context.k_consts.keys())
    if missing:
        raise MissingSchemaBinding(f"Missing required schema bindings: {', '.join(sorted(missing))}")
    for name in guards:
        if not z3.is_bool(context.guard_consts[name]):
            raise MissingSchemaBinding(f"Schema guard {name} is not Boolean")
    integer_types = {"int", "nat", "usize", "isize", "u8", "u16", "u32", "u64", "u128",
                     "i8", "i16", "i32", "i64", "i128"}
    for schema in context.schemas:
        for name, native_type in schema.k_params:
            if native_type in integer_types and not z3.is_int(context.k_consts[name]):
                raise MissingSchemaBinding(
                    f"Schema parameter {name} for {native_type} is not an SMT integer"
                )
            if native_type == "bool" and not z3.is_bool(context.k_consts[name]):
                raise MissingSchemaBinding(f"Schema parameter {name} for bool is not Boolean")


def build_schema_ctx(
    smt2_path: Path,
    fn_name: str,
    schemas: list[SchemaBinding],
    crate_name: str,
) -> SchemaCtx:
    """Load the target VC with only the declarations/axioms in scope before it.

    Missing constants and ambiguous/multiple target obligations are errors,
    never partially usable contexts. A solver diagnostic recheck which only
    disables the same sole postcondition label is safely distinguishable.
    """
    query = select_query(Path(smt2_path).read_text(), fn_name, crate_name)
    guards, params = _required_names(schemas)
    declared = {
        name for command in query.commands if (name := declaration_name(command))
    }
    names: list[str] = []
    for name in (*guards, *params):
        candidates = [candidate for candidate in (name + "!", name) if candidate in declared]
        if len(candidates) != 1:
            raise MissingSchemaBinding(
                f"Expected one binding for {name!r} in {query.function}; found {candidates}"
            )
        names.append(candidates[0])
    z3_ctx = z3.Context()
    solver = z3.Solver(ctx=z3_ctx)
    try:
        pattern_inference = apply_solver_options(query, solver)
        solver.from_string(query.parser_smt2)
        validate_pattern_inference(query, solver, pattern_inference)
        validate_fixed_goal(query, solver, fn_name)
        bindings: list[z3.ExprRef] = []
        if names:
            # A scoped tautology retrieves the already declared constants
            # without reparsing the (potentially large) source axiom prelude.
            references = " ".join(f"(= |{name}| |{name}|)" for name in names)
            solver.push()
            try:
                solver.from_string(f"(assert (and {references}))")
                bindings = [child.arg(0) for child in solver.assertions()[-1].children()]
            finally:
                solver.pop()
        if len(bindings) != len(names):
            raise MissingSchemaBinding("Z3 did not resolve every required schema constant")
    except z3.Z3Exception as exc:
        raise UnsupportedTranscript(f"Cannot parse the selected Verus VC: {exc}") from exc
    context = SchemaCtx(
        z3_ctx=z3_ctx,
        solver=solver,
        schemas=list(schemas),
        guard_consts=dict(zip(guards, bindings[:len(guards)])),
        k_consts=dict(zip(params, bindings[len(guards):])),
        diagnostics=list(query.diagnostics),
        target_function=query.function,
        query_smt2=query.smt2,
    )
    _validate_bindings(context)
    logger.info(
        "Loaded %s: %s assertions, %s guards, %s parameters",
        query.function, len(solver.assertions()), len(guards), len(params),
    )
    return context


class SchemaSearchContext:
    """Narrowing may retain UNKNOWN candidates, but only SAT updates evidence."""

    def __init__(
        self,
        det_spec: DetCheckSpec,
        schema_ctx: SchemaCtx,
        *,
        max_rounds: int = 500,
        timeout_ms: int = 10000,
        seed: int = 0,
    ):
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds < 0:
            raise ValueError("max_rounds must be a nonnegative integer")
        _validate_query_settings(timeout_ms, seed)
        _validate_bindings(schema_ctx)
        self.det_spec = det_spec
        self.a = schema_ctx
        self.tree = AssumeNode(key="root")
        self.trace: list[dict] = []
        self.diagnostics = list(schema_ctx.diagnostics)
        self._round = 0
        self._used_rounds = 0
        self._schema_by_id = {schema.id: schema for schema in schema_ctx.schemas}
        self.max_rounds = max_rounds
        self.timeout_ms = timeout_ms
        self.seed = seed
        self.check_time_ms = 0.0
        self.confirmed_assumes: list[Assume] = []
        self.last_sat_round: int | None = None
        self.search_exhausted = False

    def _consume_round(self) -> int:
        check_deadline()
        if self._used_rounds >= self.max_rounds:
            self.search_exhausted = True
            raise _SearchBudgetExhausted
        current = self._used_rounds
        self._used_rounds += 1
        self._round = current
        return current

    def _assumes_to_z3(self, assumes: list[Assume]) -> list[z3.BoolRef]:
        active: dict[str, dict[str, int]] = {}
        for assume in assumes:
            if isinstance(assume.pred, NotEqualFnPred):
                args = [
                    name for pair in self.det_spec.equal_arg_pairs
                    for name in (pair["lhs"], pair["rhs"])
                ]
                expected = f"{self.det_spec.equal_fn_name}({', '.join(args)})"
                if re.sub(r"\s+", "", assume.pred.call) != re.sub(r"\s+", "", expected):
                    raise UnsupportedPredicate("Distinctness must use the fixed generated equality call")
            translated = translate_assume(assume, self.a.schemas, self.det_spec.equal_fn_name)
            if translated is None:
                raise UnsupportedPredicate(f"No compiled schema for {assume.expression}")
            schema_id, bindings = translated
            schema = self._schema_by_id[schema_id]
            required = {name for name, _ in schema.k_params}
            if set(bindings) != required:
                raise UnsupportedPredicate(f"Incomplete parameter bindings for schema {schema_id}")
            if schema_id in active and active[schema_id] != bindings:
                raise UnsupportedPredicate(
                    f"Schema {schema_id} cannot represent multiple simultaneous parameter values"
                )
            active[schema_id] = bindings
        return self.a.constraints(active)

    def _check(self, constraints: list[z3.BoolRef]) -> tuple[str, float, str]:
        result = _check_constraints(
            self.a.solver, constraints, timeout_ms=self.timeout_ms, seed=self.seed,
        )
        self.check_time_ms += result["z3_ms"]
        self.last_timeout_ms = result["timeout_ms"]
        return result["result"], result["z3_ms"], result["reason_unknown"]

    def initial_check(self) -> str:
        round_number = self._consume_round()
        constraints = self._assumes_to_z3([])
        status, elapsed, reason = self._check(constraints)
        self.trace.append({
            "round": round_number, "phase": "initial", "node_key": "root",
            "assumes": [], "new_assume": None,
            "query_constraints": [constraint.sexpr() for constraint in constraints],
            "result": status, "z3_raw": None if status == "interrupted" else status,
            "z3_ms": round(elapsed, 2), "reason_unknown": reason,
            "timeout_ms": self.last_timeout_ms, "seed": self.seed,
            "scope": "baseline", "description": "fixed global goal; all schema guards disabled",
        })
        if status == "interrupted":
            raise RunDeadlineExceeded
        if status == "sat":
            self.last_sat_round = round_number
        return status

    def test_and_set(self, node: AssumeNode, assume: Assume, phase: str = "") -> bool:
        round_number = self._consume_round()
        old_assume = node.assume
        node.assume = assume
        all_assumes = self.tree.collect_assumes()
        entry = {
            "round": round_number, "phase": phase or "search", "node_key": node.key,
            "assumes": [candidate.expression for candidate in all_assumes],
            "new_assume": assume.expression, "description": assume.description,
            "scope": "slice", "timeout_ms": self.timeout_ms, "seed": self.seed,
        }
        try:
            constraints = self._assumes_to_z3(all_assumes)
        except UnsupportedPredicate as exc:
            node.assume = old_assume
            message = str(exc)
            logger.warning("Unsupported narrowing predicate: %s", message)
            self.diagnostics.append(message)
            self.trace.append({
                **entry, "result": "unsupported", "z3_raw": None,
                "query_constraints": None, "diagnostic": message,
            })
            return False
        try:
            status, elapsed, reason = self._check(constraints)
        except Exception:
            node.assume = old_assume
            raise
        self.trace.append({
            **entry, "result": status, "z3_raw": None if status == "interrupted" else status,
            "query_constraints": [constraint.sexpr() for constraint in constraints],
            "z3_ms": round(elapsed, 2), "reason_unknown": reason,
            "timeout_ms": self.last_timeout_ms,
        })
        if status == "interrupted":
            node.assume = old_assume
            raise RunDeadlineExceeded
        logger.info("R%s [%s] %s: %s", round_number, phase or "search", assume.expression, status)
        if status == "sat":
            self.confirmed_assumes = list(all_assumes)
            self.last_sat_round = round_number
            return True
        if status == "unknown":
            return True
        node.assume = old_assume
        return False


def run_schema_search(
    det_spec: DetCheckSpec,
    schema_ctx: SchemaCtx,
    *,
    max_rounds: int = 500,
    timeout_ms: int = 10000,
    seed: int = 0,
) -> Witness:
    """Refine one VC without modifying its immutable baseline result.

    ``max_rounds`` bounds all attempted rounds, including R0 and unsupported
    candidate probes. With a zero budget no solver call occurs. UNSAT slices
    only reject those slices; UNKNOWN is exploratory, never witness evidence.
    """
    context = SchemaSearchContext(
        det_spec, schema_ctx, max_rounds=max_rounds, timeout_ms=timeout_ms, seed=seed,
    )
    r0_z3 = "unknown"
    try:
        r0_z3 = context.initial_check()
        if r0_z3 != "unsat":
            for symbol in det_spec.symbols:
                node = context.tree.get_or_create(symbol.name)
                narrow(symbol.type, symbol.name, node, context)
            _add_distinctness_witnesses(context, det_spec)
    except _SearchBudgetExhausted:
        context.diagnostics.append(
            f"Total round budget ({max_rounds}, including baseline) exhausted"
        )
    except RunDeadlineExceeded:
        if not context.trace or context.trace[0].get("result") == "interrupted":
            r0_z3 = None
        context.diagnostics.append("Whole-analysis wall-time budget exhausted; partial query trace retained")
    candidates = context.tree.collect_assumes()
    if candidates == context.confirmed_assumes:
        candidates = []
    return Witness(
        function=det_spec.function,
        assumes=list(context.confirmed_assumes),
        candidate_assumes=candidates,
        trace=context.trace,
        r0_z3=r0_z3,
        search_exhausted=context.search_exhausted,
        diagnostics=context.diagnostics,
        last_sat_round=context.last_sat_round,
    )
