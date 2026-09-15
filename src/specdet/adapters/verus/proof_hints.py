"""Mechanically derived proof hints; every result still requires verification."""
from __future__ import annotations

from specdet.domain.models import Obligation

from .discovery import parser
from .native.codegen.expressions import Unsupported, identifiers_in_text, rename_free_identifiers


def sequence_extensionality(obligation: Obligation) -> str | None:
    native = obligation.native
    result_type = native.get("function_spec", {}).get("return_type", {})
    if result_type.get("kind") != "Seq":
        return None
    template = str(native.get("template", ""))
    source = ("verus! {\n" + template + "\n}").encode()
    tree = parser().parse(source)
    if tree.root_node.has_error:
        return None
    target = str(native.get("fn_det", ""))
    function = None

    def locate(node) -> None:
        nonlocal function
        if node.type == "function_item":
            name = node.child_by_field_name("name")
            if name is not None and name.text.decode() == target:
                function = node
            return
        for child in node.named_children:
            locate(child)

    locate(tree.root_node)
    if function is None:
        return None
    clauses = []

    def find_clauses(node) -> None:
        if node.type == "block":
            return
        if node.type == "ensures_clause":
            clauses.append(node)
            return
        for child in node.named_children:
            find_clauses(child)

    find_clauses(function)
    if len(clauses) != 1 or len(clauses[0].named_children) != 1:
        return None
    implication = clauses[0].named_children[0]
    left = implication.child_by_field_name("left")
    right = implication.child_by_field_name("right")
    if left is None or right is None or source[left.end_byte:right.start_byte].strip() != b"==>":
        return None
    used = identifiers_in_text(template)
    index, suffix = "__specdet_index", 0
    while index in used:
        suffix += 1
        index = f"__specdet_index_{suffix}"
    instances: list[str] = []

    def quantifiers(node) -> None:
        if node.type == "quantifier_expression":
            if not node.text.decode().lstrip().startswith("forall"):
                return
            parameters = next((child for child in node.named_children if child.type == "closure_parameters"), None)
            body = node.child_by_field_name("body")
            if parameters is None or body is None or len(parameters.named_children) != 1:
                return
            parameter = parameters.named_children[0]
            pattern = parameter.child_by_field_name("pattern")
            typ = parameter.child_by_field_name("type")
            if pattern is None or typ is None or pattern.type != "identifier" or typ.text != b"int":
                return
            text = body.text
            edits = []

            def attributes(current) -> None:
                if current.type == "attribute_item" and current.text.strip() in {b"#[trigger]", b"#![auto]"}:
                    edits.append((current.start_byte - body.start_byte, current.end_byte - body.start_byte))
                    return
                for child in current.named_children:
                    attributes(child)

            attributes(body)
            for start, end in sorted(edits, reverse=True):
                text = text[:start] + text[end:]
            try:
                instantiated = rename_free_identifiers(text.decode(), {pattern.text.decode(): index})
            except Unsupported:
                return
            instances.append(f"        assert({instantiated});")
            return
        for child in node.named_children:
            quantifiers(child)

    quantifiers(left)
    if not instances:
        return None
    has_view = result_type.get("spec_view") is not None
    lhs, rhs = ("(r1@)", "(r2@)") if has_view else ("r1", "r2")
    antecedent = left.text.decode()
    return (
        f"if {antecedent} {{\n"
        f"    assert({lhs}.len() == {rhs}.len());\n"
        f"    assert forall|{index}: int| 0 <= {index} < {lhs}.len()\n"
        f"        implies {lhs}[{index}] == {rhs}[{index}] by {{\n"
        + "\n".join(instances)
        + "\n    }\n"
        f"    assert({lhs} =~= {rhs});\n"
        "}"
    )

