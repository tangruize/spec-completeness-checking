"""
Module 1: extract — Spec Extraction

Uses tree-sitter-verus for structured parsing of Verus function specs.
Handles both:
  - #[verus_spec(...)] attribute-level specs
  - Inline fn_qualifier (requires/ensures) inside verus! {} blocks
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

import tree_sitter as ts
import tree_sitter_verus as tsv

from .types import (
    TypeKind, TypeInfo, FieldInfo, VariantInfo,
    Param, FunctionSpec,
)
from .attrs import (
    ItemAttrs,
    collect_item_attrs_from_tree,
    parse_item_attrs,
    propagate_attrs_to_type_defs,
)
from ..syntax import mask_noncode

logger = logging.getLogger(__name__)

_lang = ts.Language(tsv.language())
_parser = ts.Parser(_lang)


# ---------------------------------------------------------------------------
# Tree-sitter helpers
# ---------------------------------------------------------------------------

def _children_by_type(node: ts.Node, *types: str) -> list[ts.Node]:
    return [c for c in node.children if c.type in types]


def _child_by_type(node: ts.Node, *types: str) -> Optional[ts.Node]:
    for c in node.children:
        if c.type in types:
            return c
    return None


def _text(node: ts.Node) -> str:
    return node.text.decode()


# ---------------------------------------------------------------------------
# Type extraction from AST nodes
# ---------------------------------------------------------------------------

PRIMITIVE_MAP = {
    "usize": TypeKind.USIZE, "isize": TypeKind.ISIZE,
    "u8": TypeKind.U8, "u16": TypeKind.U16, "u32": TypeKind.U32, "u64": TypeKind.U64,
    "i8": TypeKind.I8, "i16": TypeKind.I16, "i32": TypeKind.I32, "i64": TypeKind.I64,
    "int": TypeKind.INT, "nat": TypeKind.INT,
    "bool": TypeKind.BOOL, "()": TypeKind.UNIT,
    "str": TypeKind.STR, "String": TypeKind.STR,
}

_KNOWN_GENERICS = {
    "Result": TypeKind.RESULT, "Option": TypeKind.OPTION,
    "Set": TypeKind.SET, "Seq": TypeKind.SEQ, "Map": TypeKind.MAP,
    # PR-F (A-1): vstd ghost/proof wrappers — recognise them so narrow
    # can project through `@` / `.value()` instead of degrading to UNKNOWN.
    "Tracked": TypeKind.TRACKED, "Ghost": TypeKind.GHOST,
    "PointsTo": TypeKind.POINTS_TO,
    # ISSUES #14 — Vec<T> is a Seq<T> at the spec level (vstd's
    # `impl<T> View for Vec<T> { type V = Seq<T>; }`), but accesses
    # require the `@` projection in spec contexts (verusage / vest's
    # invariants use `data@[k]` / `data@.len()` throughout). We pin
    # the SEQ kind here and tag the TypeInfo with a spec_view marker
    # in the generic_type branch below so narrow_seq / schemas know
    # to emit `var@[i]` / `var@.len()` rather than `var[i]` / `var.len()`.
    "Vec": TypeKind.SEQ,
}


def _parse_type_node(node: ts.Node) -> TypeInfo:
    """Convert a tree-sitter type node into TypeInfo."""
    if node.type == "primitive_type":
        name = _text(node)
        return TypeInfo(kind=PRIMITIVE_MAP.get(name, TypeKind.UNKNOWN), name=name)

    if node.type == "unit_type":
        return TypeInfo(kind=TypeKind.UNIT, name="()")

    if node.type == "type_identifier":
        name = _text(node)
        return TypeInfo(kind=PRIMITIVE_MAP.get(name, TypeKind.UNKNOWN), name=name)

    if node.type == "generic_type":
        name_node = (_child_by_type(node, "type_identifier")
                     or _child_by_type(node, "scoped_type_identifier"))
        args_node = _child_by_type(node, "type_arguments")
        name = _text(name_node) if name_node else _text(node)
        type_args = []
        if args_node:
            for c in args_node.children:
                if c.type not in ("<", ">", ","):
                    type_args.append(_parse_type_node(c))

        # PR-F: tolerate fully-qualified vstd paths like
        # `vstd::pcell::Tracked<T>` by stripping any scope prefix and
        # any stray `<…>` suffix before lookup.
        short = name.rsplit("::", 1)[-1].split("<", 1)[0]
        kind = _KNOWN_GENERICS.get(short, TypeKind.UNKNOWN)
        info = TypeInfo(kind=kind, name=_text(node), type_args=type_args)

        if kind == TypeKind.RESULT and len(type_args) >= 2:
            info.variants = [VariantInfo("Ok", type_args[0]),
                             VariantInfo("Err", type_args[1])]
        elif kind == TypeKind.OPTION and len(type_args) >= 1:
            info.variants = [VariantInfo("Some", type_args[0]),
                             VariantInfo("None")]
        # ISSUES #14 — Vec<T> needs `@` to project at the spec level.
        # Tag the TypeInfo with a synthetic Seq<T> spec_view so narrow
        # and schemas insert the `@` accessor. Native Verus Seq<T> /
        # Set<T> / Map<…> stay spec_view=None — their bare-identifier
        # access works as-is in spec contexts.
        elif short == "Vec" and type_args:
            inner = type_args[0]
            info.spec_view = TypeInfo(
                kind=TypeKind.SEQ,
                name=f"Seq<{inner.name}>",
                type_args=[inner],
            )
        return info

    if node.type == "scoped_type_identifier":
        return TypeInfo(kind=TypeKind.UNKNOWN, name=_text(node))

    if node.type == "reference_type":
        inner = node.children[-1]
        return _parse_type_node(inner)

    # ISSUES #14 — tuple types `(A, B, ...)`. Without this branch the
    # extractor punts to UNKNOWN, which leaves every tuple-typed param
    # / return value opaque to the narrow + schema pipeline (witnesses
    # for fns returning tuples collapse to a single distinctness assume,
    # never instantiating per-position values).
    #
    # We model an n-tuple as a STRUCT whose fields are named "0", "1",
    # ... — that matches Rust/Verus's positional field-access syntax
    # (`t.0`, `t.1`), so the existing `narrow_struct` strategy and the
    # STRUCT branch of `schemas._emit` produce correct per-position
    # accessors with zero changes downstream.
    # ISSUES #14 — array types `[T; N]` (fixed-size) and `[T]` (slice).
    # Without this branch the extractor punts arrays to UNKNOWN, leaving
    # any field/param typed as a fixed array opaque to narrow / schemas
    # (e.g. self_.mask: [usize; 8] on memory-allocator::next_run — the
    # struct narrow recurses into mask, finds UNKNOWN, and stops, so
    # self's state is never instantiated even though witness validity
    # depends on it).
    #
    # Verus accepts direct array indexing `arr[i]` and `arr.len()` in
    # spec contexts (same accessor as Seq<T>; verified against
    # verusage/memory-allocator's use of self.mask[i]), so we model
    # `[T; N]` and `[T]` as TypeKind.SEQ with type_args=[T]. The existing
    # narrow_seq / schemas SEQ branch then enumerate per-index schemas
    # `arr.len()`, `arr[0]`, `arr[1]`, ... up to MAX_SEQ_LEN with zero
    # downstream changes. The static size N is recovered at search time
    # via Verus rejecting `arr.len() != N` probes.
    if node.type == "array_type":
        elem_nodes = [c for c in node.children
                      if c.type not in ("[", "]", ";")
                      and c.type != "integer_literal"]
        if not elem_nodes:
            return TypeInfo(kind=TypeKind.UNKNOWN, name=_text(node))
        elem_info = _parse_type_node(elem_nodes[0])
        return TypeInfo(
            kind=TypeKind.SEQ,
            name=_text(node),
            type_args=[elem_info],
        )

    if node.type == "tuple_type":
        elem_nodes = [c for c in node.children if c.type not in ("(", ")", ",")]
        if not elem_nodes:
            return TypeInfo(kind=TypeKind.UNIT, name="()")
        field_types = [_parse_type_node(e) for e in elem_nodes]
        return TypeInfo(
            kind=TypeKind.STRUCT,
            name=_text(node),
            fields=[FieldInfo(name=str(i), type=t)
                    for i, t in enumerate(field_types)],
        )

    # Fallback
    return TypeInfo(kind=TypeKind.UNKNOWN, name=_text(node))


# ---------------------------------------------------------------------------
# Parameter extraction
# ---------------------------------------------------------------------------

def _extract_params(params_node: ts.Node) -> list[Param]:
    """Extract parameters from a `parameters` AST node."""
    result = []
    for child in params_node.children:
        if child.type == "self_parameter":
            has_mut = _child_by_type(child, "mutable_specifier") is not None
            has_ref = any(c.type == "&" for c in child.children)
            result.append(Param(
                name="self",
                type=TypeInfo(kind=TypeKind.UNKNOWN, name="Self"),
                is_mut_ref=has_ref and has_mut,
                is_ref=has_ref,
                is_self=True,
            ))
        elif child.type == "parameter":
            name_node = _child_by_type(child, "identifier")
            param_name: Optional[str] = _text(name_node) if name_node else None
            child_text = _text(child).lstrip()
            explicit_self = bool(re.match(r"(?:mut\s+)?self\s*:", child_text))
            # Type is the child after ":"
            type_node = None
            after_colon = False
            for c in child.children:
                if c.type == ":":
                    after_colon = True
                elif after_colon and c.type not in (",",):
                    type_node = c
            # Verus exec fns often use destructure patterns to unwrap ghost
            # carriers, e.g. ``Tracked(perm): Tracked<PagePerm>`` or
            # ``Ghost(g): Ghost<Seq<T>>``. The AST emits a
            # ``tuple_struct_pattern`` in lieu of a top-level identifier;
            # dig out the inner binding name so the synthesized det fn
            # gets a real parameter name (and not ``?``).
            destructure_ctor: Optional[str] = None
            if param_name is None:
                tup = _child_by_type(child, "tuple_struct_pattern")
                if tup is not None:
                    # The identifier preceding ``(`` is the ctor; the first
                    # identifier *inside* ``(`` is the inner binding name.
                    seen_open = False
                    ctor_name: Optional[str] = None
                    for c in tup.children:
                        if c.type == "(":
                            seen_open = True
                        elif not seen_open and c.type == "identifier":
                            ctor_name = _text(c)
                        elif seen_open and c.type == "identifier":
                            param_name = _text(c)
                            break
                    if ctor_name in ("Ghost", "Tracked"):
                        destructure_ctor = ctor_name
            # Detect &mut on the type: reference_type with mutable_specifier
            is_ref = type_node is not None and type_node.type == "reference_type"
            is_mut = is_ref and _child_by_type(type_node, "mutable_specifier") is not None
            # For ``Tracked(p): Tracked<&mut T>`` / ``Ghost(p): Ghost<&mut T>``
            # destructure patterns, the outer ``type_node`` is ``generic_type``,
            # so the above checks return False even though the binding ``p`` is
            # in fact a ``&mut T``. Peek inside the ``type_arguments`` for a
            # ``reference_type`` with ``mutable_specifier`` so downstream
            # ``is_mut_ref`` consumers (gen_det, _substitute_input,
            # _substitute_run) see the binding as a mut-ref.
            if (destructure_ctor in ("Ghost", "Tracked")
                    and type_node is not None
                    and type_node.type == "generic_type"
                    and not is_mut):
                ta_node = _child_by_type(type_node, "type_arguments")
                if ta_node is not None:
                    for tac in ta_node.children:
                        if tac.type == "reference_type":
                            is_ref = True
                            if _child_by_type(tac, "mutable_specifier") is not None:
                                is_mut = True
                            break
            param_type = _parse_type_node(type_node) if type_node else TypeInfo(kind=TypeKind.UNKNOWN, name="?")
            # Verus ``Ghost(name): Ghost<T>`` / ``Tracked(name): Tracked<T>``
            # destructure: inside the function body, ``name`` has type ``T``
            # (not the wrapper). Requires/ensures clauses + probe enumeration
            # all reference ``name`` as if it has type ``T``; record the inner
            # type so downstream consumers see the unwrapped form. ``destructure_ctor``
            # is preserved on the param for telemetry, but gen_det no longer
            # needs to re-wrap the synth fn signature — using the inner type
            # directly type-checks because the synth fn is never called from
            # source code.
            if destructure_ctor in ("Ghost", "Tracked") and param_type.type_args:
                inner = param_type.type_args[0]
                param_type = inner
            result.append(Param(
                name="self" if explicit_self else (param_name if param_name else "?"),
                type=param_type,
                is_mut_ref=is_mut,
                is_ref=is_ref,
                is_self=explicit_self,
                destructure_ctor=destructure_ctor,
            ))
    return result


# ---------------------------------------------------------------------------
# Return type extraction
# ---------------------------------------------------------------------------

def _extract_return_type(fn_node: ts.Node) -> tuple[TypeInfo, Optional[str]]:
    """
    Extract return type and optional result binding name.
    Returns (TypeInfo, binding_name_or_None).
    
    Handles both:
      -> Result<usize, Error>                    (no binding)
      -> (result: Result<usize, Error>)          (named return)
    """
    ret_node = _child_by_type(fn_node, "named_return_type")
    if ret_node is None:
        return TypeInfo(kind=TypeKind.UNIT, name="()"), None

    # Named return: (name: Type)
    id_node = _child_by_type(ret_node, "identifier")
    binding_name = _text(id_node) if id_node else None

    # Find the type node — permissively pick the first child that is not a
    # syntactic token or the binding identifier. tree-sitter-verus emits a
    # variety of node kinds for the type position (reference_type, tuple_type,
    # array_type, function_type, ...); enumerating them is fragile, so we
    # exclude the known non-type children instead.
    _SYNTACTIC = {"(", ")", ":", "identifier"}
    type_node = None
    for child in ret_node.children:
        if child.type not in _SYNTACTIC:
            type_node = child
            break
    if type_node:
        info = _parse_type_node(type_node)
        # Preserve `&`/`&mut ` for return types: gen_det renders return_type.name
        # verbatim and there's no separate `is_ref` channel for returns. Param
        # extraction tracks is_ref separately so this branch stays return-only.
        if type_node.type == "reference_type":
            is_mut = any(c.type == "mutable_specifier" for c in type_node.children)
            prefix = "&mut " if is_mut else "&"
            if not info.name.startswith("&"):
                info = TypeInfo(
                    kind=info.kind,
                    name=f"{prefix}{info.name}",
                    fields=info.fields,
                    variants=info.variants,
                    type_args=info.type_args,
                    spec_view=info.spec_view,
                )
        return info, binding_name

    return TypeInfo(kind=TypeKind.UNKNOWN, name=_text(ret_node)), binding_name


# ---------------------------------------------------------------------------
# Requires/ensures clause extraction
# ---------------------------------------------------------------------------

def _extract_clauses(fn_qualifier: ts.Node) -> tuple[list[str], list[str]]:
    """Extract requires and ensures clause texts from an fn_qualifier node."""
    requires = []
    ensures = []
    for child in fn_qualifier.children:
        if child.type == "requires_clause":
            requires.extend(_clause_expressions(child))
        elif child.type == "ensures_clause":
            ensures.extend(_clause_expressions(child))
    return requires, ensures


def _clause_expressions(clause_node: ts.Node) -> list[str]:
    """Extract expression text from a requires_clause or ensures_clause node."""
    exprs = []
    for child in clause_node.children:
        if child.type in ("requires", "ensures", ",", "line_comment", "block_comment"):
            continue
        exprs.append(_text(child))
    return exprs


# ---------------------------------------------------------------------------
# Find function + spec in parsed tree
# ---------------------------------------------------------------------------

def _is_inside_block_comment(source: str, pos: int) -> bool:
    """Return True iff ``pos`` falls inside a ``/* ... */`` block comment.

    Naive scan from the start of ``source`` tracking ``/*`` / ``*/``
    pairs. Treats line comments (``// ...``) and string / char literals
    as comment-transparent (they CAN'T contain ``/*`` markers in
    well-formed Rust because ``//`` consumes to end-of-line and Verus's
    grammar disallows ``/*`` mid-string in normal positions; this is
    sufficient for the corpus we operate on).
    """
    i = 0
    depth = 0
    n = len(source)
    while i < pos and i < n - 1:
        if source[i] == '/' and source[i + 1] == '*':
            depth += 1
            i += 2
            continue
        if source[i] == '*' and source[i + 1] == '/' and depth > 0:
            depth -= 1
            i += 2
            continue
        # Skip over line comments to avoid `/*` substrings inside them
        # accidentally opening a block comment in our scan.
        if depth == 0 and source[i] == '/' and source[i + 1] == '/':
            nl = source.find('\n', i + 2)
            i = nl + 1 if nl >= 0 else n
            continue
        i += 1
    return depth > 0


def _extract_fn_chunk(
    source: str,
    fn_name: str,
    source_line: Optional[int] = None,
) -> tuple[str, Optional[ts.Tree]]:
    """
    Extract a source chunk containing #[verus_spec(...)] + fn definition,
    and re-parse it in isolation for a cleaner AST.
    
    Returns (chunk_text, parsed_tree) or ("", None).
    """
    import re
    # Find `pub fn <name>` in source
    fn_pattern = re.compile(
        rf'(?:pub(?:\s*\([^)]*\))?\s+)?(?:const\s+|async\s+|unsafe\s+|extern(?:\s+"[^"]*")?\s+)*fn\s+{re.escape(fn_name)}\s*(?:<[^>]*>)?\s*\(',
    )
    # Iterate through matches; skip any that fall inside a `/* ... */`
    # block comment. Without this guard, fns commented out via block
    # comments (atmosphere's ``va_2m_valid`` / ``va_1g_valid``) are
    # extracted as live targets, then gen_det synthesises calls to
    # ``spec_va_2m_valid`` / ``spec_va_1g_valid`` that don't exist in
    # the current source.
    candidates = []
    for candidate in fn_pattern.finditer(mask_noncode(source)):
        candidates.append(candidate)
    if source_line is not None and candidates:
        candidates = [
            candidate for candidate in candidates
            if source.count("\n", 0, candidate.start()) + 1 == source_line
        ]
    if len(candidates) > 1:
        raise Unsupported(f"Ambiguous source recovery for '{fn_name}'; supply an exact source_line")
    m = candidates[0] if candidates else None
    if not m:
        return "", None

    fn_start = m.start()

    # Walk backwards to find the verus_spec attribute (if any)
    chunk_start = fn_start
    prefix = source[:fn_start].rstrip()
    if prefix.endswith(')]'):
        # Find the matching #[verus_spec or #[cfg_attr(... verus_spec
        bracket_depth = 0
        i = len(prefix) - 1
        while i >= 0:
            if prefix[i] == ']':
                bracket_depth += 1
            elif prefix[i] == '[':
                bracket_depth -= 1
                if bracket_depth == 0:
                    # Check for # before [
                    if i > 0 and prefix[i - 1] == '#':
                        chunk_start = i - 1
                    else:
                        chunk_start = i
                    break
            i -= 1

    # Walk forward from fn_start to find the opening brace of the body
    body_open = None
    for i in range(fn_start, len(source)):
        if source[i] == '{':
            body_open = i
            break

    if body_open is None:
        return "", None

    # Walk forward to find end of body (text-level brace matching; may be
    # wrong in the presence of `proof!{}` / macros — we accept that because
    # we discard the body below).
    depth = 0
    fn_end = len(source)
    for i in range(body_open, len(source)):
        if source[i] == '{':
            depth += 1
        elif source[i] == '}':
            depth -= 1
            if depth == 0:
                fn_end = i + 1
                break

    chunk = source[chunk_start:fn_end]
    chunk_tree = _parser.parse(chunk.encode())

    # If the body contains constructs tree-sitter can't parse (e.g. `proof!`,
    # `cfg_attr(..., verus_spec(...))` invariants), the root can end up as
    # ERROR and no function_item is recognised. Retry with the body stubbed
    # out — we only need the signature + attribute for spec extraction.
    if chunk_tree.root_node.has_error:
        stub = source[chunk_start:body_open] + "{}"
        stub_tree = _parser.parse(stub.encode())
        if not stub_tree.root_node.has_error or \
           _find_function_items(stub_tree):
            return stub, stub_tree

    return chunk, chunk_tree

def _find_function_items(tree: ts.Tree) -> list[ts.Node]:
    """Find definitions, bodyless signatures and external specification items."""
    results = []

    def walk(node: ts.Node):
        if node.type in {
            "function_item", "function_signature_item", "assume_specification_item",
        }:
            results.append(node)
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return results


def _function_item_name(node: ts.Node) -> Optional[str]:
    """Return a declared name, or the qualified external target without turbofish."""
    target = node.child_by_field_name("target")
    if target is not None:
        while target.type == "generic_function":
            function = target.child_by_field_name("function")
            if function is None:
                return None
            target = function
        return _text(target)
    name = node.child_by_field_name("name") or _child_by_type(node, "identifier")
    return _text(name) if name is not None else None


def _matches_function_item(node: ts.Node, name: str) -> bool:
    declared = _function_item_name(node)
    if declared == name:
        return True
    target = node.child_by_field_name("target")
    if target is not None and _text(target) == name:
        return True
    if node.type != "assume_specification_item" or "::" in name:
        return False
    return declared is not None and declared.rsplit("::", 1)[-1] == name


def _find_impl_type(fn_node: ts.Node, tree: ts.Tree, source: str,
                    fn_name: Optional[str] = None) -> Optional[str]:
    """Find the impl type name for a function.
    
    Strategy:
      1. Walk up parent chain looking for impl_item
      2. Find impl_item nodes by byte range
      3. Scan top-level tokens for `impl` → `type_identifier` → `{` ... `}` pattern
         and check containment (by byte range, or by fn_name text search)
    """
    fn_start = fn_node.start_byte
    fn_end = fn_node.end_byte

    # Strategy 1: walk up parent chain
    node = fn_node.parent
    while node is not None:
        if node.type == "impl_item":
            ty = _child_by_type(node, "type_identifier")
            if ty:
                return _text(ty)
        node = node.parent

    # Strategy 2: find enclosing impl_item by byte range
    best = None
    for impl_node in _find_all_nodes(tree.root_node, "impl_item"):
        if impl_node.start_byte <= fn_start and impl_node.end_byte >= fn_end:
            if best is None or (impl_node.end_byte - impl_node.start_byte) < (best.end_byte - best.start_byte):
                best = impl_node
    if best:
        ty = _child_by_type(best, "type_identifier")
        if ty:
            return _text(ty)

    # Strategy 3: scan for flat `impl` → `type_identifier` → `{` ... `}` pattern
    root = tree.root_node
    children = root.children
    for i, child in enumerate(children):
        if child.type == "impl" and child.child_count == 0:
            if i + 2 < len(children) and children[i + 1].type == "type_identifier":
                type_name = _text(children[i + 1])
                if children[i + 2].type != "{":
                    continue
                brace_start = children[i + 2].start_byte
                # Find matching `}` by scanning forward
                depth = 0
                impl_end = len(source)
                for j in range(i + 2, len(children)):
                    if children[j].type == "{" and children[j].child_count == 0:
                        depth += 1
                    elif children[j].type == "}" and children[j].child_count == 0:
                        depth -= 1
                        if depth == 0:
                            impl_end = children[j].end_byte
                            break

                # Check containment: either by byte range (same tree)
                # or by fn_name text search in the impl span (for chunk-parsed fns)
                if brace_start <= fn_start and impl_end >= fn_end:
                    return type_name
                if fn_name:
                    impl_text = source[brace_start:impl_end]
                    import re
                    if re.search(rf'\bfn\s+{re.escape(fn_name)}\b', impl_text):
                        return type_name

    return None


def _find_verus_spec_for_fn(
    fn_node: ts.Node,
    tree: ts.Tree,
) -> Optional[tuple[Optional[str], ts.Node]]:
    """
    Find verus_spec attribute associated with a function.
    Returns (result_binding_name, fn_qualifier_node) or None.
    
    Strategy:
      1. Check sibling attribute_items in the same declaration_with_attrs
      2. Fallback: find nearest verus_spec_attribute by byte proximity
         (handles cases where parse errors break the parent chain)
    """
    if fn_node.parent is not None and fn_node.parent.type == "declaration_with_attrs":
        return _find_verus_spec_sibling(fn_node)

    # Fallback: search all verus_spec_attribute nodes in the tree
    # and find the one immediately before this function
    all_attrs = _find_all_nodes(tree.root_node, "verus_spec_attribute")
    best = None
    for attr in all_attrs:
        if attr.end_byte <= fn_node.start_byte:
            if best is None or attr.end_byte > best.end_byte:
                best = attr
    if best is None:
        return None

    # Only accept if the gap is small (whitespace/comments only)
    gap = fn_node.start_byte - best.end_byte
    if gap > 200:
        return None

    return _extract_from_verus_spec_attr_node(best)


def _find_verus_spec_sibling(fn_node: ts.Node) -> Optional[tuple[Optional[str], ts.Node]]:
    """Try to find verus_spec as a sibling attribute in declaration_with_attrs."""
    parent = fn_node.parent
    if parent is None or parent.type != "declaration_with_attrs":
        return None

    for sibling in parent.children:
        if sibling.type != "attribute_item":
            continue
        spec_attr = _child_by_type(sibling, "verus_spec_attribute")
        if spec_attr is None:
            continue
        result = _extract_from_verus_spec_attr_node(spec_attr)
        if result:
            return result

    return None


def _extract_from_verus_spec_attr_node(
    spec_attr: ts.Node,
) -> Optional[tuple[Optional[str], ts.Node]]:
    """Extract (binding_name, fn_qualifier) from a verus_spec_attribute node."""
    # Find verus_spec_attr (could be direct or inside cfg_attr_verus_spec)
    vs_attr = _child_by_type(spec_attr, "verus_spec_attr")
    if vs_attr is None:
        cfg = _child_by_type(spec_attr, "cfg_attr_verus_spec")
        if cfg:
            vs_attr = _child_by_type(cfg, "verus_spec_attr")
    if vs_attr is None:
        return None

    binding = None
    id_node = _child_by_type(vs_attr, "identifier")
    if id_node:
        binding = _text(id_node)

    fq = _child_by_type(vs_attr, "fn_qualifier")
    if fq:
        return binding, fq

    return None


def _parse_cfg_feature_req(attr_node: ts.Node) -> Optional[str]:
    """
    If `attr_node` is `#[cfg(feature = "X")]`, return "X". Otherwise None
    (including for cfg expressions we don't understand — caller treats
    None conservatively, i.e. does NOT drop the guarded item).
    """
    if attr_node.type != "attribute_item":
        return None
    attr = _child_by_type(attr_node, "attribute")
    if attr is None:
        return None
    ident = _child_by_type(attr, "identifier")
    if ident is None or _text(ident) != "cfg":
        return None
    tt = _child_by_type(attr, "token_tree")
    if tt is None:
        return None
    # Expect shape: ( feature = "X" )
    inner = [c for c in tt.children if c.type not in ("(", ")")]
    if len(inner) != 3:
        return None
    if inner[0].type != "identifier" or _text(inner[0]) != "feature":
        return None
    if inner[1].type != "=":
        return None
    if inner[2].type != "string_literal":
        return None
    sc = _child_by_type(inner[2], "string_content")
    return _text(sc) if sc is not None else None


def _is_cfg_excluded(
    preceding_attrs: list[ts.Node],
    active_features: Optional[set[str]],
) -> bool:
    """
    True iff any of the preceding `#[cfg(feature = "X")]` attributes is
    for a feature not in active_features. active_features=None disables
    filtering entirely (back-compat). Unrecognized cfg shapes are treated
    conservatively (not excluded).
    """
    if active_features is None:
        return False
    for a in preceding_attrs:
        req = _parse_cfg_feature_req(a)
        if req is not None and req not in active_features:
            return True
    return False


def _find_all_nodes(node: ts.Node, target_type: str) -> list[ts.Node]:
    """Find all nodes of a given type in the tree."""
    results = []
    if node.type == target_type:
        results.append(node)
    for child in node.children:
        results.extend(_find_all_nodes(child, target_type))
    return results


# ---------------------------------------------------------------------------
# Struct / enum extraction
# ---------------------------------------------------------------------------

def _parent_item_attrs(parent_node: ts.Node, item_node: ts.Node) -> ItemAttrs:
    """Collect ``attribute_item`` siblings that precede ``item_node`` inside
    a ``declaration_with_attrs`` parent, and parse them into an
    :class:`ItemAttrs`.

    tree-sitter-verus wraps annotated items in a ``declaration_with_attrs``
    node containing ``attribute_item`` siblings preceding the actual
    ``struct_item`` / ``enum_item`` / ``function_item``; this helper
    inspects those siblings.

    Returns the all-defaults ``ItemAttrs`` when ``parent_node`` is not a
    ``declaration_with_attrs`` (i.e. the item has no attribute sidecar).
    Parsing — including recognition of ``verifier::external_body``,
    ``verifier::ext_equal`` (but NOT the unrelated ``auto_ext_equal(N)``
    attribute), ``cfg(feature = ...)``, and ``derive(...)`` — lives in
    :mod:`specdet.adapters.verus.native.extract.attrs` so the registry side can share
    the exact same recogniser.
    """
    if parent_node.type != "declaration_with_attrs":
        return ItemAttrs()
    nodes: list[ts.Node] = []
    for c in parent_node.children:
        if c is item_node:
            break
        if c.type == "attribute_item":
            nodes.append(c)
    return parse_item_attrs(nodes)


def find_ext_equal_type_names(sources: list[str]) -> set[str]:
    """Public helper kept for backward compatibility — returns the set of
    bare type names whose declaration carries ``#[verifier::ext_equal]``
    / ``#[verifier(ext_equal)]`` across all ``sources``.

    New code should prefer :func:`find_item_attrs`, which returns the
    full structured :class:`ItemAttrs` for every recognised type at once
    (a single source scan, multiple attributes harvested).
    """
    out: set[str] = set()
    for name, attrs in find_item_attrs(sources).items():
        if attrs.is_ext_equal:
            out.add(name)
    return out


def find_item_attrs(sources: list[str]) -> dict[str, ItemAttrs]:
    """Parse each ``source`` string with the verus tree-sitter grammar and
    return the union ``{bare_name: ItemAttrs}`` for every reachable
    ``struct_item`` / ``enum_item`` wrapped in a
    ``declaration_with_attrs``. Duplicate names across sources are
    OR-merged.

    Used by the Tier 1.5 runner's post-pass: cached LLM type-completion
    patches don't carry attribute provenance, so we re-derive it
    directly from the project source. The returned table feeds
    :func:`specdet.adapters.verus.native.extract.attrs.propagate_attrs_to_type_defs`.
    """
    from .attrs import _merge  # noqa: PLC0415

    out: dict[str, ItemAttrs] = {}
    for src in sources:
        # tree-sitter-rust trips on inner attributes; strip them so the
        # outer ``struct_item`` / ``enum_item`` is reachable. Mirrors the
        # cleanup ``_resolve_types`` does for the same reason.
        cleaned = re.sub(r'#!\[[^\]]*\]', '', src)
        tree = _parser.parse(cleaned.encode())
        table = collect_item_attrs_from_tree(tree)
        for name, attrs in table.items():
            if name in out:
                _merge(out[name], attrs)
            else:
                out[name] = attrs
    return out


def _find_struct(
    tree: ts.Tree,
    name: str,
    active_features: Optional[set[str]] = None,
) -> Optional[TypeInfo]:
    """Find a struct definition by name and extract its fields."""
    def walk(node: ts.Node, parent: Optional[ts.Node] = None) -> Optional[TypeInfo]:
        if node.type == "struct_item":
            name_node = _child_by_type(node, "type_identifier")
            if name_node and _text(name_node) == name:
                fields = []
                fdl = _child_by_type(node, "field_declaration_list")
                if fdl:
                    pending_attrs: list[ts.Node] = []
                    for c in fdl.children:
                        if c.type == "attribute_item":
                            pending_attrs.append(c)
                            continue
                        if c.type != "field_declaration":
                            # punctuation ({, }, ,) — reset attrs only on siblings
                            if c.type in ("{", "}", ","):
                                pass
                            continue
                        fd = c
                        excluded = _is_cfg_excluded(pending_attrs, active_features)
                        pending_attrs = []
                        if excluded:
                            continue
                        fname_node = _child_by_type(fd, "field_identifier")
                        ftype_node = None
                        after_colon = False
                        for cc in fd.children:
                            if cc.type == ":":
                                after_colon = True
                            elif after_colon:
                                ftype_node = cc
                                break
                        if fname_node and ftype_node:
                            fields.append(FieldInfo(
                                name=_text(fname_node),
                                type=_parse_type_node(ftype_node),
                            ))
                is_ghost = _child_by_type(node, "data_mode") is not None
                parent_attrs = (_parent_item_attrs(parent, node)
                                if parent is not None else ItemAttrs())
                return TypeInfo(kind=TypeKind.STRUCT, name=name,
                                fields=fields,
                                is_opaque=parent_attrs.is_external_body,
                                is_ext_equal=parent_attrs.is_ext_equal)
        for child in node.children:
            result = walk(child, node)
            if result:
                return result
        return None

    return walk(tree.root_node)


def _find_view_method_return(tree: ts.Tree, struct_name: str) -> Optional[TypeInfo]:
    """C-patch: find ``impl <struct_name> { spec fn view(self) -> X { ... } }``
    and return the parsed return type ``X`` as a TypeInfo.

    The ironkv convention (and the canonical Verus pattern for exposing a
    spec view from an ``external_body`` struct or any exec wrapper) is to
    write the view as an inherent ``spec fn`` rather than as a sibling
    ``TView`` struct. Without picking this up, gen_det's STRUCT branch
    would descend into the wrapper's exec fields and emit field-access
    obligations that Verus rejects for opaque datatypes.

    Recognises both ``function_item`` (body present) and
    ``function_signature_item`` (declarations like
    ``pub uninterp spec fn view(self) -> Map<K,V>;``). Skips trait impls
    (``impl Trait for Type``) and any view that lacks a named return type.
    """
    def walk(node: ts.Node) -> Optional[TypeInfo]:
        if node.type == "impl_item":
            # `impl Trait for Type` includes a `for` keyword child — skip
            # those; only handle bare `impl Type { ... }`.
            has_for = any(c.type == "for" for c in node.children)
            type_id = _child_by_type(node, "type_identifier")
            if not has_for and type_id and _text(type_id) == struct_name:
                decl_list = _child_by_type(node, "declaration_list")
                if decl_list:
                    for d in decl_list.children:
                        fn_node = d
                        if d.type == "declaration_with_attrs":
                            fn_node = next(
                                (cc for cc in d.children
                                 if cc.type in ("function_item",
                                                "function_signature_item")),
                                None,
                            )
                            if fn_node is None:
                                continue
                        if fn_node.type not in ("function_item",
                                                "function_signature_item"):
                            continue
                        fmode = _child_by_type(fn_node, "function_mode")
                        fid = _child_by_type(fn_node, "identifier")
                        if (fmode is None or _text(fmode) != "spec"
                                or fid is None or _text(fid) != "view"):
                            continue
                        ret = _child_by_type(fn_node, "named_return_type")
                        if ret is None:
                            continue
                        type_node = next(
                            (cc for cc in ret.children if cc.type != "->"),
                            None,
                        )
                        if type_node is None:
                            continue
                        return _parse_type_node(type_node)
        for child in node.children:
            result = walk(child)
            if result is not None:
                return result
        return None

    return walk(tree.root_node)


def _find_enum(
    tree: ts.Tree,
    name: str,
    active_features: Optional[set[str]] = None,
) -> Optional[TypeInfo]:
    """Find an enum definition by name and extract its variants."""
    def walk(node: ts.Node, parent: Optional[ts.Node] = None) -> Optional[TypeInfo]:
        if node.type == "enum_item":
            name_node = _child_by_type(node, "type_identifier")
            if name_node and _text(name_node) == name:
                variants = []
                vl = _child_by_type(node, "enum_variant_list")
                if vl:
                    pending_attrs: list[ts.Node] = []
                    for c in vl.children:
                        if c.type == "attribute_item":
                            pending_attrs.append(c)
                            continue
                        if c.type != "enum_variant":
                            continue
                        v = c
                        excluded = _is_cfg_excluded(pending_attrs, active_features)
                        pending_attrs = []
                        if excluded:
                            continue
                        vname = _child_by_type(v, "identifier")
                        # Check for tuple inner type
                        inner = None
                        struct_form = False
                        ofl = _child_by_type(v, "ordered_field_declaration_list")
                        if ofl:
                            for cc in ofl.children:
                                if cc.type not in ("(", ")", ","):
                                    inner = _parse_type_node(cc)
                                    break
                        else:
                            # Rust struct-form variant: ``V { f1: T1, f2: T2 }``
                            # Tree-sitter exposes the body as a
                            # ``field_declaration_list``.
                            fdl_v = _child_by_type(v, "field_declaration_list")
                            if fdl_v is not None:
                                named_fields: list[FieldInfo] = []
                                for fc in fdl_v.children:
                                    if fc.type != "field_declaration":
                                        continue
                                    fname_node = _child_by_type(fc, "field_identifier")
                                    if fname_node is None:
                                        continue
                                    # The field's type is the first non-name,
                                    # non-punct child; reuse _parse_type_node.
                                    ftype_info: Optional[TypeInfo] = None
                                    for fcc in fc.children:
                                        if fcc.type in ("field_identifier",
                                                        ":", ",",
                                                        "visibility_modifier",
                                                        "attribute_item"):
                                            continue
                                        ftype_info = _parse_type_node(fcc)
                                        if ftype_info is not None:
                                            break
                                    if ftype_info is None:
                                        continue
                                    named_fields.append(FieldInfo(
                                        name=_text(fname_node),
                                        type=ftype_info,
                                    ))
                                if named_fields and vname:
                                    inner = TypeInfo(
                                        kind=TypeKind.STRUCT,
                                        name=f"{name}::{_text(vname)}",
                                        fields=named_fields,
                                    )
                                    struct_form = True
                        # Explicit discriminant: `Slab8 = 8` — parse the
                        # integer literal that follows '='. This turns the
                        # enum into a C-like int enum so narrow can emit
                        # `x as int == 8` witnesses instead of `x is Slab8`.
                        discriminant = None
                        kids = list(v.children)
                        for i, cc in enumerate(kids):
                            if cc.type == "=" and i + 1 < len(kids):
                                lit = kids[i + 1]
                                if lit.type == "integer_literal":
                                    txt = _text(lit).replace("_", "")
                                    # Strip any numeric suffix like i32/u64
                                    m = re.match(r"-?\d+", txt)
                                    if m:
                                        try:
                                            discriminant = int(m.group(0))
                                        except ValueError:
                                            pass
                                break
                        if vname:
                            variants.append(VariantInfo(
                                name=_text(vname),
                                inner=inner,
                                discriminant=discriminant,
                                struct_form=struct_form,
                            ))
                enum_attrs = (_parent_item_attrs(parent, node)
                              if parent is not None else ItemAttrs())
                return TypeInfo(kind=TypeKind.ENUM, name=name, variants=variants,
                                is_ext_equal=enum_attrs.is_ext_equal)
        for child in node.children:
            result = walk(child, node)
            if result:
                return result
        return None

    return walk(tree.root_node)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class Unsupported(Exception):
    """The mechanical parser cannot safely extract the requested specification."""
    pass


def _resolve_self_in_type(ty: TypeInfo, impl_text: str):
    """Replace ``Self`` references in a TypeInfo with the impl target text.

    ``impl_text`` may include type arguments (e.g. ``Foo<K>``); we use a
    word-boundary regex so substring identifiers like ``MySelfType`` are
    untouched.
    """
    pattern = re.compile(r'\bSelf\b')
    if ty.name == "Self":
        ty.name = impl_text
    else:
        ty.name = pattern.sub(impl_text, ty.name)
    for ta in ty.type_args:
        _resolve_self_in_type(ta, impl_text)
    for v in ty.variants:
        if v.inner:
            _resolve_self_in_type(v.inner, impl_text)


# ---------------------------------------------------------------------------
# Impl / generic context — kept entirely text-based (we copy the AST text of
# `type_parameters` / `where_clause` verbatim into the synthesized det fn).
# ---------------------------------------------------------------------------

# Tree-sitter-verus emits a variety of node kinds for the type position. We
# enumerate the common ones; other kinds fall through to the fallback in
# _impl_target_node.
_IMPL_TYPE_REF_KINDS = (
    "generic_type", "type_identifier", "scoped_type_identifier",
    "scoped_identifier", "reference_type", "tuple_type", "array_type",
)


@dataclass
class _ImplContext:
    """Generic / impl-block context lifted as raw AST text."""
    self_type: Optional[str] = None       # impl target, e.g. "Foo<K>"
    generics_decl: str = ""                # e.g. "<K: KeyTrait, V: Clone>"
    where_decl: str = ""                    # e.g. "where K: Ord"


def _find_trait_node(fn_node: ts.Node, tree: ts.Tree) -> Optional[ts.Node]:
    """Find the enclosing trait without confusing methods from sibling impls."""
    node = fn_node.parent
    while node is not None:
        if node.type == "impl_item":
            return None
        if node.type == "trait_item":
            return node
        node = node.parent

    fn_start, fn_end = fn_node.start_byte, fn_node.end_byte
    best = None
    for trait_node in _find_all_nodes(tree.root_node, "trait_item"):
        if trait_node.start_byte <= fn_start and trait_node.end_byte >= fn_end:
            if best is None or (trait_node.end_byte - trait_node.start_byte) < (best.end_byte - best.start_byte):
                best = trait_node
    return best


def _find_enclosing_trait(fn_node: ts.Node, tree: ts.Tree) -> Optional[str]:
    """Return the bare enclosing trait name, or None outside a trait."""
    node = _find_trait_node(fn_node, tree)
    ti = _child_by_type(node, "type_identifier") if node is not None else None
    return _text(ti) if ti else None


def _find_impl_node(fn_node: ts.Node, tree: ts.Tree) -> Optional[ts.Node]:
    """Locate the enclosing ``impl_item`` AST node for ``fn_node``.

    Tries (1) parent chain walk and (2) byte-range containment over all
    impl_item nodes in the tree. Returns ``None`` if the fn lives at module
    scope or the parser couldn't recover the impl structure (in which case
    the caller falls back to the older flat-token strategy via
    ``_find_impl_type``).
    """
    node = fn_node.parent
    while node is not None:
        if node.type == "impl_item":
            return node
        node = node.parent

    fn_start, fn_end = fn_node.start_byte, fn_node.end_byte
    best = None
    for impl_node in _find_all_nodes(tree.root_node, "impl_item"):
        if impl_node.start_byte <= fn_start and impl_node.end_byte >= fn_end:
            if best is None or (impl_node.end_byte - impl_node.start_byte) < (best.end_byte - best.start_byte):
                best = impl_node
    return best


def _find_enclosing_node_by_line(
    tree: ts.Tree,
    node_type: str,
    source_line: int,
) -> Optional[ts.Node]:
    """Return the smallest node of ``node_type`` containing ``source_line``."""
    target_row = source_line - 1
    best = None
    for node in _find_all_nodes(tree.root_node, node_type):
        if node.start_point.row <= target_row <= node.end_point.row:
            if best is None or (node.end_byte - node.start_byte) < (
                best.end_byte - best.start_byte
            ):
                best = node
    return best


def _impl_target_node(impl_node: ts.Node) -> Optional[ts.Node]:
    """Return the type-reference child that names the impl target.

    For trait impls (``impl<T> Trait<T> for Foo<T>``) the target is the type
    ref *after* the ``for`` token; for inherent impls it's the first type ref
    after ``impl`` / type-parameters.
    """
    children = impl_node.children
    for_idx = None
    for i, c in enumerate(children):
        if c.type == "for":
            for_idx = i
            break
    candidates = children[for_idx + 1:] if for_idx is not None else children
    for c in candidates:
        if c.type in _IMPL_TYPE_REF_KINDS:
            return c
    return None


def _extract_impl_context(impl_node: Optional[ts.Node]) -> _ImplContext:
    if impl_node is None:
        return _ImplContext()
    tp = _child_by_type(impl_node, "type_parameters")
    target = _impl_target_node(impl_node)
    wc = _child_by_type(impl_node, "where_clause")
    return _ImplContext(
        self_type=_text(target) if target else None,
        generics_decl=_text(tp) if tp else "",
        where_decl=_text(wc) if wc else "",
    )


def _extract_fn_generics_decl(fn_node: ts.Node) -> str:
    tp = _child_by_type(fn_node, "type_parameters")
    return _text(tp) if tp else ""


def _extract_fn_where_decl(fn_node: ts.Node) -> str:
    wc = _child_by_type(fn_node, "where_clause")
    return _text(wc) if wc else ""


def _combine_generics_decl(impl_g: str, fn_g: str) -> str:
    """Merge two ``<...>`` text blobs into one. Either may be empty."""
    inner: list[str] = []
    for g in (impl_g, fn_g):
        s = g.strip()
        if s.startswith("<") and s.endswith(">"):
            body = s[1:-1].strip()
            if body:
                inner.append(body)
    return f"<{', '.join(inner)}>" if inner else ""


def _combine_where_decl(impl_w: str, fn_w: str) -> str:
    """Merge two ``where ...`` text blobs into one. Either may be empty."""
    parts: list[str] = []
    for w in (impl_w, fn_w):
        s = w.strip()
        if s.startswith("where"):
            tail = s[len("where"):].strip()
            if tail:
                parts.append(tail)
    return f"where {', '.join(parts)}" if parts else ""


def extract_spec(
    source: str,
    fn_name: str,
    type_sources: list[str] | None = None,
    active_features: Optional[set[str]] = None,
    source_line: Optional[int] = None,
    diagnostics: Optional[list[str]] = None,
) -> FunctionSpec:
    """
    Extract function spec from source code using tree-sitter-verus.

    Args:
        source: The .rs source containing the function
        fn_name: Name of the target function
        type_sources: Additional sources to search for type definitions
        source_line: Optional 1-based source line used to disambiguate
            same-named functions or methods.

    Returns:
        FunctionSpec

    Raises:
        Unsupported: when parser cannot handle the pattern
    """
    full_tree = _parser.parse(source.encode())
    if full_tree.root_node.has_error:
        message = f"Source parse errors while extracting {fn_name}; attempting local recovery"
        logger.warning(message)
        if diagnostics is not None:
            diagnostics.append(message)

    # Find target function — first try tree-sitter, then re-parse a chunk
    fn_node = None
    tree = full_tree  # tree used for fn_node context (may be chunk tree)
    matching_fns = []
    for fn in _find_function_items(full_tree):
        if _matches_function_item(fn, fn_name):
            matching_fns.append(fn)
    if source_line is not None and matching_fns:
        exact = [
            fn
            for fn in matching_fns
            if fn.start_point.row + 1 == source_line
        ]
        if len(exact) == 1:
            fn_node = exact[0]
        elif len(exact) > 1:
            raise Unsupported(
                f"Multiple functions named '{fn_name}' start at line "
                f"{source_line}"
            )
        else:
            candidate_lines = [fn.start_point.row + 1 for fn in matching_fns]
            raise Unsupported(
                f"Cannot find function '{fn_name}' at line {source_line}; "
                f"candidate lines: {candidate_lines}"
            )
    elif len(matching_fns) > 1:
        raise Unsupported(
            f"Multiple functions named '{fn_name}'; supply source_line to disambiguate"
        )
    elif matching_fns:
        fn_node = matching_fns[0]

    if fn_node is None:
        # Fallback: extract the function chunk and re-parse it in isolation.
        # This handles cases where ERROR recovery in the full file swallows
        # some functions (e.g. due to proof! macros, nested cfg_attr, etc.)
        chunk, chunk_tree = _extract_fn_chunk(
            source,
            fn_name,
            source_line=source_line,
        )
        if chunk_tree is not None:
            tree = chunk_tree
            for fn in _find_function_items(tree):
                if _matches_function_item(fn, fn_name):
                    fn_node = fn
                    break

    if fn_node is None:
        raise Unsupported(f"Cannot find function '{fn_name}' in source")
    for child in fn_node.named_children:
        if child.type != "block" and child.has_error:
            raise Unsupported(
                f"Parse errors in signature or specification of '{fn_name}' "
                f"at line {child.start_point.row + 1}"
            )

    # Extract parameters
    params_node = _child_by_type(fn_node, "parameters")
    params = _extract_params(params_node) if params_node else []

    # Extract return type
    return_type, ret_binding = _extract_return_type(fn_node)

    # Extract requires/ensures — try attribute first, then inline fn_qualifier
    requires_raw: list[str] = []
    ensures_raw: list[str] = []
    result_binding = ret_binding  # from named return type

    spec_info = _find_verus_spec_for_fn(fn_node, tree)
    if spec_info is not None:
        attr_binding, fq_node = spec_info
        if attr_binding:
            result_binding = attr_binding
        requires_raw, ensures_raw = _extract_clauses(fq_node)
    else:
        # Bodyless declarations can put later clauses beside fn_qualifier.
        for child in fn_node.children:
            if child.type == "fn_qualifier":
                requires, ensures = _extract_clauses(child)
                requires_raw.extend(requires)
                ensures_raw.extend(ensures)
            elif child.type == "requires_clause":
                requires_raw.extend(_clause_expressions(child))
            elif child.type == "ensures_clause":
                ensures_raw.extend(_clause_expressions(child))

    # Resolve self type from enclosing impl block (always use full_tree).
    # Two-tier strategy: prefer the AST-based `_find_impl_node` (also
    # surfaces generics + where), and fall back to the older flat-token
    # `_find_impl_type` (chunk-parsed / recovery cases) for bare type
    # names only.
    if tree is full_tree:
        impl_node = _find_impl_node(fn_node, full_tree)
        trait_node = _find_trait_node(fn_node, full_tree)
    else:
        impl_node = (
            _find_enclosing_node_by_line(full_tree, "impl_item", source_line)
            if source_line is not None else None
        )
        trait_node = (
            _find_enclosing_node_by_line(full_tree, "trait_item", source_line)
            if source_line is not None and impl_node is None else None
        )
    impl_ctx = _extract_impl_context(impl_node)
    if impl_ctx.self_type is None and trait_node is None and tree is not full_tree:
        bare_name = _find_impl_type(fn_node, full_tree, source, fn_name=fn_name)
        if bare_name:
            impl_ctx = _ImplContext(self_type=bare_name)

    fn_generics = _extract_fn_generics_decl(fn_node)
    fn_where = _extract_fn_where_decl(fn_node)
    generics_decl = _combine_generics_decl(impl_ctx.generics_decl, fn_generics)
    where_decl = _combine_where_decl(impl_ctx.where_decl, fn_where)

    if impl_ctx.self_type:
        for p in params:
            if p.is_self:
                p.type = TypeInfo(kind=TypeKind.UNKNOWN, name=impl_ctx.self_type)
            else:
                # Non-self params may reference Self / Self::Item too
                _resolve_self_in_type(p.type, impl_ctx.self_type)
        # Also resolve Self in return type
        _resolve_self_in_type(return_type, impl_ctx.self_type)

    trait_name = None
    if impl_ctx.self_type is None and trait_node is not None:
        name_node = _child_by_type(trait_node, "type_identifier")
        trait_name = _text(name_node) if name_node is not None else None
        generics_decl = _combine_generics_decl(
            _extract_fn_generics_decl(trait_node), generics_decl,
        )
        where_decl = _combine_where_decl(_extract_fn_where_decl(trait_node), where_decl)
        type_params = _child_by_type(trait_node, "type_parameters")
        if trait_name and type_params is not None:
            names: list[str] = []
            for parameter in type_params.named_children:
                if parameter.type in {"line_comment", "block_comment"}:
                    continue
                name_node = parameter.child_by_field_name("name")
                if name_node is None:
                    raise Unsupported(f"Unsupported generic trait parameter: {_text(parameter)}")
                names.append(_text(name_node))
            if names:
                trait_name += "<" + ", ".join(names) + ">"

    # Resolve type definitions from all sources
    all_sources = [source] + (type_sources or [])
    type_defs, return_type = _resolve_types(params, return_type, all_sources, active_features)

    return FunctionSpec(
        name=fn_name,
        params=params,
        return_type=return_type,
        requires=requires_raw,
        ensures=ensures_raw,
        type_defs=type_defs,
        result_binding=result_binding or "result",
        generics_decl=generics_decl,
        where_decl=where_decl,
        self_type=impl_ctx.self_type,
        trait_name=trait_name,
    )


def _resolve_types(
    params: list[Param],
    return_type: TypeInfo,
    sources: list[str],
    active_features: Optional[set[str]] = None,
) -> tuple[dict[str, TypeInfo], TypeInfo]:
    """Resolve unknown types by searching source files for struct/enum definitions.

    Does a transitive resolution: if `Error` is resolved to a struct with
    field `code: ErrorCode`, `ErrorCode` is resolved too. Also propagates
    resolved types into any TypeInfo slot (params, return type_args, struct
    field types, enum variant inners) that still has kind=UNKNOWN with the
    matching name.

    Returns (type_defs, resolved_return_type). The return_type is returned
    explicitly because if it is itself UNKNOWN at the top level (e.g. a
    bare user-defined struct like `-> (cloned_ep: EndPoint)`), the
    in-place mutation of nested slots can't replace the top-level
    TypeInfo. Without the explicit return, parameters resolve correctly
    (via `p.type = _substitute(p.type)`) but the return type stays
    UNKNOWN, leaving `r1`/`r2` un-narrowable in witness search.
    """
    type_defs: dict[str, TypeInfo] = {}
    # tree-sitter-rust trips on inner attributes (`#![...]`) at the top of
    # some files (e.g. nanvix's error crate has `#![cfg_attr(...)]`), which
    # produces an ERROR node that prevents finding enum/struct definitions.
    # Strip them out for type-resolution parsing.
    cleaned_sources = [re.sub(r'#!\[[^\]]*\]', '', s) for s in sources]
    trees = [_parser.parse(src.encode()) for src in cleaned_sources]
    for index, tree in enumerate(trees):
        if tree.root_node.has_error:
            logger.warning(
                "Parse errors in type source %s; structural type resolution may be incomplete",
                index,
            )

    def _lookup(name: str) -> Optional[TypeInfo]:
        if name in type_defs:
            return type_defs[name]
        if name in PRIMITIVE_MAP:
            return None
        for t in trees:
            resolved = _find_struct(t, name, active_features) or _find_enum(t, name, active_features)
            if resolved:
                type_defs[name] = resolved
                return resolved
        return None

    def _collect_unknown(ti: TypeInfo, out: set[str]) -> None:
        if ti.kind == TypeKind.UNKNOWN and ti.name and ti.name not in PRIMITIVE_MAP:
            out.add(ti.name)
        for ta in ti.type_args:
            _collect_unknown(ta, out)
        for f in ti.fields:
            _collect_unknown(f.type, out)
        for v in ti.variants:
            if v.inner:
                _collect_unknown(v.inner, out)

    def _substitute(ti: TypeInfo) -> TypeInfo:
        if ti.kind == TypeKind.UNKNOWN and ti.name in type_defs:
            return type_defs[ti.name]
        # Recurse into nested slots (mutate in place for consistency)
        ti.type_args = [_substitute(ta) for ta in ti.type_args]
        for f in ti.fields:
            f.type = _substitute(f.type)
        for v in ti.variants:
            if v.inner:
                v.inner = _substitute(v.inner)
        return ti

    # Seed worklist from params and return type_args
    worklist: set[str] = set()
    for p in params:
        _collect_unknown(p.type, worklist)
    _collect_unknown(return_type, worklist)

    # Transitive resolution: resolve each name, then harvest any new names
    # referenced by the resolved definition.
    seen: set[str] = set()
    while worklist:
        name = worklist.pop()
        if name in seen:
            continue
        seen.add(name)
        resolved = _lookup(name)
        if resolved is None:
            continue
        # New names referenced by the resolved def
        refs: set[str] = set()
        _collect_unknown(resolved, refs)
        worklist.update(refs - seen)

    # Propagate resolved types into all TypeInfo slots
    for p in params:
        p.type = _substitute(p.type)
    return_type = _substitute(return_type)
    for td in list(type_defs.values()):
        _substitute(td)

    # Look for spec view types (e.g. BitmapView for Bitmap). The view types
    # themselves may reference additional unresolved types (e.g. KheapView's
    # `slabs: Seq<SlabView>` pulls SlabView into scope). Collect those as a
    # second-pass worklist and resolve + substitute so every field inside a
    # view structure has its concrete definition.
    view_worklist: set[str] = set()
    for name in list(type_defs.keys()):
        view_name = name + "View"
        for t in trees:
            view_type = _find_struct(t, view_name)
            if view_type:
                type_defs[view_name] = view_type
                type_defs[name].spec_view = view_type
                _collect_unknown(view_type, view_worklist)
                break

    # C-patch (Tier 1, semantics-preserving): also pick up the ironkv-style
    # `impl T { spec fn view(self) -> X { ... } }` convention. This is the
    # canonical Verus pattern for exposing a spec view from an external_body
    # struct (or any wrapper). Without this, codegen would descend into the
    # struct's exec fields and emit obligations that Verus rejects (`field
    # expression for an opaque datatype`). When the impl-side view exists,
    # populate `spec_view` so the gen_det STRUCT branch falls back to
    # `(lhs)@ == (rhs)@`.
    for name, td in list(type_defs.items()):
        if td.spec_view is not None:
            continue
        for t in trees:
            ret_ty = _find_view_method_return(t, name)
            if ret_ty is not None:
                td.spec_view = ret_ty
                _collect_unknown(ret_ty, view_worklist)
                break

    while view_worklist:
        name = view_worklist.pop()
        if name in seen:
            continue
        seen.add(name)
        resolved = _lookup(name)
        if resolved is None:
            continue
        refs: set[str] = set()
        _collect_unknown(resolved, refs)
        view_worklist.update(refs - seen)
        # Also pick up the referenced type's own spec_view (e.g. Slab → SlabView)
        vname = name + "View"
        if vname not in type_defs:
            for t in trees:
                v = _find_struct(t, vname)
                if v:
                    type_defs[vname] = v
                    resolved.spec_view = v
                    extra: set[str] = set()
                    _collect_unknown(v, extra)
                    view_worklist.update(extra - seen)
                    break
        # C-patch — also try `impl T { spec fn view -> X }` for pulled-in T.
        if resolved.spec_view is None:
            for t in trees:
                ret_ty = _find_view_method_return(t, name)
                if ret_ty is not None:
                    resolved.spec_view = ret_ty
                    extra2: set[str] = set()
                    _collect_unknown(ret_ty, extra2)
                    view_worklist.update(extra2 - seen)
                    break

    # Second-pass substitution so newly-resolved names propagate into every slot
    for p in params:
        p.type = _substitute(p.type)
    return_type = _substitute(return_type)
    for td in list(type_defs.values()):
        _substitute(td)
        if td.spec_view:
            _substitute(td.spec_view)

    # Tag every TypeInfo whose declaration carries a recognised attribute
    # (``#[verifier::external_body]``, ``#[verifier::ext_equal]``). This lets
    # gen_det's STRUCT/ENUM branches short-circuit appropriately instead of
    # an exponential field-by-field expansion. ``_find_struct`` /
    # ``_find_enum`` already detect the attribute on the items they
    # discover, but tree-walking can miss generic types whose source-text
    # name doesn't round-trip cleanly through ``_lookup`` (e.g.
    # ``AckState<MT>`` declared inline inside a macro-expanded block), and
    # generic instantiation via the second-pass ``_substitute`` may produce
    # shallow copies that need re-tagging. A direct AST-level scan over
    # every parse tree picks those up regardless. All of this is now
    # implemented inside :mod:`specdet.adapters.verus.native.extract.attrs` so that the
    # runner side can share the exact same code path.
    attrs_table: dict[str, ItemAttrs] = {}
    from .attrs import _merge  # noqa: PLC0415
    for t in trees:
        for name, ia in collect_item_attrs_from_tree(t).items():
            if name in attrs_table:
                _merge(attrs_table[name], ia)
            else:
                attrs_table[name] = ia
    propagate_attrs_to_type_defs(
        attrs_table,
        type_defs=type_defs,
        params=params,
        return_type=return_type,
    )

    return type_defs, return_type


# ---------------------------------------------------------------------------
# Self-tests — invoke via `python -m specdet.adapters.verus.native.extract.extractor test`.
# Keep these scoped to internal helpers that don't depend on a full tree-sitter
# parse pipeline (the latter is exercised by the corpus runner).
# ---------------------------------------------------------------------------

def _run_self_tests() -> int:
    failures: list[str] = []

    # ISSUES #14 follow-up — `_resolve_types` must resolve user-defined
    # struct names appearing AT THE TOP LEVEL of the return type, not
    # just nested inside type_args / fields. Pre-fix, `p.type = _substitute(p.type)`
    # captured the substitution result, while `_substitute(return_type)`
    # discarded it: an UNKNOWN top-level return type like
    # `-> (cloned_ep: EndPoint)` stayed UNKNOWN even after a matching
    # `struct EndPoint { id: Vec<u8> }` was found in `sources`. The
    # consequence: r1/r2 in the synthesised proof fn could not be
    # narrowed (no fields → narrow_struct dead-ended), so witnesses
    # for clone-shaped fns regressed to the degenerate bare-neq form.
    src = "pub struct EndPoint { pub id: Vec<u8>, }"
    ep_unknown = TypeInfo(kind=TypeKind.UNKNOWN, name="EndPoint")
    type_defs, resolved_ret = _resolve_types([], ep_unknown, [src])
    if resolved_ret.kind != TypeKind.STRUCT:
        failures.append(
            f"Top-level UNKNOWN return type must be substituted to STRUCT; "
            f"got kind={resolved_ret.kind}"
        )
    if not any(f.name == "id" for f in resolved_ret.fields):
        failures.append(
            "Resolved EndPoint must expose `id` field; "
            f"fields={[f.name for f in resolved_ret.fields]}"
        )
    # Inner Vec<u8> must inherit the spec_view tag from the generic_type branch.
    id_field = next((f for f in resolved_ret.fields if f.name == "id"), None)
    if id_field is None or id_field.type.spec_view is None:
        failures.append(
            "EndPoint.id (Vec<u8>) must carry spec_view=Seq<u8>; "
            f"got spec_view={id_field.type.spec_view if id_field else None!r}"
        )

    # C-patch (Tier 1) — an external_body wrapper that exposes its spec view
    # via `impl T { spec fn view(self) -> X }` must populate `T.spec_view`
    # to X. Without this, gen_det descends into the wrapper's exec fields
    # and emits obligations rejected by Verus.
    src_view_impl_map = (
        "#[verifier(external_body)] pub struct CKeyHashMap { m: u32 } "
        "impl CKeyHashMap { "
        "  pub uninterp spec fn view(self) -> Map<AbstractKey, Seq<u8>>; "
        "}"
    )
    tdefs, _ = _resolve_types(
        [],
        TypeInfo(kind=TypeKind.UNKNOWN, name="CKeyHashMap"),
        [src_view_impl_map],
    )
    chm = tdefs.get("CKeyHashMap")
    if chm is None or chm.spec_view is None:
        failures.append(
            "C-patch: CKeyHashMap.spec_view must be populated from "
            f"`impl CKeyHashMap {{ spec fn view -> ... }}`; got {chm}"
        )
    elif chm.spec_view.kind != TypeKind.MAP:
        failures.append(
            "C-patch: CKeyHashMap.spec_view should be Map<K,V> "
            f"(kind=MAP); got kind={chm.spec_view.kind}, name={chm.spec_view.name!r}"
        )

    # Also exercise the inherent-view case where the view is itself a named
    # struct (typical for `impl T { spec fn view -> AbstractT }`).
    src_view_impl_struct = (
        "pub struct EndPoint { pub id: Vec<u8>, } "
        "impl EndPoint { "
        "  pub open spec fn view(self) -> AbstractEndPoint { "
        "    AbstractEndPoint { id: self.id@ } "
        "  } "
        "} "
        "pub struct AbstractEndPoint { pub id: Seq<u8>, }"
    )
    tdefs2, _ = _resolve_types(
        [],
        TypeInfo(kind=TypeKind.UNKNOWN, name="EndPoint"),
        [src_view_impl_struct],
    )
    ep = tdefs2.get("EndPoint")
    if ep is None or ep.spec_view is None:
        failures.append(
            "C-patch: EndPoint.spec_view must be populated from "
            f"`impl EndPoint {{ spec fn view -> AbstractEndPoint }}`; got {ep}"
        )
    elif ep.spec_view.name != "AbstractEndPoint":
        failures.append(
            "C-patch: EndPoint.spec_view should be AbstractEndPoint; "
            f"got {ep.spec_view.name!r}"
        )

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    # A1 regression — destructure patterns in exec-fn params (Tracked / Ghost
    # carriers) must surface the *inner* binding as Param.name, not "?".
    # Without this, gen_det renders `?: Tracked<...>` in the synthesized
    # proof fn signature, which fails to parse (see verus_error fix plan
    # entry A1, 42 atmosphere targets pre-fix).
    src_destructure = (
        "verus! {\n"
        "fn free_page_4k(&mut self, target_ptr: u64, "
        "Tracked(target_perm): Tracked<u32>, Ghost(g): Ghost<u32>)\n"
        "    ensures self == old(self)\n"
        "{}\n"
        "}\n"
    )
    spec = extract_spec(src_destructure, "free_page_4k", type_sources=[])
    names = [p.name for p in spec.params]
    if "?" in names:
        failures.append(
            "A1: destructure-pattern params must yield a bound name, "
            f"not '?'; got names={names}"
        )
    if "target_perm" not in names:
        failures.append(
            "A1: Tracked(target_perm) must surface inner identifier "
            f"'target_perm' as Param.name; got names={names}"
        )
    if "g" not in names:
        failures.append(
            "A1: Ghost(g) must surface inner identifier 'g' as "
            f"Param.name; got names={names}"
        )

    # Ghost/Tracked destructure: the param's type must be unwrapped to the
    # inner type (e.g. ``Ghost(g): Ghost<u32>`` → ``g: u32``) because the
    # original function body uses ``g`` as the inner type. Probe enumeration
    # and requires/ensures substitution all read Param.type; carrying the
    # wrapper type would emit spurious ``g@`` over a non-View concrete type.
    for p in spec.params:
        if p.name == "target_perm":
            if p.type.name != "u32" or p.destructure_ctor != "Tracked":
                failures.append(
                    "A1: Tracked(target_perm): Tracked<u32> must unwrap to "
                    f"type=u32 + destructure_ctor='Tracked'; got type={p.type.name} "
                    f"ctor={p.destructure_ctor}"
                )
        if p.name == "g":
            if p.type.name != "u32" or p.destructure_ctor != "Ghost":
                failures.append(
                    "A1: Ghost(g): Ghost<u32> must unwrap to type=u32 + "
                    f"destructure_ctor='Ghost'; got type={p.type.name} "
                    f"ctor={p.destructure_ctor}"
                )

    # A3 regression — fn declared inside `pub trait T { ... }` (no enclosing
    # impl block) must surface ``trait_name`` so gen_det can emit a
    # ``<__DetSelf: T>`` bound on the synthesized proof fn. Without this,
    # standalone references to ``Self::method`` fall back to E0411 / E0599.
    src_trait_fn = (
        "verus! {\n"
        "pub trait KeyTrait: Sized {\n"
        "    spec fn zero_spec() -> Self;\n"
        "    fn zero() -> (z: Self)\n"
        "        ensures z == Self::zero_spec()\n"
        "    { Self::zero_spec() }\n"
        "}\n"
        "}\n"
    )
    spec = extract_spec(src_trait_fn, "zero", type_sources=[])
    if spec.self_type is not None:
        failures.append(
            "A3: trait-declared fn must have self_type=None, "
            f"got {spec.self_type!r}"
        )
    if spec.trait_name != "KeyTrait":
        failures.append(
            "A3: trait-declared fn must capture enclosing trait name "
            f"'KeyTrait'; got trait_name={spec.trait_name!r}"
        )

    # Same-named impl methods must be selectable by source line. vstd has many
    # repeated names (`new`, `get`, `insert`, `take`) in one module; silently
    # taking the first match audits the wrong specification.
    src_same_name = (
        "verus! {\n"
        "pub struct First {}\n"
        "pub struct Second {}\n"
        "impl First {\n"
        "    pub fn get(&self) -> (r: u32)\n"
        "        ensures r == 1\n"
        "    { 1 }\n"
        "}\n"
        "impl Second {\n"
        "    pub fn get(&self) -> (r: u32)\n"
        "        ensures r == 2\n"
        "    { 2 }\n"
        "}\n"
        "}\n"
    )
    first_get = src_same_name.index("pub fn get")
    second_get = src_same_name.index("pub fn get", first_get + 1)
    second_line = src_same_name.count("\n", 0, second_get) + 1
    spec = extract_spec(
        src_same_name,
        "get",
        type_sources=[],
        source_line=second_line,
    )
    if spec.self_type != "Second":
        failures.append(
            "line-qualified extraction should select impl Second::get; "
            f"got self_type={spec.self_type!r}"
        )
    if not any("r == 2" in ensure for ensure in spec.ensures):
        failures.append(
            "line-qualified extraction selected the wrong ensures; "
            f"got ensures={spec.ensures}"
        )

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All extractor self-tests passed.")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        raise SystemExit(_run_self_tests())
    print("usage: python -m specdet.adapters.verus.native.extract.extractor test")
    raise SystemExit(2)
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        raise SystemExit(_run_self_tests())
    print("usage: python -m specdet.adapters.verus.native.extract.extractor test")
    raise SystemExit(2)
