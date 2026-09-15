"""Explicit, collision-free input bindings for native paired determinism."""

from __future__ import annotations

from dataclasses import dataclass
import re

from specdet.adapters.verus.native.extract.types import FunctionSpec, TypeKind

from .expressions import (
    Unsupported, free_identifiers, identifier_key, identifiers_in_text, is_identifier,
)


@dataclass(frozen=True)
class InputPair:
    parameter: str
    left: str
    right: str
    relation: str


def _reserved_identifiers(spec: FunctionSpec) -> set[str]:
    names = {"r1", "r2", "__DetSelf", "__PRE__", "__POST__", "__RESULT__"}
    check_name = "det_" + re.sub(r"[^A-Za-z0-9_]", "_", spec.name)
    names.update({check_name, f"{check_name}_equal"})
    for param in spec.params:
        name = "self_" if param.is_self else param.name
        names.update(map(identifier_key, (
            param.name, name, f"pre_{name}", f"post1_{name}", f"post2_{name}",
        )))
    texts = [
        spec.name, spec.result_binding, spec.generics_decl, spec.where_decl,
        spec.self_type or "", spec.trait_name or "", spec.return_type.name,
        *(param.type.name for param in spec.params), *spec.requires, *spec.ensures,
    ]
    for text in texts:
        names.update(identifiers_in_text(text))
    return names


def pair_identifiers(spec: FunctionSpec) -> dict[str, tuple[str, str]]:
    """Allocate both run names for every input without selecting any view.

    Callers select the parameters to pair and supply their approved relations
    separately. Names reserve all original clause identifiers, including local
    binders, as well as the concrete lowerer's pre/post and return parameters.
    """
    used = _reserved_identifiers(spec)
    result = {}
    source_names: set[str] = set()
    for param in spec.params:
        key = identifier_key(param.name)
        if key in source_names:
            raise ValueError(f"Duplicate input parameter: {param.name}")
        source_names.add(key)
        if not param.is_self and not is_identifier(param.name):
            raise Unsupported(f"Unsupported input binding: {param.name!r}")
        base = identifier_key(param.name)
        pair = []
        for run in (1, 2):
            stem = f"__specdet_in{run}_{base}"
            candidate, suffix = stem, 1
            while identifier_key(candidate) in used:
                candidate = f"{stem}_{suffix}"
                suffix += 1
            used.add(identifier_key(candidate))
            pair.append(candidate)
        result[param.name] = (pair[0], pair[1])
    return result


def validate_input_pairs(
    spec: FunctionSpec, input_pairs: tuple[InputPair, ...], check_name: str | None = None,
) -> None:
    if not input_pairs:
        return
    params = {param.name: param for param in spec.params}
    if len({identifier_key(name) for name in params}) != len(spec.params):
        raise ValueError("Cannot pair duplicate input parameters")
    reserved = _reserved_identifiers(spec)
    if check_name:
        reserved.update({identifier_key(check_name), identifier_key(f"{check_name}_equal")})
    selected: set[str] = set()
    aliases: set[str] = set()
    for pair in input_pairs:
        if not isinstance(pair, InputPair):
            raise ValueError("input_pairs must contain InputPair values with an explicit relation")
        if pair.parameter not in params:
            raise ValueError(f"Unknown paired input parameter: {pair.parameter}")
        if pair.parameter in selected:
            raise ValueError(f"Duplicate paired input parameter: {pair.parameter}")
        selected.add(pair.parameter)
        param = params[pair.parameter]
        if param.destructure_ctor not in {None, "Ghost", "Tracked"}:
            raise Unsupported(f"Unsupported paired input destructuring: {param.destructure_ctor}")
        if param.destructure_ctor and param.type.kind in {TypeKind.GHOST, TypeKind.TRACKED}:
            raise Unsupported("Ambiguous wrapped destructuring type; expected the extracted inner binding type")
        for name in (pair.left, pair.right):
            if not is_identifier(name):
                raise ValueError(f"Invalid paired input identifier: {name!r}")
            key = identifier_key(name)
            if key in reserved or key in aliases:
                raise ValueError(f"Paired input identifier collision: {name}")
            aliases.add(key)
    by_parameter = {pair.parameter: pair for pair in input_pairs}
    signature = ["r1", "r2"]
    for param in spec.params:
        if not param.is_self and not is_identifier(param.name):
            raise Unsupported(f"Unsupported input binding: {param.name!r}")
        name = "self_" if param.is_self else param.name
        pair = by_parameter.get(param.name)
        signature.extend(
            (pair.left, pair.right) if pair is not None
            else (f"pre_{name}" if param.is_mut_ref else name,)
        )
        if param.is_mut_ref:
            signature.extend((f"post1_{name}", f"post2_{name}"))
    if len({identifier_key(name) for name in signature}) != len(signature):
        raise ValueError("Paired input/output signature contains a naming collision")
    removed = {
        identifier_key(name)
        for param in spec.params if param.name in selected
        for name in (param.name, "self_" if param.is_self else param.name,
                     f"pre_{'self_' if param.is_self else param.name}")
    }
    outputs = {"r1", "r2"} | {
        f"post{run}_{'self_' if param.is_self else param.name}"
        for param in spec.params if param.is_mut_ref for run in (1, 2)
    }
    for pair in input_pairs:
        if isinstance(pair.relation, str) and "{ASSUMES}" in pair.relation:
            raise Unsupported("An input relation contains the native template marker")
        try:
            free = free_identifiers(pair.relation)
        except Unsupported as exc:
            raise Unsupported(f"Malformed or unsupported input relation for {pair.parameter}: {exc}") from exc
        if not {identifier_key(pair.left), identifier_key(pair.right)} <= free:
            raise ValueError(f"Input relation for {pair.parameter} must reference both paired bindings")
        if free & (removed | outputs):
            raise ValueError(f"Input relation for {pair.parameter} references a removed input or output")
