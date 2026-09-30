from __future__ import annotations

import re
import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable

from specdet.config import Config
from specdet.domain.models import Diagnostic, Stage, TargetRef, text_digest
from specdet.storage.workspace import PreparedProject, input_files


@dataclass(frozen=True)
class FunctionLocation:
    target: TargetRef
    start: int
    end: int
    insertion: int
    in_verus: bool
    has_contract: bool
    declaration: bool
    unsupported: str = ""
    extractor_line: int = 0
    extractor_name: str = ""


def parser():
    from tree_sitter import Language, Parser
    import tree_sitter_verus

    return Parser(Language(tree_sitter_verus.language()))


def _text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8")


def _cfg(expression: str, features: tuple[str, ...], test: bool) -> bool | None:
    expression = expression.strip()
    if match := re.fullmatch(r'feature\s*=\s*"([^"]+)"', expression):
        return match[1] in features
    if expression == "test":
        return test
    if expression in {"verus_keep_ghost", "verus_keep_ghost_body"}:
        return True
    if match := re.fullmatch(r"(all|any|not)\s*\((.*)\)", expression, re.S):
        pieces, start, depth, quoted = [], 0, 0, False
        for index, char in enumerate(match[2]):
            if char == '"':
                quoted = not quoted
            if not quoted:
                depth += (char == "(") - (char == ")")
                if char == "," and depth == 0:
                    pieces.append(match[2][start:index])
                    start = index + 1
        if match[2][start:].strip():
            pieces.append(match[2][start:])
        values = [_cfg(piece, features, test) for piece in pieces]
        if match[1] == "not":
            return not values[0] if len(values) == 1 and values[0] is not None else None
        if match[1] == "all":
            return False if False in values else (None if None in values else True)
        return True if True in values else (None if None in values else False)
    return None


def _attribute_contract(attribute, data: bytes, features: tuple[str, ...], test: bool):
    from .native.extract.extractor import Unsupported, _textual_verus_spec
    from .native.syntax import LexicalError, verus_spec_payload

    try:
        parts = verus_spec_payload(_text(data, attribute))
    except LexicalError:
        return None, True
    if parts is None:
        return None
    condition, _ = parts
    active = True if condition is None else _cfg(condition, features, test)
    if active is not True:
        return False, active
    try:
        parsed = _textual_verus_spec(attribute)
    except Unsupported:
        return None, active
    return bool(parsed and parsed[2]), active


def source_for_features(
    source: str, features: tuple[str, ...], *, test: bool = False,
) -> tuple[str, list[str]]:
    data = source.encode("utf-8")
    root = parser().parse(data).root_node
    masked = bytearray(data)
    diagnostics: list[str] = []

    def walk(node) -> None:
        if node.type in {"declaration_with_attrs", "attribute_expression"}:
            disabled = False
            for attribute in node.named_children:
                if attribute.type != "attribute_item":
                    continue
                contract = _attribute_contract(attribute, data, features, test)
                if contract is not None:
                    _, active = contract
                    if active is not True:
                        for index in range(attribute.start_byte, attribute.end_byte):
                            if masked[index] not in (10, 13):
                                masked[index] = 32
                        if active is None:
                            diagnostics.append(f"Unresolved contract cfg at line {attribute.start_point.row + 1}")
                    continue
                text = _text(data, attribute)
                match = re.fullmatch(r"#\[\s*cfg\s*\((.*)\)\s*\]", text, re.S)
                if match:
                    active = _cfg(match[1], features, test)
                    if active is None:
                        diagnostics.append(f"Unresolved cfg at line {attribute.start_point.row + 1}: {text}")
                    disabled = disabled or active is not True
                elif re.match(r"#\[\s*cfg_attr\b", text):
                    diagnostics.append(f"Unresolved cfg_attr at line {attribute.start_point.row + 1}")
                    disabled = True
            if disabled:
                for index in range(node.start_byte, node.end_byte):
                    if masked[index] not in (10, 13):
                        masked[index] = 32
                return
        for child in node.named_children:
            walk(child)

    walk(root)
    return masked.decode("utf-8"), diagnostics


