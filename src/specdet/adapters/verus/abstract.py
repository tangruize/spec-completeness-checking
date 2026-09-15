"""Resolve source-backed input views without changing output observations."""
from __future__ import annotations

import re
from dataclasses import dataclass

from specdet.domain.models import JsonObject, as_object

from .native.codegen.gen_det import (
    _generic_l3_view_bounds_satisfied,
    _generic_view_equal,
)
from .native.codegen.pairing import InputPair, pair_identifiers
from .native.extract.types import FunctionSpec, TypeKind
from .native.view.registry import ViewRegistry
from .type_context import TypeContextError, parse_input_type


class InputViewError(ValueError):
    pass


class NoAbstractInputs(InputViewError):
    pass


@dataclass(frozen=True)
class InputPlan:
    pairs: tuple[InputPair, ...]
    observations: tuple[JsonObject, ...]
    definitions: tuple[JsonObject, ...]

    def to_dict(self) -> JsonObject:
        from specdet.domain.models import as_object

        return {
            "kind": "view_equivalence",
            "pairs": [as_object(pair) for pair in self.pairs],
            "inputs": list(self.observations),
            "definitions": list(self.definitions),
            "domain_preservation": "not_checked",
        }


def resolve_inputs(
    function: FunctionSpec, registry: ViewRegistry, requested: tuple[str, ...] = (),
) -> InputPlan:
    names = pair_identifiers(function)
    parameters = {parameter.name: parameter for parameter in function.params}
    selected = set(requested)
    if "self_" in selected and "self_" not in parameters and "self" in parameters:
        selected.remove("self_")
        selected.add("self")
    if missing := selected - set(parameters):
        raise InputViewError(f"Unknown abstract input parameter(s): {', '.join(sorted(missing))}")
    caller_generics = " ".join([function.generics_decl, function.where_decl])
    pairs: list[InputPair] = []
    observations: list[JsonObject] = []
    definitions: list[JsonObject] = []
    for parameter in function.params:
        name = parameter.name
        if selected and name not in selected:
            observations.append({"parameter": name, "mode": "shared", "reason": "not_selected"})
            continue
        left, right = names[name]
        ty = parameter.type
        try:
            expr = parse_input_type(ty, function)
        except TypeContextError as error:
            raise InputViewError(str(error)) from error
        resolution = registry.resolve(expr)
        relation: str | None = None
        layer = resolution.layer
        view_type = resolution.view_type_text
        declared = registry.scan.views.get(expr.head, [])
        distinct = {
            (entry.view_assoc_type, entry.view_fn_signature, entry.view_fn_body,
             str(entry.target_expr), entry.is_inherent)
            for entry in declared
        }
        if len(distinct) > 1:
            raise InputViewError(f"Ambiguous source View definitions for input {name}: {ty.name}")
        if layer in {"L3", "source-bound"}:
            if layer == "L3" and not _generic_l3_view_bounds_satisfied(ty.name, caller_generics, view_type):
                raise InputViewError(f"View bounds are not established for input {name}: {ty.name}")
            relation = f"{resolution.view_expr(left)} == {resolution.view_expr(right)}"
        elif ty.spec_view is not None:
            # TypeInfo marks Vec as a sequence; its source value still needs @.
            relation = f"({left})@ == ({right})@"
            view_type = ty.spec_view.name
            layer = "type_view"
        elif layer in {"L1", "L2", "L4"}:
            lhs, rhs = resolution.view_expr(left), resolution.view_expr(right)
            identity = (
                re.sub(r"[()\s]", "", lhs) == left
                and re.sub(r"[()\s]", "", rhs) == right
            )
            if not identity:
                relation = f"{lhs} == {rhs}"
        if relation is None:
            relation = _generic_view_equal(ty, left, right, caller_generics)
            if relation is not None:
                layer = "generic_bound"
                view_type = f"<{ty.name} as View>::V"
        if relation is None:
            if name in selected:
                raise InputViewError(f"No source-backed input View is available for {name}: {ty.name}")
            observations.append({
                "parameter": name, "mode": "shared", "type": ty.name,
                "reason": "no_nonidentity_view",
            })
            continue
        if parameter.destructure_ctor:
            raise InputViewError(
                f"Pairing destructured {parameter.destructure_ctor} input {name} needs an explicit backend lowering"
            )
        pairs.append(InputPair(name, left, right, relation))
        observations.append({
            "parameter": name, "mode": "paired", "type": ty.name,
            "state": "pre" if parameter.is_mut_ref else "value",
            "left": left, "right": right, "relation": "view_equivalence",
            "view_type": view_type, "source": layer,
        })
        for entry in declared:
            definition = {
                "type": entry.target_name, "file": entry.source_file, "line": entry.source_line,
                "signature": entry.view_fn_signature, "body": entry.view_fn_body,
            }
            if definition not in definitions:
                definitions.append(definition)
        if layer == "source-bound":
            for entry in registry.bound_views.values():
                if entry.rationale == resolution.rationale:
                    definition = {"parameter": name, **as_object(entry)}
                    if definition not in definitions:
                        definitions.append(definition)
    if not pairs:
        raise NoAbstractInputs("No selected input has a nonidentity View to compare")
    return InputPlan(tuple(pairs), tuple(observations), tuple(definitions))
