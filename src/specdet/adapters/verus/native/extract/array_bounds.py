"""Read fixed Rust array extents without evaluating constant expressions."""
from __future__ import annotations

from functools import lru_cache

import tree_sitter as ts
import tree_sitter_verus as tsv


_parser = ts.Parser(ts.Language(tsv.language()))
_COMMENTS = {"line_comment", "block_comment"}


def _children(node: ts.Node) -> list[ts.Node]:
    return [child for child in node.named_children if child.type not in _COMMENTS]


@lru_cache(maxsize=256)
def fixed_array_extent(type_name: str) -> tuple[bool, int | None]:
    """Return (is_fixed_array, literal_extent); symbolic extents stay unknown."""
    if not type_name.lstrip().startswith("["):
        return False, None
    tree = _parser.parse(f"type __specdet_array = {type_name};".encode())
    declarations = _children(tree.root_node)
    if tree.root_node.has_error or len(declarations) != 1:
        return False, None
    declaration = declarations[0]
    if declaration.type == "declaration_with_attrs":
        items = _children(declaration)
        if len(items) != 1:
            return False, None
        declaration = items[0]
    if declaration.type != "type_item":
        return False, None
    array = declaration.child_by_field_name("type")
    if array is None or array.type != "array_type":
        return False, None
    length = array.child_by_field_name("length")
    if length is None:
        return False, None
    while length.type == "parenthesized_expression":
        children = _children(length)
        if len(children) != 1:
            return True, None
        length = children[0]
    if length.type != "integer_literal":
        return True, None
    literal = length.text.decode().removesuffix("usize").replace("_", "")
    base = 0 if literal.startswith(("0x", "0o", "0b")) else 10
    try:
        return True, int(literal, base)
    except ValueError:
        return True, None
