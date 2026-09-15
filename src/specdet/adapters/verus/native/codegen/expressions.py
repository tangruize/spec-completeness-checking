"""Hygienic, byte-addressed substitution of supported Verus expressions.

Token trees and syntax with unknown binding rules are deliberately unsupported.
In particular, parsing a macro's tokens as ordinary identifiers is not a safe
substitute for knowing its expansion.
"""

from __future__ import annotations

from dataclasses import dataclass
from unicodedata import normalize

import tree_sitter as ts
import tree_sitter_verus as tsv


class Unsupported(ValueError):
    """The native lowerer cannot preserve this expression's semantics."""


_parser = ts.Parser(ts.Language(tsv.language()))
_COMMENTS = frozenset({"line_comment", "block_comment"})
_IDENTIFIERS = frozenset({
    "identifier", "self", "type_identifier", "field_identifier",
    "shorthand_field_identifier",
})
_LITERALS = frozenset({
    "integer_literal", "float_literal", "boolean_literal", "char_literal",
    "string_literal", "raw_string_literal", "negative_literal",
})
_TYPES = frozenset({
    "primitive_type", "type_identifier", "scoped_type_identifier",
    "generic_type", "generic_type_with_turbofish", "type_arguments",
    "type_binding", "reference_type", "pointer_type", "array_type",
    "tuple_type", "unit_type", "never_type", "bracketed_type",
    "qualified_type", "bounded_type", "abstract_type", "dynamic_type",
    "trait_bounds", "lifetime", "mutable_specifier",
})


def identifier_key(name: str) -> str:
    return normalize("NFC", name.removeprefix("r#"))


def _children(node: ts.Node) -> list[ts.Node]:
    return [child for child in node.named_children if child.type not in _COMMENTS]


def _function(root: ts.Node) -> ts.Node | None:
    children = _children(root)
    if len(children) != 1 or children[0].type != "declaration_with_attrs":
        return None
    declarations = _children(children[0])
    if len(declarations) != 1 or declarations[0].type != "function_item":
        return None
    return declarations[0]


def is_identifier(name: str) -> bool:
    """Check a *binding* identifier, not just an expression or a path."""
    if not isinstance(name, str) or not identifier_key(name).isidentifier():
        return False
    if name in {"_", "self", "Self", "super", "crate"}:
        return False
    if name.startswith("r#") and identifier_key(name) in {"self", "Self", "super", "crate", "_"}:
        return False
    tree = _parser.parse(f"fn __name_probe({name}: bool) {{}}".encode())
    fn = _function(tree.root_node)
    if tree.root_node.has_error or fn is None:
        return False
    params = fn.child_by_field_name("parameters")
    children = _children(params) if params is not None else []
    if len(children) != 1 or children[0].type != "parameter":
        return False
    pattern = children[0].child_by_field_name("pattern")
    return pattern is not None and pattern.type == "identifier" and pattern.text.decode() == name


def identifiers_in_text(text: str) -> set[str]:
    """Reserve identifiers even in syntax the expression lowerer rejects.

    This permissive token collection is used only for fresh-name allocation,
    never for substitution. Literals and comments cannot introduce bindings.
    """
    tree = _parser.parse(("spec fn __names() -> bool {\n" + text + "\n}").encode())
    names: set[str] = set()

    def visit(node: ts.Node) -> None:
        if node.type in _COMMENTS or node.type in _LITERALS:
            return
        if node.type in _IDENTIFIERS:
            names.add(identifier_key(node.text.decode()))
        for child in node.named_children:
            visit(child)

    visit(tree.root_node)
    return names


def _parse(text: str, *, statements: bool = False) -> tuple[ts.Node, int]:
    prefix = "spec fn __expression() -> bool {\n" + ("" if statements else "(\n")
    suffix = "\n}" if statements else "\n)\n}"
    source = (prefix + text + suffix).encode()
    tree = _parser.parse(source)
    fn = _function(tree.root_node)
    body = fn.child_by_field_name("body") if fn is not None else None
    if tree.root_node.has_error or body is None or body.end_byte != len(source):
        raise Unsupported("Cannot parse native Verus expression without errors")
    if statements:
        return body, len(prefix.encode())
    children = _children(body)
    if len(children) != 1 or children[0].type != "parenthesized_expression":
        raise Unsupported("Expected one native Verus expression")
    inner = _children(children[0])
    if len(inner) != 1:
        raise Unsupported("Expected one native Verus expression")
    return inner[0], len(prefix.encode())