def scan_source(
    source: str, relative: str, *, module: str = "", visibility: str = "all",
    features: tuple[str, ...] = (), test: bool = False,
) -> tuple[list[FunctionLocation], list[Diagnostic]]:
    data = source.encode("utf-8")
    tree = parser().parse(data)
    locations: list[FunctionLocation] = []
    diagnostics: list[Diagnostic] = []
    reported: set[tuple[str, int]] = set()
    source_hash = text_digest(source)

    def partial(node, reason: str) -> None:
        key = (reason, node.start_byte)
        if key not in reported:
            reported.add(key)
            diagnostics.append(Diagnostic(
                Stage.DISCOVER, "partial_discovery", reason, "warning",
                {"file": relative, "line": node.start_point.row + 1},
            ))

    def walk(node, modules: tuple[str, ...], owners: tuple[str, ...],
             anchor=None, in_verus: bool = False, trait_public: bool = False) -> None:
        if node.type == "ERROR" or node.is_missing:
            partial(node, "Parser recovery prevents complete target discovery")
        if node.type == "declaration_with_attrs":
            for attr in node.named_children:
                if attr.type != "attribute_item":
                    continue
                contract = _attribute_contract(attr, data, features, test)
                if contract is not None:
                    _, active = contract
                    if active is None:
                        partial(attr, "Conditional contract attribute is not resolved by the explicit build profile")
                    elif active:
                        partial(attr, "Contract attribute is parsed; macro expansion and import context are not part of source discovery")
                    continue
                value = _text(data, attr)
                if match := re.fullmatch(r"#\[\s*cfg\s*\((.*)\)\s*\]", value, re.S):
                    active = _cfg(match[1], features, test)
                    if active is False:
                        return
                    if active is None:
                        partial(attr, "Conditional compilation is not resolved by the explicit build profile")
                        return
                elif re.match(r"#\[\s*(cfg_attr|derive)\b", value):
                    partial(attr, "Attribute macro expansion is not included in source discovery")
                elif not re.match(
                    r"#\[\s*(?:verifier::|verus::|allow\b|warn\b|deny\b|forbid\b|"
                    r"doc\b|inline\b|repr\b|path\b|must_use\b|deprecated\b|cold\b|"
                    r"no_mangle\b|link_name\b|export_name\b)", value,
                ):
                    partial(attr, "Unknown attribute may change the set of declarations")
        if node.type == "verus_block":
            in_verus = True
        if node.type == "mod_item":
            name = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if body is None:
                body = next((c for c in node.named_children if c.type == "declaration_list"), None)
            if name is not None and body is not None:
                for child in body.named_children:
                    walk(child, (*modules, _text(data, name)), (), None, in_verus)
            return
        if node.type in {"impl_item", "trait_item"}:
            if node.type == "impl_item":
                ty = node.child_by_field_name("type")
                trait = node.child_by_field_name("trait")
                owner = _text(data, ty) if ty is not None else "<unresolved-impl>"
                if trait is not None:
                    owner = f"<{owner} as {_text(data, trait)}>"
            else:
                name = node.child_by_field_name("name")
                owner = f"trait {_text(data, name)}" if name is not None else "<unresolved-trait>"
            wrapper = node.parent if node.parent.type == "declaration_with_attrs" else node
            body = node.child_by_field_name("body")
            if body is not None:
                for child in body.named_children:
                    walk(child, modules, (*owners, owner), anchor or wrapper, in_verus,
                         node.type == "trait_item" and any(
                             c.type == "visibility_modifier" for c in node.named_children
                         ))
            return
        if node.type in {"function_item", "function_signature_item", "assume_specification_item"}:
            mode = next((c for c in node.named_children if c.type == "function_mode"), None)
            if mode is not None and _text(data, mode).strip() in {"spec", "proof"}:
                return
            is_public = trait_public or any(c.type == "visibility_modifier" for c in node.named_children)
            if visibility == "public" and not is_public:
                return
            name_node = node.child_by_field_name("name") or node.child_by_field_name("target")
            if name_node is None:
                partial(node, "Function declaration has no unambiguous source name")
                return
            raw_name = _text(data, name_node)
            if node.type == "assume_specification_item":
                from .native.extract.extractor import _function_item_name

                normalized_name = _function_item_name(node)
                if normalized_name is None:
                    partial(node, "External specification has no unambiguous target name")
                    return
                name = normalized_name.split("::")[-1]
            else:
                name = raw_name
            wrapper = node.parent if node.parent.type == "declaration_with_attrs" else node
            fn_token = next((c for c in node.children if c.type in {"fn", "assume_specification"}), node)
            line = fn_token.start_point.row + 1
            prefix = tuple(part for part in module.split("::") if part)
            qualified = "::".join((*prefix, *modules, *owners, raw_name))
            if owners:
                kind = "method_declaration" if node.type == "function_signature_item" else "method"
            else:
                kind = "contract_declaration" if node.type != "function_item" else "function"
            target = TargetRef(
                "verus", relative, name, line, f"{relative}::{qualified}", kind,
                source_hash, "::".join((*prefix, *modules)),
            )
            modifiers = next((c for c in node.named_children if c.type == "function_modifiers"), None)
            unsupported = ""
            body = node.child_by_field_name("body")

            def header_has_error(part) -> bool:
                if part == body:
                    return False
                return part.type == "ERROR" or part.is_missing or any(
                    header_has_error(child) for child in part.children
                )

            if header_has_error(wrapper):
                unsupported = "Target declaration contains parser recovery nodes"
            elif modifiers is not None and re.search(r"\basync\b", _text(data, modifiers)):
                unsupported = "Async executable contracts are not supported"
            elif node.has_error or wrapper.has_error:
                partial(node, "Implementation body has parser recovery; signature and contract remain source-parsed")
            qualifiers = [child for child in node.named_children if child.type == "fn_qualifier"]
            attribute_has_contract = False
            active_contracts = 0
            for attribute in wrapper.named_children:
                if attribute.type != "attribute_item":
                    continue
                attribute_contract = _attribute_contract(attribute, data, features, test)
                if attribute_contract is not None:
                    has_postconditions, active = attribute_contract
                    if active is None:
                        unsupported = "Conditional contract attribute has an unresolved compilation context"
                    elif active and has_postconditions is None:
                        unsupported = "Contract attribute cannot be parsed without recovery"
                    elif active:
                        active_contracts += 1
                        attribute_has_contract = attribute_has_contract or has_postconditions
            if active_contracts > 1:
                unsupported = "Multiple active contract attributes on one declaration"
            has_contract = (
                attribute_has_contract
                or
                any(child.type == "ensures_clause" for child in node.named_children)
                or any(
                    child.type == "ensures_clause"
                    for qualifier in qualifiers for child in qualifier.named_children
                )
            )
            locations.append(FunctionLocation(
                target, wrapper.start_byte, wrapper.end_byte,
                (anchor or wrapper).end_byte, in_verus,
                has_contract,
                node.type != "function_item", unsupported, node.start_point.row + 1,
                raw_name,
            ))
            if unsupported:
                partial(node, unsupported)
            return
        if node.type in {"macro_invocation", "macro_definition"}:
            partial(node, "Item macro expansion is not included in source discovery")
            return
        for child in node.named_children:
            walk(child, modules, owners, anchor, in_verus, trait_public)

    walk(tree.root_node, (), ())
    if tree.root_node.has_error and not any("Parser recovery" in d.message for d in diagnostics):
        partial(tree.root_node, "Parser recovery prevents complete target discovery")
    return locations, diagnostics


