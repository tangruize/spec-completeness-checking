"""Replay concrete candidates against the unchanged source-level obligation.

Successful replay is a verifier certificate, not a fabricated SMT SAT result.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from specdet.domain.models import JsonObject, JsonValue, Obligation, as_object, digest, text_digest
from specdet.storage.artifacts import ArtifactStore, file_digest
from specdet.storage.workspace import PreparedProject

from .discovery import parser
from .execution import classify_process
from .witness_constants import constant_hints
from .native.codegen.expressions import identifiers_in_text, is_identifier


class UnsupportedWitness(ValueError):
    pass


@dataclass(frozen=True)
class WitnessGoal:
    parameters: tuple[tuple[str, str], ...]
    requires: tuple[str, ...]
    posts: str
    distinctness: str
    generics: tuple[tuple[str, str, str], ...] = ()
    generics_decl: str = ""
    where_decl: str = ""


def witness_goal(obligation: Obligation) -> WitnessGoal:
    template = str(obligation.native["template"])
    data = ("verus! {\n" + template + "\n}").encode()
    tree = parser().parse(data)
    if tree.root_node.has_error:
        raise UnsupportedWitness("Cannot parse the frozen replay obligation")
    found = []

    def locate(node):
        if node.type == "function_item":
            name = node.child_by_field_name("name")
            if name is not None and name.text.decode() == obligation.native["fn_det"]:
                found.append(node)
            return
        for child in node.named_children:
            locate(child)

    locate(tree.root_node)
    if len(found) != 1:
        raise UnsupportedWitness("Replay requires one exact frozen determinism function")
    function = found[0]
    generics = function.child_by_field_name("type_parameters")
    generic_parameters = []
    for parameter in generics.named_children if generics else []:
        name = parameter.child_by_field_name("name")
        if name is None:
            raise UnsupportedWitness("Unsupported generic replay parameter")
        if parameter.type == "type_parameter":
            generic_parameters.append((name.text.decode(), "type", ""))
        elif parameter.type == "const_parameter":
            typ = parameter.child_by_field_name("type")
            if typ is None:
                raise UnsupportedWitness("Const replay parameter type is missing")
            generic_parameters.append((name.text.decode(), "const", typ.text.decode()))
        else:
            raise UnsupportedWitness("Lifetime-generic witness replay is not supported")
    parameters = []
    node = function.child_by_field_name("parameters")
    for parameter in node.named_children if node else []:
        pattern = parameter.child_by_field_name("pattern")
        typ = parameter.child_by_field_name("type")
        if pattern is None or typ is None or pattern.type != "identifier":
            raise UnsupportedWitness("Replay currently requires simple native parameter bindings")
        parameters.append((pattern.text.decode(), typ.text.decode()))
    clauses = {"requires_clause": [], "ensures_clause": []}

    def collect(node):
        if node.type == "block":
            return
        if node.type in clauses:
            clauses[node.type].extend(
                child for child in node.named_children
                if child.type not in {"line_comment", "block_comment"}
            )
            return
        for child in node.named_children:
            collect(child)

    collect(function)
    if len(clauses["ensures_clause"]) != 1:
        raise UnsupportedWitness("Replay cannot identify the frozen postcondition relation")
    implication = clauses["ensures_clause"][0]
    left = implication.child_by_field_name("left")
    right = implication.child_by_field_name("right")
    if left is None or right is None or data[left.end_byte:right.start_byte].strip() != b"==>":
        raise UnsupportedWitness("The frozen goal is not a contract implication")
    return WitnessGoal(
        tuple(parameters),
        tuple(node.text.decode() for node in clauses["requires_clause"]),
        left.text.decode(), f"!({right.text.decode()})",
        tuple(generic_parameters), generics.text.decode() if generics is not None else "",
        next((node.text.decode() for node in function.named_children if node.type == "where_clause"), ""),
    )


def _type_node(text: str):
    tree = parser().parse(f"type __ReplayType = {text};".encode())
    items = [
        node for node in tree.root_node.named_children
        if node.type not in {"line_comment", "block_comment"}
    ]
    if tree.root_node.has_error or len(items) != 1:
        raise UnsupportedWitness(f"Unsupported witness type: {text}")
    declaration = items[0]
    if declaration.type == "declaration_with_attrs":
        nested = [
            child for child in declaration.named_children
            if child.type not in {"line_comment", "block_comment"}
        ]
        if len(nested) != 1:
            raise UnsupportedWitness("Witness type cannot introduce attributes or other declarations")
        declaration = nested[0]
    if declaration.type != "type_item":
        raise UnsupportedWitness("Witness type must be one complete type expression")
    node = declaration.child_by_field_name("type")
    if node is None:
        raise UnsupportedWitness("Witness type is missing")
    return node


def _reject_relocated_paths(code: str) -> None:
    tree = parser().parse(("verus! {\n" + code + "\n}").encode())
    if tree.root_node.has_error:
        raise UnsupportedWitness("Cannot establish the generic replay's source namespace")
    tokens = []

    def walk(node):
        if node.type in {
            "line_comment", "block_comment", "string_literal", "raw_string_literal", "char_literal",
        }:
            return
        if not node.children:
            tokens.append(node.text.decode())
        else:
            for child in node.children:
                walk(child)

    walk(tree.root_node)
    if any(
        token in {"self", "super"} and index + 1 < len(tokens) and tokens[index + 1] == "::"
        for index, token in enumerate(tokens)
    ):
        raise UnsupportedWitness(
            "Generic replay cannot relocate self:: or super:: paths without a source-namespace correspondence"
        )


class _Values:
    def __init__(self, reserved: set[str]):
        self.reserved = reserved
        self.statements: list[str] = []
        self.counter = 0

    def fresh(self) -> str:
        while True:
            name = f"__specdet_value_{self.counter}"
            self.counter += 1
            if name not in self.reserved:
                self.reserved.add(name)
                return name

    def expression(self, value: JsonValue, expected: str = "", depth: int = 0) -> str:
        if depth > 12:
            raise UnsupportedWitness("Witness constructor nesting limit exceeded")
        if expected:
            node = _type_node(expected)
            if node.type == "reference_type":
                if any(child.type == "mutable_specifier" for child in node.children):
                    raise UnsupportedWitness("Replay expects detached mutable pre/post values")
                inner = node.child_by_field_name("type")
                if inner is None:
                    raise UnsupportedWitness("Reference pointee type is missing")
                return "&(" + self.expression(value, inner.text.decode(), depth + 1) + ")"
        if type(value) is bool:
            return "true" if value else "false"
        if type(value) is int:
            return str(value) if value >= 0 else f"({value})"
        if value is None:
            return "()"
        if not isinstance(value, dict):
            raise UnsupportedWitness("Witness values must be typed constructors, integers, booleans or unit")
        kind = value.get("kind")
        if kind in {"tuple", "array", "vec", "seq"}:
            if set(value) != {"kind", "items"} or not isinstance(value["items"], list):
                raise UnsupportedWitness("Sequence constructors require an items array")
            if len(value["items"]) > 1024:
                raise UnsupportedWitness("Witness constructor length limit exceeded")
            items = [self.expression(item, depth=depth + 1) for item in value["items"]]
            if kind == "tuple":
                return "(" + ", ".join(items) + ("," if len(items) == 1 else "") + ")"
            if kind == "array":
                return "[" + ", ".join(items) + "]"
            if kind == "seq":
                return "Seq::empty()" + "".join(f".push({item})" for item in items)
            name = self.fresh()
            annotation = f": {expected}" if expected else ""
            self.statements.append(f"let mut {name}{annotation} = Vec::new();")
            self.statements.extend(f"{name}.push({item});" for item in items)
            return name
        if kind == "repeat_array":
            if set(value) != {"kind", "value", "count"} or type(value["count"]) is not int:
                raise UnsupportedWitness("Repeat arrays require value and integer count")
            if not 0 <= value["count"] <= 1024:
                raise UnsupportedWitness("Repeat array count is outside replay limits")
            return f"[{self.expression(value['value'], depth=depth + 1)}; {value['count']}]"
        if kind == "ghost":
            if set(value) != {"kind", "value"}:
                raise UnsupportedWitness("Ghost constructor requires exactly one value")
            expression = self.expression(value["value"], depth=depth + 1)
            name = self.fresh()
            self.statements.append(f"let ghost {name} = {expression};")
            return f"Ghost({name})"
        if kind == "variant":
            if set(value) != {"kind", "name", "items"} or value["name"] not in {"Ok", "Err", "Some", "None"}:
                raise UnsupportedWitness("Unsupported witness variant")
            items = value["items"]
            if not isinstance(items, list) or len(items) != (0 if value["name"] == "None" else 1):
                raise UnsupportedWitness("Variant payload arity mismatch")
            return str(value["name"]) + (
                "(" + self.expression(items[0], depth=depth + 1) + ")" if items else ""
            )
        if kind == "struct":
            if set(value) - {"kind", "fields", "type"} or not isinstance(value.get("fields"), dict):
                raise UnsupportedWitness("Struct constructor requires named fields")
            constructor = value.get("type", expected)
            if not isinstance(constructor, str) or not constructor:
                raise UnsupportedWitness("Struct constructor requires a source type")
            constructor_node = _type_node(constructor)
            if constructor_node.type not in {"type_identifier", "scoped_type_identifier", "generic_type"}:
                raise UnsupportedWitness("Struct constructor must name a type")
            constructor = constructor_node.text.decode()
            pending = [constructor_node]
            while pending:
                node = pending.pop()
                if node.type in {"macro_invocation", "block", "call_expression"}:
                    raise UnsupportedWitness("Witness constructor types cannot contain executable expressions")
                pending.extend(node.named_children)
            if constructor_node.type == "generic_type":
                head = constructor_node.child_by_field_name("type")
                arguments = constructor_node.child_by_field_name("type_arguments")
                if head is None or arguments is None:
                    raise UnsupportedWitness("Generic constructor type is incomplete")
                constructor = head.text.decode() + "::" + arguments.text.decode()
            fields = []
            for name, item in value["fields"].items():
                if not is_identifier(name):
                    raise UnsupportedWitness("Invalid witness field name")
                fields.append(f"{name}: {self.expression(item, depth=depth + 1)}")
            return constructor + " { " + ", ".join(fields) + " }"
        raise UnsupportedWitness(f"Unsupported witness constructor kind: {kind}")


def _value_facts(expression: str, value: JsonValue) -> list[str]:
    if type(value) in {int, bool}:
        literal = str(value).lower() if type(value) is bool else str(value)
        return [f"{expression} == {literal}"]
    if not isinstance(value, dict):
        return []
    kind = value.get("kind")
    if kind in {"vec", "seq"}:
        base = f"({expression})@" if kind == "vec" else expression
        items = value["items"]
        facts = [f"({base}).len() == {len(items)}"]
        for index, item in enumerate(items):
            facts.extend(_value_facts(f"({base})[{index}]", item))
        return facts
    if kind == "tuple":
        return [
            fact for index, item in enumerate(value["items"])
            for fact in _value_facts(f"({expression}).{index}", item)
        ]
    if kind == "struct":
        return [
            fact for field, item in value["fields"].items()
            for fact in _value_facts(f"({expression}).{field}", item)
        ]
    if kind == "ghost":
        return _value_facts(f"({expression})@", value["value"])
    return []


def render_witness_replay(
    obligation: Obligation, bindings: JsonObject, *, type_arguments: JsonObject | None = None,
) -> tuple[str, str, WitnessGoal]:
    goal = witness_goal(obligation)
    type_arguments = {} if type_arguments is None else type_arguments
    if set(type_arguments) != {name for name, _, _ in goal.generics}:
        raise UnsupportedWitness("Replay must explicitly instantiate every generic parameter")
    expected = {name for name, _ in goal.parameters}
    if set(bindings) != expected:
        raise UnsupportedWitness("A replay must bind every frozen input and both output states exactly")
    reserved = identifiers_in_text(str(obligation.native["source"]) + str(obligation.native["template"]))
    name = f"__specdet_witness_{digest([obligation.id, bindings, type_arguments])[:20]}"
    if name in reserved:
        raise UnsupportedWitness("Replay function collides with an existing declaration")
    values = _Values(reserved | expected)
    aliases: list[str] = []
    for generic, kind, typ in goal.generics:
        value = type_arguments[generic]
        if kind == "type":
            if value not in {"bool", "u8", "u16", "u32", "u64", "usize", "int"}:
                raise UnsupportedWitness("Replay type instantiation must be an explicit scalar type")
            aliases.append(f"type {generic} = {value};")
        else:
            if type(value) is not int or value < 0:
                raise UnsupportedWitness("Replay const instantiation must be a nonnegative integer")
            aliases.append(f"const {generic}: {typ} = {value};")
    facts: list[str] = []
    for parameter, typ in goal.parameters:
        expression = values.expression(bindings[parameter], typ)
        values.statements.append(f"let {parameter}: {typ} = {expression};")
        base = f"*({parameter})" if _type_node(typ).type == "reference_type" else parameter
        facts.extend(_value_facts(base, bindings[parameter]))
    assertions = [
        *(f"assert({fact});" for fact in facts),
        *constant_hints(
            str(obligation.native["source"]), (*goal.requires, goal.posts, goal.distinctness),
        ),
        *(f"assert({clause});" for clause in goal.requires),
        f"assert({goal.posts});",
        f"reveal({obligation.native['equal_fn']});",
        f"assert({goal.distinctness});",
    ]
    bounds_name = name + "_bounds"
    bounds = ""
    if goal.generics:
        # This helper has no logical postcondition. Its call only checks the
        # original generic/where requirements for the chosen concrete types.
        bounds = f"proof fn {bounds_name}{goal.generics_decl}() {goal.where_decl} {{}}\n\n"
        arguments = ", ".join(generic for generic, _, _ in goal.generics)
        assertions.insert(0, f"{bounds_name}::<{arguments}>();")
    code = (
        str(obligation.native["det_spec"]["equal_fn_def"]) + "\n\n"
        + bounds
        + f"fn {name}() {{\n"
        + "\n".join("    " + statement for statement in values.statements)
        + "\n    proof {\n"
        + "\n".join("        " + statement for statement in assertions)
        + "\n    }\n}\n"
    )
    if aliases:
        _reject_relocated_paths("\n".join(aliases) + "\n" + code)
        code = (
            f"mod {name}_module {{\nuse super::*;\n"
            + "\n".join(aliases) + "\n" + code + "\n}\n"
        )
    return name, code, goal


def replay_witness(
    backend, project: PreparedProject, obligation: Obligation,
    bindings: JsonObject, artifact_dir: Path, *, type_arguments: JsonObject | None = None,
) -> JsonObject:
    """Certify the candidate against P, both Q instances, and frozen inequality."""
    backend._check_frozen(obligation)
    type_arguments = {} if type_arguments is None else type_arguments
    name, code, goal = render_witness_replay(obligation, bindings, type_arguments=type_arguments)
    if project.snapshot_digest != obligation.native["snapshot_digest"]:
        raise UnsupportedWitness("Replay source snapshot differs from the frozen problem")
    kind = str(obligation.native.get("analysis_kind", "concrete_determinism"))
    if backend._semantic_config(kind) != obligation.native["semantic_config"]:
        raise UnsupportedWitness("Replay build semantics differ from the frozen problem")
    if artifact_dir.exists():
        raise FileExistsError(f"Replay requires a fresh artifact directory: {artifact_dir}")
    store = ArtifactStore(artifact_dir)
    worktree = artifact_dir / "worktree"
    transformations = backend._copy_worktree(project, worktree, obligation.native["overlays"])
    entry, injection, module = backend._build_paths(project, obligation, worktree)
    if goal.generics:
        module = "::".join(part for part in (module, name + "_module") if part)
    original = injection.read_text(encoding="utf-8")
    if text_digest(original) != obligation.native["source_digest"]:
        raise UnsupportedWitness("Replay source does not match its obligation")
    injected = backend._inject(original, obligation.native["source_context"], code)
    injection.write_text(injected, encoding="utf-8")
    source_digests = {
        relative: file_digest(worktree / relative) for relative in project.files
    }
    store.write_text("harness.rs", injected)
    process = backend.executor.verify(
        entry, worktree, artifact_dir / "verifier", name, module,
    )
    status, goals = classify_process(process)
    changed = [
        relative for relative, expected in source_digests.items()
        if file_digest(worktree / relative) != expected
    ]
    if changed:
        status, goals = "error", 0
    evidence: JsonObject = {
        "schema_version": 1,
        "kind": "source_witness_replay",
        "problem_id": obligation.problem_id,
        "obligation_digest": obligation.id,
        "bindings": bindings,
        "bindings_digest": digest(bindings),
        "type_arguments": type_arguments,
        "formula": as_object(goal),
        "formula_digest": digest(goal),
        "status": status,
        "verified_goals": goals,
        "verifier": as_object(process),
        "harness_digest": text_digest(injected),
        "artifact": str(artifact_dir),
        "transformations": transformations,
        "modified_inputs": changed,
        "raw_solver_status": "not_reported",
    }
    store.artifact("replay.json", "source_witness_replay", evidence, (obligation.id, digest(bindings)))
    return evidence
