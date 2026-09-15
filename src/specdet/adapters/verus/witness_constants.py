"""Evaluate simple source constants for independently checked replay hints."""
from __future__ import annotations

import re

from .discovery import parser
from .native.codegen.expressions import free_identifiers


def constant_hints(source: str, expressions: tuple[str, ...]) -> list[str]:
    data = source.encode()
    tree = parser().parse(data)
    declarations: dict[str, list] = {}

    def collect(node):
        if node.type == "const_item":
            name = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if value is None:
                assignment = next(
                    (child for child in node.named_children if child.type == "const_assign_or_spec"), None,
                )
                if assignment is not None:
                    value = assignment.child_by_field_name("value")
            if name is not None and value is not None:
                declarations.setdefault(name.text.decode(), []).append(value)
            return
        for child in node.named_children:
            collect(child)

    collect(tree.root_node)

    def evaluate(node, active: frozenset[str]):
        kind = node.type
        if kind == "integer_literal":
            literal = re.sub(r"(?:u|i)(?:8|16|32|64|128|size)$", "", node.text.decode()).replace("_", "")
            return int(literal, 0) if literal.lower().startswith(("0x", "0b", "0o")) else int(literal)
        if kind == "identifier":
            name = node.text.decode()
            values = declarations.get(name, [])
            if name in active or len(values) != 1:
                raise ValueError("Ambiguous or cyclic constant")
            return evaluate(values[0], active | {name})
        if kind == "parenthesized_expression" and len(node.named_children) == 1:
            return evaluate(node.named_children[0], active)
        if kind == "binary_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is None or right is None:
                raise ValueError("Missing constant operand")
            operation = data[left.end_byte:right.start_byte].decode().strip()
            first, second = evaluate(left, active), evaluate(right, active)
            if operation == "+":
                return first + second
            if operation == "-":
                return first - second
            if operation == "*":
                return first * second
            if operation == "/" and second:
                return first // second
            if operation == "%" and second:
                return first % second
            if operation == "<<" and 0 <= second <= 128:
                return first << second
            if operation == ">>" and 0 <= second <= 128:
                return first >> second
            if operation == "|":
                return first | second
            if operation == "&":
                return first & second
            if operation == "^":
                return first ^ second
        raise ValueError("Unsupported constant expression")

    referenced = set()
    for expression in expressions:
        referenced.update(free_identifiers(expression))
    hints = []
    for name in sorted(referenced & declarations.keys()):
        if len(declarations[name]) != 1:
            continue
        try:
            value = evaluate(declarations[name][0], frozenset({name}))
        except (ValueError, OverflowError, ZeroDivisionError):
            continue
        if 0 <= value < 2**128:
            hints.append(f"assert({name} == {value}) by (compute_only);")
    return hints
