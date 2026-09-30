"""Small deterministic constructor catalogs for concrete counterexample search."""
from __future__ import annotations

from heapq import heappop, heappush
from itertools import combinations, islice, zip_longest

from specdet.domain.models import JsonObject, JsonValue, canonical_json

from .native.codegen.gen_det import _var_name
from .native.extract import function_spec_from_dict
from .type_context import parse_input_type
from .witness_replay import UnsupportedWitness, witness_goal


def _distinct(values: list[JsonValue], limit: int) -> list[JsonValue]:
    seen = set()
    result = []
    for value in values:
        key = canonical_json(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
            if len(result) >= limit:
                break
    return result


def _fair_product(catalogs):
    """Visit small indices in every dimension before exhausting any one dimension."""
    if any(not values for values in catalogs):
        return
    first = (0,) * len(catalogs)
    pending = [(0, first)]
    seen = {first}
    while pending:
        rank, indices = heappop(pending)
        yield tuple(values[index] for values, index in zip(catalogs, indices))
        for dimension, values in enumerate(catalogs):
            if indices[dimension] + 1 < len(values):
                following = list(indices)
                following[dimension] += 1
                following = tuple(following)
                if following not in seen:
                    seen.add(following)
                    heappush(pending, (rank + 1, following))


def constructor_values(
    ty: dict, function, *, limit: int = 16, depth: int = 0,
    type_arguments: JsonObject | None = None,
) -> list[JsonValue]:
    if depth > 5:
        raise UnsupportedWitness("Constructor catalog exceeds its type-depth bound")
    kind, name = ty.get("kind"), str(ty.get("name", ""))
    type_arguments = {} if type_arguments is None else type_arguments
    if isinstance(type_arguments.get(name), str):
        kind = str(type_arguments[name])
    if kind == "bool":
        return [False, True]
    if kind in {"usize", "u8", "u16", "u32", "u64", "u128", "nat"} or name == "nat":
        return [0, 1, 2, 3]
    if kind in {"int", "isize", "i8", "i16", "i32", "i64", "i128"}:
        return [0, 1, 2, -1]
    if kind == "()":
        return [None]
    arguments = ty.get("type_args", [])
    if kind in {"Ghost", "Tracked"}:
        if kind == "Tracked" or len(arguments) != 1:
            raise UnsupportedWitness("Permission tokens cannot be invented as witness values")
        return [
            {"kind": "ghost", "value": value}
            for value in constructor_values(
                arguments[0], function, limit=limit, depth=depth + 1, type_arguments=type_arguments,
            )
        ]
    if kind in {"Result", "Option"}:
        choices = []
        if kind == "Option":
            choices.append({"kind": "variant", "name": "None", "items": []})
        for index, variant in enumerate(("Ok", "Err") if kind == "Result" else ("Some",)):
            if index < len(arguments):
                choices.extend(
                    {
                        "kind": "variant", "name": variant,
                        "items": [_typed_value(arguments[index], value)],
                    }
                    for value in constructor_values(
                        arguments[index], function, limit=limit, depth=depth + 1, type_arguments=type_arguments,
                    )
                )
        return _distinct(choices, limit)
    if kind == "enum":
        choices = []
        for variant in ty.get("variants", []):
            variant_name = variant.get("name")
            if not isinstance(variant_name, str):
                continue
            inner = variant.get("inner")
            if inner is None:
                choices.append({
                    "kind": "variant", "type": name, "name": variant_name, "items": [],
                })
                continue
            if variant.get("struct_form"):
                fields = inner.get("fields", [])
                catalogs = [
                    constructor_values(
                        field["type"], function, limit=4, depth=depth + 1,
                        type_arguments=type_arguments,
                    )
                    for field in fields
                ]
                for row in islice(_fair_product(catalogs), limit):
                    choices.append({
                        "kind": "variant_struct", "type": name, "name": variant_name,
                        "fields": {
                            field["name"]: _typed_value(field["type"], value)
                            for field, value in zip(fields, row)
                        },
                    })
                continue
            values = constructor_values(
                inner, function, limit=4, depth=depth + 1, type_arguments=type_arguments,
            )
            choices.extend({
                "kind": "variant", "type": name, "name": variant_name,
                "items": [_typed_value(inner, value)],
            } for value in values)
        if choices:
            return _distinct(choices, limit)
    if kind in {"Map", "Set"}:
        arity = 2 if kind == "Map" else 1
        if len(arguments) != arity:
            raise UnsupportedWitness(f"Container arguments are unresolved: {name}")
        catalogs = [
            [_typed_value(argument, item) for item in constructor_values(
                argument, function, limit=4, depth=depth + 1, type_arguments=type_arguments,
            )]
            for argument in arguments
        ]
        if kind == "Set":
            elements = catalogs[0]
            choices = [{"kind": "set", "items": []}]
            choices.extend({"kind": "set", "items": [item]} for item in elements[:max(1, limit // 2)])
            choices.extend(
                {"kind": "set", "items": list(pair)} for pair in combinations(elements, 2)
            )
        else:
            entries = [
                {"key": key, "value": value}
                for key, value in islice(_fair_product(catalogs), max(1, limit // 2))
            ]
            choices = [{"kind": "map", "entries": []}]
            choices.extend({"kind": "map", "entries": [entry]} for entry in entries)
            if len(catalogs[0]) > 1 and catalogs[1]:
                choices.extend({
                    "kind": "map",
                    "entries": [
                        {"key": catalogs[0][0], "value": catalogs[1][0]},
                        {"key": catalogs[0][1], "value": value},
                    ],
                } for value in catalogs[1])
        return _distinct(choices, limit)
    if kind == "Seq":
        expression = parse_input_type(name, function)
        if not arguments:
            raise UnsupportedWitness(f"Sequence element type is unresolved: {name}")
        elements = constructor_values(
            arguments[0], function, limit=4, depth=depth + 1, type_arguments=type_arguments,
        )
        if expression.kind == "array":
            length = expression.extra
            if type(type_arguments.get(length)) is int:
                length = str(type_arguments[length])
            if not length.isdecimal() or not 0 <= int(length) <= 32:
                raise UnsupportedWitness("Array witness search requires a small literal length")
            size = int(length)
            choices = [{"kind": "array", "items": [elements[0]] * size}]
            if size:
                choices.extend(
                    {"kind": "array", "items": [value, *([elements[0]] * (size - 1))]}
                    for value in elements[1:]
                )
            return choices
        category = "vec" if ty.get("spec_view") is not None else "seq"
        values: list[JsonValue] = [{"kind": category, "items": []}]
        values.extend({"kind": category, "items": [element]} for element in elements[:2])
        if len(elements) > 1:
            values.extend([
                {"kind": category, "items": [elements[0], elements[1]]},
                {"kind": category, "items": [elements[1], elements[0]]},
            ])
        values.extend({"kind": category, "items": [element]} for element in elements[2:])
        return _distinct(values, limit)
    if kind == "struct":
        fields = ty.get("fields", [])
        catalogs = [
            constructor_values(
                field["type"], function, limit=4, depth=depth + 1, type_arguments=type_arguments,
            )
            for field in fields
        ]
        if any(not values for values in catalogs):
            return []
        base = [values[0] for values in catalogs]
        rows = [base]
        for index, choices in enumerate(catalogs):
            for value in choices[1:]:
                row = list(base)
                row[index] = value
                rows.append(row)
        if catalogs:
            rows.append([values[min(1, len(values) - 1)] for values in catalogs])
        if name.strip().startswith("("):
            return _distinct([
                {
                    "kind": "tuple",
                    "items": [
                        _typed_value(field["type"], item)
                        for field, item in zip(fields, row)
                    ],
                }
                for row in rows
            ], limit)
        if ty.get("is_opaque"):
            raise UnsupportedWitness(f"Opaque type requires a source-authorized constructor: {name}")
        return _distinct([
            {"kind": "struct", "type": name,
             "fields": {
                 field["name"]: _typed_value(field["type"], item)
                 for field, item in zip(fields, row)
             }}
            for row in rows
        ], limit)
    raise UnsupportedWitness(f"No concrete constructor catalog for {name} ({kind})")


def _typed_value(ty: dict, value: JsonValue) -> JsonValue:
    if ty.get("name") in {"int", "nat"} and type(value) is int:
        return {"kind": "spec_integer", "type": ty["name"], "value": value}
    return value


def _shape(value: JsonValue) -> object:
    if isinstance(value, dict):
        kind = value.get("kind")
        if kind in {"vec", "seq", "array", "tuple", "variant"}:
            return kind, value.get("name"), tuple(_shape(item) for item in value.get("items", []))
        if kind == "variant_struct":
            return kind, value.get("name"), tuple(
                (name, _shape(item)) for name, item in value.get("fields", {}).items()
            )
        if kind == "struct":
            return kind, tuple((name, _shape(item)) for name, item in value["fields"].items())
        if kind == "ghost":
            return kind, _shape(value["value"])
        if kind == "spec_integer":
            return kind, value.get("type")
        if kind == "map":
            return kind, len(value.get("entries", []))
        if kind == "set":
            return kind, len(value.get("items", []))
        return kind
    return type(value).__name__


def _balanced_pairs(values, shape):
    same, different = [], []
    for pair in combinations(values, 2):
        (same if shape(pair[0]) == shape(pair[1]) else different).append(pair)
    return [pair for row in zip_longest(same, different) for pair in row if pair is not None]


def candidate_bindings(
    obligation, *, max_candidates: int = 32, type_arguments: JsonObject | None = None,
):
    """Enumerate typed values; no case names, expected verdicts, or seeded outputs."""
    if max_candidates <= 0:
        return
    function = function_spec_from_dict(obligation.native["function_spec"])
    goal = witness_goal(obligation)
    types = {"r1": function.return_type.to_dict(), "r2": function.return_type.to_dict()}
    input_names = []
    output_names = [("r1", "r2")]
    mutable_inputs = {}
    for parameter in function.params:
        name = _var_name(parameter)
        if parameter.is_mut_ref:
            for post in (f"post1_{name}", f"post2_{name}"):
                types[post] = parameter.type.to_dict()
            output_names.append((f"post1_{name}", f"post2_{name}"))
            mutable_inputs[f"pre_{name}"] = (f"post1_{name}", f"post2_{name}")
            name = f"pre_{name}"
        types[name] = parameter.type.to_dict()
        input_names.append(name)
    if set(types) != {name for name, _ in goal.parameters}:
        raise UnsupportedWitness("The concrete catalog does not cover this frozen input/output layout")
    input_catalogs = [
        constructor_values(types[name], function, type_arguments=type_arguments) for name in input_names
    ]
    output_catalogs = [
        constructor_values(types[first], function, type_arguments=type_arguments)
        for first, _ in output_names
    ]
    outputs = list(islice(_fair_product(output_catalogs), max(16, max_candidates)))
    output_pairs = _balanced_pairs(outputs, lambda row: tuple(_shape(item) for item in row))
    inputs = list(islice(_fair_product(input_catalogs), max_candidates))

    def independent_states():
        for row, (first, second) in _fair_product([inputs, output_pairs]):
            binding = dict(zip(input_names, row))
            for (left, right), left_value, right_value in zip(output_names, first, second):
                binding[left], binding[right] = left_value, right_value
            yield binding

    def unchanged_states():
        if not mutable_inputs:
            return
        pairs = _balanced_pairs(output_catalogs[0], _shape)
        for row, (first, second) in _fair_product([inputs, pairs]):
            binding = dict(zip(input_names, row))
            for old, (left, right) in mutable_inputs.items():
                binding[left] = binding[right] = binding[old]
            yield {**binding, "r1": first, "r2": second}

    # No-op/error branches commonly preserve state. These are candidate
    # values only: replay must still prove every original pre/postcondition.
    seen = set()
    for row in zip_longest(unchanged_states(), independent_states()):
        for binding in row:
            if binding is None:
                continue
            key = canonical_json(binding)
            if key not in seen:
                seen.add(key)
                yield binding
                if len(seen) >= max_candidates:
                    return