@dataclass(frozen=True)
class StateNames:
    old: str
    final: str | None = None


def _pattern_bindings(node: ts.Node) -> list[ts.Node]:
    kind = node.type
    if kind in {"identifier", "shorthand_field_identifier"}:
        return [node]
    if kind in _LITERALS or kind in {
        "_", "remaining_field_pattern", "range_pattern", "scoped_identifier",
        "scoped_type_identifier", "type_identifier", "mutable_specifier",
    }:
        return []
    if kind == "field_pattern":
        pattern = node.child_by_field_name("pattern")
        if pattern is None:
            pattern = node.child_by_field_name("name")
        if pattern is None:
            raise Unsupported("Unsupported field binding pattern")
        return _pattern_bindings(pattern)
    if kind in {"tuple_struct_pattern", "struct_pattern"}:
        ctor = node.child_by_field_name("type")
        return [
            binding for child in _children(node) if child != ctor
            for binding in _pattern_bindings(child)
        ]
    if kind == "or_pattern":
        alternatives = [_pattern_bindings(child) for child in _children(node)]
        names = [{identifier_key(n.text.decode()) for n in arm} for arm in alternatives]
        if names and any(arm != names[0] for arm in names[1:]):
            raise Unsupported("Or-pattern alternatives bind different identifiers")
        return [binding for arm in alternatives for binding in arm]
    if kind in {
        "tuple_pattern", "slice_pattern", "mut_pattern", "ref_pattern",
        "reference_pattern", "captured_pattern", "match_pattern",
    }:
        condition = node.child_by_field_name("condition")
        return [
            binding for child in _children(node) if child != condition
            for binding in _pattern_bindings(child)
        ]
    raise Unsupported(f"Unsupported native binding pattern: {kind}")