def project_modules(
    config: Config, project: PreparedProject, read_source: Callable[[str], str],
) -> tuple[dict[str, str], list[Diagnostic]]:
    if config.build.adapter == "verus.single_file":
        return {}, []
    entry = config.build.entrypoint
    if not entry and config.build.adapter == "verus.cargo":
        for candidate in ("src/lib.rs", "src/main.rs"):
            if candidate in project.files:
                entry = candidate
                break
    if not entry or entry not in project.files:
        return {}, [Diagnostic(
            Stage.DISCOVER, "partial_discovery",
            "An explicit crate entrypoint is needed to resolve module identities", "warning",
        )]
    modules: dict[str, str] = {}
    diagnostics: list[Diagnostic] = []

    def visit(relative: str, prefix: tuple[str, ...], directory: PurePosixPath) -> None:
        qualified = "::".join(prefix)
        if relative in modules:
            if modules[relative] != qualified:
                diagnostics.append(Diagnostic(
                    Stage.DISCOVER, "ambiguous_module",
                    "A source file is included under multiple module identities", "warning",
                    {"file": relative},
                ))
            return
        modules[relative] = qualified
        data = read_source(relative).encode("utf-8")
        root = parser().parse(data).root_node

        def walk(node, parts: tuple[str, ...], child_dir: PurePosixPath) -> None:
            if node.type in {"declaration_with_attrs", "attribute_expression"}:
                for attr in node.named_children:
                    if attr.type == "attribute_item":
                        text = _text(data, attr)
                        match = re.fullmatch(r"#\[\s*cfg\s*\((.*)\)\s*\]", text, re.S)
                        if match and _cfg(match[1], config.build.features, "--test" in config.build.extra_args) is not True:
                            return
            if node.type == "mod_item":
                name = node.child_by_field_name("name")
                if name is None:
                    return
                value = _text(data, name)
                body = node.child_by_field_name("body")
                if body is not None:
                    for child in body.named_children:
                        walk(child, (*parts, value), child_dir / value)
                    return
                wrapper = node.parent if node.parent.type == "declaration_with_attrs" else node
                attributes = " ".join(
                    _text(data, c) for c in wrapper.named_children if c.type == "attribute_item"
                )
                path_attr = re.search(r'#\[\s*path\s*=\s*"([^"]+)"\s*\]', attributes)
                if path_attr:
                    paths = [child_dir / path_attr[1]]
                else:
                    paths = [child_dir / f"{value}.rs", child_dir / value / "mod.rs"]
                found = [p.as_posix() for p in paths if p.as_posix() in project.files and ".." not in p.parts]
                if len(found) != 1:
                    diagnostics.append(Diagnostic(
                        Stage.DISCOVER, "partial_discovery",
                        "External module cannot be resolved uniquely from the project snapshot", "warning",
                        {"file": relative, "module": "::".join((*parts, value))},
                    ))
                else:
                    resolved = PurePosixPath(found[0])
                    directory = resolved.parent if resolved.name == "mod.rs" else resolved.with_suffix("")
                    visit(found[0], (*parts, value), directory)
                return
            if node.type == "macro_invocation":
                macro = node.child_by_field_name("macro")
                if macro is None or _text(data, macro) != "include":
                    return
                tokens = next((c for c in node.named_children if c.type == "token_tree"), None)
                arguments = [
                    c for c in tokens.named_children
                    if c.type not in {"line_comment", "block_comment"}
                ] if tokens is not None else []
                literal = _text(data, arguments[0]) if len(arguments) == 1 else ""
                match = re.fullmatch(r'"([^"\\]+)"', literal)
                included = posixpath.normpath(
                    str(PurePosixPath(relative).parent / match[1])
                ) if match else ""
                if not included or included not in project.files or included.startswith(("/", "../")):
                    diagnostics.append(Diagnostic(
                        Stage.DISCOVER, "partial_discovery",
                        "Only literal include! paths within the source snapshot are supported", "warning",
                        {"file": relative, "line": node.start_point.row + 1},
                    ))
                else:
                    visit(included, parts, PurePosixPath(included).parent)
                return
            if node.type not in {"function_item", "impl_item", "trait_item", "macro_invocation"}:
                for child in node.named_children:
                    walk(child, parts, child_dir)

        walk(root, prefix, directory)

    visit(entry, (), PurePosixPath(entry).parent)
    return modules, diagnostics


def discover_project(
    config: Config, project: PreparedProject, read_source: Callable[[str], str],
) -> tuple[list[FunctionLocation], list[Diagnostic]]:
    modules, diagnostics = project_modules(config, project, read_source)
    locations: list[FunctionLocation] = []
    for path in input_files(config, project):
        relative = path.relative_to(project.root).as_posix()
        if config.build.adapter != "verus.single_file" and relative not in modules:
            diagnostics.append(Diagnostic(
                Stage.DISCOVER, "partial_discovery",
                "Source file is not reachable from the configured crate entrypoint", "warning",
                {"file": relative},
            ))
            continue
        found, gaps = scan_source(
            read_source(relative), relative, module=modules.get(relative, ""),
            visibility=config.visibility, features=config.build.features,
            test="--test" in config.build.extra_args,
        )
        locations.extend(found)
        diagnostics.extend(gaps)
    return locations, diagnostics
