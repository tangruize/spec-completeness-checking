from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from specdet.config import Config
from specdet.domain.capabilities import capability_for
from specdet.domain.models import JsonObject, Stage, text_digest

from .discovery import parser, scan_source


KINDS: dict[Stage, set[str]] = {stage: set(capability_for(stage).kinds) for stage in Stage}


def relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("A nonempty project-relative file is required")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value != path.as_posix():
        raise ValueError("Proposal paths must stay inside the project")
    return value


def source_tokens(source: str) -> tuple[tuple[str, str], ...] | None:
    data = source.encode("utf-8")
    root = parser().parse(data).root_node
    if root.has_error:
        return None
    result: list[tuple[str, str]] = []

    def walk(node) -> None:
        if node.type in {"line_comment", "block_comment"}:
            return
        if node.children:
            for child in node.children:
                walk(child)
        else:
            result.append((node.type, data[node.start_byte:node.end_byte].decode("utf-8")))

    walk(root)
    return tuple(result)


def checked_edits(payload: JsonObject, sources: dict[str, str]) -> tuple[dict[str, str], list[JsonObject], bool]:
    if set(payload) != {"edits"}:
        raise ValueError("Source proposals require only an edits array")
    edits = payload["edits"]
    if not isinstance(edits, list) or not edits or len(edits) > 32:
        raise ValueError("edits must contain between 1 and 32 exact source-bound edits")
    result = dict(sources)
    transformations: list[JsonObject] = []
    faithful = True
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"file", "before", "after"}:
            raise ValueError("Each edit requires file, before, and after")
        relative = relative_path(edit["file"])
        before, after = edit["before"], edit["after"]
        if not isinstance(before, str) or not before or not isinstance(after, str):
            raise ValueError("Edit before must be nonempty text and after must be text")
        if relative not in result:
            raise ValueError("Edits can only address existing prepared source files")
        old = result[relative]
        if old.count(before) != 1:
            raise ValueError("Edit before must match exactly one occurrence in the current source")
        new = old.replace(before, after, 1)
        old_tokens, new_tokens = source_tokens(old), source_tokens(new)
        _, gaps = scan_source(old, relative)
        preserved = old_tokens is not None and old_tokens == new_tokens and not gaps
        faithful = faithful and preserved
        result[relative] = new
        transformations.append({
            "file": relative, "before_digest": text_digest(old),
            "after_digest": text_digest(new), "token_preserving": preserved,
        })
    changed = {name: text for name, text in result.items() if text != sources[name]}
    if not changed:
        raise ValueError("Proposal does not change the source")
    return changed, transformations, faithful