class _Renamer:
    def __init__(
        self,
        text: str,
        name_map: dict[str, str],
        ref_renames: set[str],
        state_names: dict[str, StateNames],
        match_suffix: int | None,
        statements: bool,
        deref_names: dict[str, str],
    ):
        self.text = text.encode()
        self.node, self.offset = _parse(text, statements=statements)
        self.names = {identifier_key(k): v for k, v in name_map.items()}
        self.refs = {identifier_key(k) for k in ref_renames}
        self.states = {identifier_key(k): v for k, v in state_names.items()}
        self.derefs = {identifier_key(k): v for k, v in deref_names.items()}
        self.match_suffix = match_suffix
        destinations = list(name_map.values()) + list(deref_names.values())
        destinations += [state.old for state in state_names.values()]
        destinations += [state.final for state in state_names.values() if state.final is not None]
        if any(not is_identifier(value) for value in destinations):
            raise Unsupported("Native identifier substitutions must target binding identifiers")
        self.targets = {
            identifier_key(v) for k, v in self.names.items() if k != identifier_key(v)
        } | {identifier_key(v) for v in destinations[len(name_map):]}
        self.used = identifiers_in_text(text) | set(self.names) | {
            identifier_key(v) for v in destinations
        }
        self.edits: list[tuple[int, int, bytes]] = []
        self.free: set[str] = set()

    def edit(self, node: ts.Node, replacement: str) -> None:
        start, end = node.start_byte - self.offset, node.end_byte - self.offset
        if not 0 <= start < end <= len(self.text):
            raise Unsupported("Native expression substitution escaped its source span")
        self.edits.append((start, end, replacement.encode()))

    def fresh(self, base: str) -> str:
        candidate, suffix = base, 1
        while identifier_key(candidate) in self.used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        self.used.add(identifier_key(candidate))
        return candidate

    def bind(
        self, patterns: list[ts.Node], *, matches: bool = False,
    ) -> dict[str, str | None]:
        bindings: dict[str, list[ts.Node]] = {}
        for pattern in patterns:
            for node in _pattern_bindings(pattern):
                bindings.setdefault(identifier_key(node.text.decode()), []).append(node)
        env: dict[str, str | None] = {}
        for name, nodes in bindings.items():
            replacement = None
            # Bare Rust pattern identifiers can also resolve to constants.
            # A name already bound by the source signature is not such a
            # constant. Do not gratuitously alpha-rename other pattern names.
            if matches and self.match_suffix is not None and name in self.names:
                replacement = self.fresh(f"{name}_{self.match_suffix}")
            elif name in self.targets:
                replacement = self.fresh(f"__specdet_bound_{name}")
            env[name] = replacement
            if replacement is not None:
                for node in nodes:
                    if node.type == "shorthand_field_identifier":
                        field = node.parent
                        prefix = field.text[:node.start_byte - field.start_byte].decode()
                        self.edit(field, f"{node.text.decode()}: {prefix}{replacement}")
                    else:
                        self.edit(node, replacement)
        return env

    def identifier(self, node: ts.Node, env: dict, *, argument: bool = False) -> None:
        name = identifier_key(node.text.decode())
        if name in env:
            replacement = env[name]
        else:
            self.free.add(name)
            replacement = self.names.get(name)
            if replacement is not None and argument and name in self.refs:
                replacement = f"&{replacement}"
        if replacement is not None and replacement != node.text.decode():
            if node.parent.type == "shorthand_field_initializer":
                replacement = f"{node.text.decode()}: {replacement}"
            self.edit(node, replacement)

    @staticmethod
    def unparen(node: ts.Node) -> ts.Node:
        while node.type == "parenthesized_expression":
            children = _children(node)
            if len(children) != 1:
                break
            node = children[0]
        return node

    def state_reference(self, node: ts.Node, env: dict) -> tuple[str, str, bool] | None:
        if node.type != "call_expression" or not self.states:
            return None
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "identifier":
            return None
        state = identifier_key(fn.text.decode())
        if state not in {"old", "final"} or state in env or state in self.names:
            return None
        args = node.child_by_field_name("arguments")
        children = _children(args) if args is not None else []
        if len(children) != 1:
            raise Unsupported(f"Unsupported {state}(...) state expression")
        arg = self.unparen(children[0])
        dereferenced = arg.type == "unary_expression" and arg.children[0].type == "*"
        if dereferenced:
            arg = self.unparen(_children(arg)[0])
        if arg.type in {"identifier", "self"}:
            name = identifier_key(arg.text.decode())
            if name in env:
                return None
            if name in self.states:
                states = self.states[name]
                target = states.old if state == "old" else states.final
                if target is None:
                    raise Unsupported(f"{state}({name}) has no state in this native clause")
                self.free.add(name)
                return name, target, dereferenced
        # In particular, never recursively turn old(complex(p)) into
        # old(complex(post_p)). Only the documented reference forms are lowered.
        raise Unsupported(f"Unsupported {state}(...) argument; expected an input reference")

    def visit_type(self, node: ts.Node, env: dict) -> None:
        if node.type in {"identifier", "type_identifier", "self", "lifetime"}:
            return
        if node.type in _COMMENTS or node.type in _LITERALS:
            return
        if node.type == "block":
            self.visit(node, env)
            return
        if node.type == "array_type":
            length = node.child_by_field_name("length")
            for child in _children(node):
                if child == length:
                    self.visit(child, env)
                else:
                    self.visit_type(child, env)
            return
        if node.type in _TYPES or node.type in {"scoped_identifier", "crate", "super"}:
            for child in _children(node):
                self.visit_type(child, env)
            return
        raise Unsupported(f"Unsupported native type syntax in expression: {node.type}")

    def attribute(self, node: ts.Node, env: dict) -> None:
        for child in _children(node):
            if child.type == "trigger_attribute":
                for expression in _children(child):
                    self.visit(expression, env)
            elif child.type == "attribute" and child.text.decode() in {"auto", "all_triggers"}:
                continue
            else:
                raise Unsupported("Native expression attribute has unknown binding semantics")

    def block(self, node: ts.Node, env: dict) -> None:
        scope = dict(env)
        for child in _children(node):
            if child.type == "declaration_with_attrs":
                declarations = _children(child)
                if len(declarations) != 1 or declarations[0].type not in {"let_declaration", "empty_statement"}:
                    raise Unsupported("Native expression contains an unsupported local declaration")
                child = declarations[0]
            if child.type == "let_declaration":
                pattern = child.child_by_field_name("pattern")
                if pattern is None:
                    raise Unsupported("Native let declaration has no binding pattern")
                for field in ("value", "alternative"):
                    expression = child.child_by_field_name(field)
                    if expression is not None:
                        self.visit(expression, scope)
                annotation = child.child_by_field_name("type")
                if annotation is not None:
                    self.visit_type(annotation, scope)
                scope.update(self.bind([pattern]))
            else:
                self.visit(child, scope)

    def visit(self, node: ts.Node, env: dict, *, argument: bool = False) -> dict:
        """Visit an expression; return bindings available when it is true."""
        kind = node.type
        if kind in _COMMENTS or kind in _LITERALS or kind in {"unit_expression", "empty_statement"}:
            return {}
        if kind in {"identifier", "self"}:
            self.identifier(node, env, argument=argument)
            return {}
        if kind in _TYPES or kind in {"scoped_identifier", "crate", "super"}:
            self.visit_type(node, env)
            return {}
        if kind in {"macro_invocation", "assert_macro_call", "token_tree"}:
            raise Unsupported("Native substitution does not know this macro's binding semantics")
        if kind == "call_expression":
            state = self.state_reference(node, env)
            if state is not None:
                name, target, dereferenced = state
                if dereferenced and name not in self.refs:
                    target = f"*{target}"
                elif not dereferenced and argument and name in self.refs:
                    target = f"&{target}"
                self.edit(node, target)
            else:
                self.visit(node.child_by_field_name("function"), env)
                for child in _children(node.child_by_field_name("arguments")):
                    self.visit(child, env, argument=True)
            return {}
        if kind == "unary_expression":
            child = _children(node)[0]
            operand = self.unparen(child)
            if node.children[0].type == "*":
                state = self.state_reference(operand, env)
                if state is not None:
                    name, target, dereferenced = state
                    if name in self.refs and not dereferenced:
                        self.edit(node, target)
                        return {}
                if operand.type in {"identifier", "self"}:
                    name = identifier_key(operand.text.decode())
                    if name not in env and (name in self.refs or name in self.derefs):
                        self.free.add(name)
                        self.edit(node, self.derefs.get(name, self.names.get(name, name)))
                        return {}
            self.visit(child, env)
            return {}
        if kind == "parenthesized_expression":
            return self.visit(_children(node)[0], env, argument=argument)
        if kind in {"field_expression", "arrow_expression", "view_expression", "is_expression"}:
            self.visit(node.child_by_field_name("value"), env)
            return {}
        if kind == "struct_expression":
            self.visit_type(node.child_by_field_name("name"), env)
            self.visit(node.child_by_field_name("body"), env)
            return {}
        if kind == "field_initializer":
            self.visit(node.child_by_field_name("value"), env)
            return {}
        if kind == "generic_function":
            self.visit(node.child_by_field_name("function"), env)
            self.visit_type(node.child_by_field_name("type_arguments"), env)
            return {}
        if kind in {"closure_expression", "quantifier_expression"}:
            params = next((c for c in _children(node) if c.type == "closure_parameters"), None)
            if params is None:
                raise Unsupported("Native quantifier or closure has no parameter list")
            patterns = []
            for param in _children(params):
                if param.type == "parameter":
                    pattern = param.child_by_field_name("pattern")
                    annotation = param.child_by_field_name("type")
                    if annotation is not None:
                        self.visit_type(annotation, env)
                else:
                    pattern = param
                if pattern is None:
                    raise Unsupported("Unsupported native closure parameter")
                patterns.append(pattern)
            scope = env | self.bind(patterns)
            body = node.child_by_field_name("body")
            for child in _children(node):
                if child == body:
                    self.visit(child, scope)
                elif child == params:
                    continue
                elif child.type in {"attribute_item", "inner_attribute_item"}:
                    self.attribute(child, scope)
                elif child.type in _TYPES:
                    self.visit_type(child, scope)
                else:
                    raise Unsupported(f"Unsupported native closure syntax: {child.type}")
            return {}
        if kind in {"matches_expression", "let_condition"}:
            self.visit(node.child_by_field_name("value"), env)
            pattern = node.child_by_field_name("pattern")
            if pattern is None:
                raise Unsupported("Native matching expression has no pattern")
            return self.bind([pattern], matches=(kind == "matches_expression"))
        if kind == "binary_expression":
            left = self.visit(node.child_by_field_name("left"), env)
            operator = node.child_by_field_name("operator").type
            propagates = operator in {"&&", "&&&", "==>"}
            right = self.visit(node.child_by_field_name("right"), env | left if propagates else env)
            return left | right if operator in {"&&", "&&&"} else {}
        if kind in {"big_and_expression", "let_chain"}:
            bindings = {}
            for child in _children(node):
                bindings.update(self.visit(child, env | bindings))
            return bindings
        if kind == "if_expression":
            bindings = self.visit(node.child_by_field_name("condition"), env)
            self.visit(node.child_by_field_name("consequence"), env | bindings)
            alternative = node.child_by_field_name("alternative")
            if alternative is not None:
                self.visit(alternative, env)
            return {}
        if kind == "match_expression":
            self.visit(node.child_by_field_name("value"), env)
            for arm in _children(node.child_by_field_name("body")):
                if arm.type != "match_arm":
                    raise Unsupported("Unsupported native match arm")
                pattern = arm.child_by_field_name("pattern")
                if pattern is None:
                    raise Unsupported("Native match arm has no pattern")
                scope = env | self.bind([pattern])
                guard = pattern.child_by_field_name("condition")
                if guard is not None:
                    scope.update(self.visit(guard, scope))
                self.visit(arm.child_by_field_name("value"), scope)
            return {}
        if kind == "block":
            self.block(node, env)
            return {}
        if kind in {"attribute_item", "inner_attribute_item"}:
            self.attribute(node, env)
            return {}
        if kind in {
            "tuple_expression", "array_expression", "index_expression", "range_expression",
            "type_cast_expression", "reference_expression", "has_expression",
            "big_or_expression", "field_initializer_list", "shorthand_field_initializer",
            "base_field_initializer", "else_clause", "expression_statement",
            "attribute_expression",
        }:
            for child in _children(node):
                self.visit(child, env)
            return {}
        raise Unsupported(f"Unsupported native expression syntax: {kind}")

    def render(self) -> str:
        self.visit(self.node, {})
        result = self.text
        boundary = len(result)
        for start, end, replacement in sorted(self.edits, reverse=True):
            if end > boundary:
                raise Unsupported("Overlapping native expression substitutions")
            result = result[:start] + replacement + result[end:]
            boundary = start
        return result.decode()


def rename_free_identifiers(
    text: str,
    name_map: dict[str, str],
    ref_renames: set[str] | None = None,
    *,
    state_names: dict[str, StateNames] | None = None,
    match_suffix: int | None = None,
    statements: bool = False,
    deref_names: dict[str, str] | None = None,
) -> str:
    if not text.strip():
        return text
    return _Renamer(
        text, name_map, ref_renames or set(), state_names or {},
        match_suffix, statements, deref_names or {},
    ).render()


def free_identifiers(text: str) -> set[str]:
    """Validate a single expression and collect its free value identifiers."""
    if not isinstance(text, str) or not text.strip():
        raise Unsupported("Expected a nonempty native Verus expression")
    renamer = _Renamer(text, {}, set(), {}, None, False, {})
    renamer.render()
    return renamer.free
