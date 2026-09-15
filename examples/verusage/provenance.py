from __future__ import annotations

import hashlib
from pathlib import Path

from specdet.adapters.verus.discovery import parser

TOOL_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = TOOL_ROOT / "tests" / "fixtures" / "verusage"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"
REPOSITORIES = (
    "anvil-controller", "anvil-library", "atmosphere", "ironkv",
    "memory-allocator", "node-replication", "nrkernel", "storage", "vest",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def contained_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    resolved = (root / path).resolve()
    if path.is_absolute() or not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"Not a contained relative path: {relative}")
    return resolved


def span(data: bytes, start: int, end: int) -> dict:
    return {
        "start_byte": start,
        "end_byte": end,
        "start_line": data.count(b"\n", 0, start) + 1,
        "end_line": data.count(b"\n", 0, max(start, end - 1)) + 1,
        "sha256": sha256(data[start:end]),
    }


def span_bytes(data: bytes, location: dict) -> bytes:
    start, end = location["start_byte"], location["end_byte"]
    if not 0 <= start <= end <= len(data):
        raise ValueError(f"Invalid byte span: {start}:{end}")
    return data[start:end]


def functions(data: bytes) -> list[dict]:
    result = []

    def text(node) -> str:
        return data[node.start_byte:node.end_byte].decode("utf-8")

    def walk(node, owners: tuple[str, ...] = ()) -> None:
        if node.type in {"impl_item", "trait_item"}:
            field = "type" if node.type == "impl_item" else "name"
            owner = node.child_by_field_name(field)
            if owner is not None:
                owners = (*owners, text(owner))
        if node.type in {"function_item", "function_signature_item"}:
            name = node.child_by_field_name("name")
            if name is None:
                return
            mode_node = next(
                (child for child in node.named_children if child.type == "function_mode"), None,
            )
            mode = text(mode_node).split("(")[0] if mode_node is not None else "exec"
            body = node.child_by_field_name("body")
            end_header = body.start_byte if body is not None else node.end_byte
            wrapper = node.parent if node.parent.type == "declaration_with_attrs" else node
            attributes = [
                text(child) for child in wrapper.named_children if child.type == "attribute_item"
            ]
            result.append({
                "name": text(name),
                "owners": list(owners),
                "qualified_name": "::".join((*owners, text(name))),
                "mode": mode,
                "declaration": node.type == "function_signature_item",
                "external_body": any("external_body" in attribute for attribute in attributes),
                "span": span(data, node.start_byte, node.end_byte),
                "header_span": span(data, node.start_byte, end_header),
                "header": data[node.start_byte:end_header].decode("utf-8"),
                "body_span": span(data, body.start_byte, body.end_byte) if body is not None else None,
            })
            return
        for child in node.named_children:
            walk(child, owners)

    walk(parser().parse(data).root_node)
    return result


def selected_function(data: bytes, case: dict) -> dict:
    owner = case.get("owner")
    found = [
        item for item in functions(data)
        if item["name"] == case["function"] and item["mode"] == case["mode"]
        and (not owner or any(name.split("<")[0] == owner for name in item["owners"]))
        and (not case.get("declaration") or item["declaration"])
    ]
    if len(found) != 1:
        raise ValueError(f"{case['id']}: expected one exact function, found {len(found)}")
    return found[0]