def checked_profile(payload: JsonObject, config: Config, files: set[str]) -> tuple[Config, bool]:
    if not payload or set(payload) - {"build", "type_sources", "include", "exclude", "visibility"}:
        raise ValueError("A project profile may contain only supported build and source-selection fields")
    values: dict = {}
    for key in ("type_sources", "include", "exclude"):
        if key in payload:
            value = payload[key]
            if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
                raise ValueError(f"{key} must be an array of nonempty strings")
            if any(Path(x).is_absolute() or ".." in Path(x).parts for x in value):
                raise ValueError(f"{key} must stay inside the project")
            if key == "type_sources" and any(x not in files for x in value):
                raise ValueError("Every type source must exist in the project")
            values[key] = tuple(value)
    if "visibility" in payload:
        if payload["visibility"] not in {"all", "public"}:
            raise ValueError("visibility must be all or public")
        values["visibility"] = payload["visibility"]
    build = config.build
    if "build" in payload:
        supplied = payload["build"]
        allowed = {"adapter", "entrypoint", "injection_file", "package", "features", "extra_args", "verify_module"}
        if not isinstance(supplied, dict) or not supplied or set(supplied) - allowed:
            raise ValueError("Unsupported build profile fields")
        updates = {}
        for key, value in supplied.items():
            if key in {"features", "extra_args"}:
                if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                    raise ValueError(f"build.{key} must be an array of strings")
                updates[key] = tuple(value)
            elif not isinstance(value, str):
                raise ValueError(f"build.{key} must be text")
            else:
                updates[key] = value
        build = replace(build, **updates)
        if build.adapter not in {"verus.single_file", "verus.native", "verus.cargo"}:
            raise ValueError("Unsupported build adapter")
        for relative in (build.entrypoint, build.injection_file):
            if relative and relative_path(relative) not in files:
                raise ValueError("Build files must exist in the project")
        if build.adapter == "verus.native" and not build.entrypoint:
            raise ValueError("Native verification requires an entrypoint")
        if build.adapter == "verus.cargo" and not any(name.endswith("Cargo.toml") for name in files):
            raise ValueError("Cargo verification requires a manifest")
        if build.verify_module and not re.fullmatch(r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*", build.verify_module):
            raise ValueError("verify_module must be a Rust module path")
        values["build"] = build
    updated = replace(config, **values)
    updated.validate()
    # Resolving an unspecified file is safe; changing build semantics is not.
    old_semantics = replace(config.build, entrypoint="", injection_file="")
    new_semantics = replace(build, entrypoint="", injection_file="")
    faithful = old_semantics == new_semantics
    for key in ("entrypoint", "injection_file"):
        old = getattr(config.build, key)
        faithful = faithful and (not old or old == getattr(build, key))
    faithful = faithful and updated.type_sources == config.type_sources
    return updated, faithful


def proof_structure(proof: str, helpers: str, reserved: set[str]) -> tuple[str, ...]:
    if len(proof) + len(helpers) > 200_000:
        raise ValueError("Proof proposal exceeds the bounded candidate size")
    wrapped = f"verus! {{ proof fn __specdet_candidate_body() {{\n{proof}\n}} }}".encode()
    root = parser().parse(wrapped).root_node
    if root.has_error:
        raise ValueError("Proof body is not well-formed Verus syntax")
    functions = []

    def collect(node, found: list) -> None:
        if node.type in {"function_item", "function_signature_item"}:
            found.append(node)
        for child in node.named_children:
            collect(child, found)

    collect(root, functions)
    if len(functions) != 1:
        raise ValueError("Proof bodies cannot introduce nested or escaped declarations")
    name = functions[0].child_by_field_name("name")
    if name is None or wrapped[name.start_byte:name.end_byte] != b"__specdet_candidate_body":
        raise ValueError("Proof body escaped its frozen function")
    blocks = [node for node in root.named_children if node.type == "verus_block"]
    if len(blocks) != 1 or len(blocks[0].named_children) != 1:
        raise ValueError("Proof body must contain only the supplied function body")
    if not helpers.strip():
        return ()
    data = f"verus! {{\n{helpers}\n}}".encode()
    tree = parser().parse(data).root_node
    if tree.has_error:
        raise ValueError("Helper lemmas are not well-formed Verus syntax")
    blocks = [node for node in tree.named_children if node.type == "verus_block"]
    if len(blocks) != 1 or len(tree.named_children) != 1:
        raise ValueError("Helper lemmas cannot escape their module")
    names: list[str] = []
    for declaration in blocks[0].named_children:
        if declaration.type in {"line_comment", "block_comment"}:
            continue
        if declaration.type != "declaration_with_attrs":
            raise ValueError("Helpers must be explicit proof function definitions")
        functions = [c for c in declaration.named_children if c.type == "function_item"]
        if len(functions) != 1:
            raise ValueError("Helpers must be proof functions, not imports, types, or macros")
        function = functions[0]
        mode = next((c for c in function.named_children if c.type == "function_mode"), None)
        if mode is None or data[mode.start_byte:mode.end_byte] != b"proof":
            raise ValueError("Every helper must be a proof function verified as an additional goal")
        if function.child_by_field_name("body") is None:
            raise ValueError("Helper declarations without a proof body are not allowed")
        name = function.child_by_field_name("name")
        value = data[name.start_byte:name.end_byte].decode()
        if value in reserved or value in names:
            raise ValueError("Helper names must not shadow the frozen context or each other")
        nested: list = []
        collect(function, nested)
        if len(nested) != 1:
            raise ValueError("Nested helper goals are not supported")
        names.append(value)
    return tuple(names)
