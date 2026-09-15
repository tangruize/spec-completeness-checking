"""Type registry — Phase 1: structural type dependency graph.

Walks a Verus source file, enumerates every top-level *type definition*
(``struct`` / ``enum`` / ``union`` / ``type`` alias) — including those nested
under ``mod`` blocks — and records:

* its kind, generics, visibility, derives, ``cfg(...)`` guards;
* whether it is marked ``#[verifier::external_body]`` /
  ``#[verifier::external_type_specification]``;
* for structs: each field's name, raw type text, and a parsed
  :class:`TypeExpr` *tree* of its type expression — which preserves
  containers, generic args, references, tuples, arrays, fn types.
* for enums: each variant (unit / tuple / named struct), with the same
  per-field info.

The output is a per-file dependency graph (nodes = type defs, edges =
"this type's body references that type by name"). It is **deliberately
unresolved** — short names are recorded as written; we do *not* try to map
``crate::foo::Bar`` to a fully-qualified name in this phase. Resolution
(use-tracking, View impls, trait impls) is Phase 2 work.

Beyond the raw direct-reference ``edges`` set, an enriched
:class:`DepGraph` is computable on top of any registry — see
:func:`compute_dep_graph` — which adds:

* ``forward_closure`` / ``reverse_closure`` — full transitive
  reachability in either direction;
* ``sccs`` / ``topological_order`` — Tarjan-style cycle detection +
  Kahn-style topological order over the SCC condensation;
* ``classification`` — each type labelled as ``leaf`` / ``container`` /
  ``compound`` / ``external`` so witness generation can pick a strategy
  per node.

Verusage corpus is flat single-file style — every `.rs` is self-contained.
The registry therefore operates one source file at a time. A separate
audit CLI rolls up registries across a project for cross-file statistics.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import tree_sitter as ts

from .extractor import _child_by_type, _children_by_type, _parser, _text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class GenericParam:
    """A single generic parameter on a type definition."""
    kind: str            # "type" | "lifetime" | "const"
    name: str            # "T" / "'a" / "N"
    raw: str             # full source text incl. bounds / default
    # Bound names referenced in trait bounds (so they get pruned out of edges).
    bounds: list[str] = field(default_factory=list)


@dataclass
class TypeExpr:
    """Recursive parsed type expression.

    The ``kind`` discriminates how to interpret ``head`` and ``args``:

    * ``"leaf"`` — a single user-type name (``head`` set, ``args`` empty)
    * ``"primitive"`` — a primitive (``head`` set to ``u32`` / ``bool`` /
      ``str`` / ``int`` / ``nat`` / etc; ``args`` empty)
    * ``"generic"`` — a generic instance like ``Vec<T>`` (``head`` is the
      head name, ``args`` are the type arguments)
    * ``"ref"`` — ``&T`` / ``&mut T``; ``args = [T]``; ``is_mut`` set
    * ``"ptr"`` — ``*const T`` / ``*mut T``; ``args = [T]``; ``is_mut``
      set
    * ``"tuple"`` — ``(A, B, C)``; ``args`` is the element list
    * ``"array"`` — ``[T; N]`` or ``[T]``; ``args = [T]``; ``extra``
      holds the size text (``""`` for unsized slices)
    * ``"fn"`` — ``fn(A) -> R`` / ``spec_fn(A) -> R``; ``args`` are
      ``[*params, return_type]``; ``head`` carries the constructor
      keyword (``"fn"`` / ``"spec_fn"``)
    * ``"dyn"`` / ``"impl"`` — trait-object / opaque types; ``args`` are
      the trait references
    * ``"unit"`` — ``()``
    * ``"unknown"`` — anything we couldn't parse cleanly; ``head`` holds
      the raw text for inspection
    """
    kind: str
    head: str = ""
    args: list["TypeExpr"] = field(default_factory=list)
    raw: str = ""
    is_mut: bool = False
    extra: str = ""

    def leaves(self, include_primitives: bool = False) -> list[str]:
        """Walk this expression collecting every user-type name (or
        primitive name if ``include_primitives``). De-duped, first-seen
        order.
        """
        seen: list[str] = []
        seen_set: set[str] = set()

        def visit(e: TypeExpr) -> None:
            if e.kind == "leaf" and e.head and e.head not in seen_set:
                seen.append(e.head)
                seen_set.add(e.head)
            if e.kind == "generic" and e.head and e.head not in seen_set:
                seen.append(e.head)
                seen_set.add(e.head)
            if e.kind == "primitive" and include_primitives:
                if e.head and e.head not in seen_set:
                    seen.append(e.head)
                    seen_set.add(e.head)
            for a in e.args:
                visit(a)

        visit(self)
        return seen


@dataclass
class FieldDecl:
    """A struct field or an enum-variant field."""
    name: str            # named field name; "0" / "1" / ... for tuple positions
    type_text: str       # raw type expression as written
    type_refs: list[str] # short names referenced (excludes generic params)
    is_pub: bool = False
    span: tuple[int, int] = (0, 0)
    type_expr: Optional[TypeExpr] = None  # parsed structured type tree


@dataclass
class VariantDecl:
    """An enum variant (unit / tuple / struct-shaped)."""
    name: str
    kind: str                                # "unit" | "tuple" | "struct"
    fields: list[FieldDecl] = field(default_factory=list)


@dataclass
class TypeDef:
    """A single type definition."""
    name: str                                # local short name
    qualified_name: str                      # mod::path::Name
    kind: str                                # "struct" | "enum" | "union" | "alias"
    generics: list[GenericParam] = field(default_factory=list)
    fields: list[FieldDecl] = field(default_factory=list)        # struct/union
    variants: list[VariantDecl] = field(default_factory=list)    # enum
    alias_target: Optional[str] = None                           # type alias RHS
    alias_target_refs: list[str] = field(default_factory=list)
    alias_target_expr: Optional[TypeExpr] = None
    is_external_body: bool = False
    is_external_type_specification: bool = False
    derives: list[str] = field(default_factory=list)
    visibility: str = ""                     # "" / "pub" / "pub(crate)" / ...
    cfg: list[str] = field(default_factory=list)
    source_file: str = ""
    source_line: int = 0


@dataclass
class TypeRegistry:
    """Per-file type registry.

    ``types`` is keyed by qualified name (mod path + local name); ``short_names``
    maps a local name to all qualified names that share it (handles
    cfg-gated duplicates and inner-mod shadowing). ``edges`` records, for
    each qualified name, the set of *short* type names referenced in its
    body.
    """
    source_file: str
    types: dict[str, TypeDef] = field(default_factory=dict)
    short_names: dict[str, list[str]] = field(default_factory=dict)
    edges: dict[str, set[str]] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class DepGraph:
    """Enriched dependency view derived from a :class:`TypeRegistry`.

    Nodes are *short names* (so generic instantiations of the same head
    type collapse together — ``Vec<u8>``, ``Vec<Foo>`` both depend on
    ``Vec``). Edges combine references from every TypeDef sharing that
    short name.
    """
    nodes: list[str]
    forward: dict[str, list[str]]          # direct refs
    forward_closure: dict[str, list[str]]  # transitive refs
    reverse: dict[str, list[str]]          # who refs me directly
    reverse_closure: dict[str, list[str]]  # who refs me transitively
    sccs: list[list[str]]                  # strongly-connected components
    topological_order: list[str]           # over the SCC condensation
    classification: dict[str, str]         # see classify_node()
    field_paths: dict[str, list[dict]]     # type → per-field structured trees


# ---------------------------------------------------------------------------
# Tree-sitter helpers (local — keep extract.py untouched)
# ---------------------------------------------------------------------------


def _attr_text(attr_node: ts.Node) -> str:
    """Return the inner attribute path text — e.g. for ``#[derive(Eq, Clone)]``
    return ``derive(Eq, Clone)`` (the bytes between ``#[`` / ``#![`` and ``]``).
    """
    return _text(attr_node).strip("#![] ").strip()


def _parse_derives(attr_text: str) -> list[str]:
    m = re.match(r"derive\s*\((.*)\)\s*$", attr_text, re.DOTALL)
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def _is_external_body(attr_text: str) -> bool:
    return "external_body" in attr_text and "verifier" in attr_text


def _is_external_type_specification(attr_text: str) -> bool:
    return "external_type_specification" in attr_text and "verifier" in attr_text


def _extract_cfg(attr_text: str) -> Optional[str]:
    if attr_text.startswith("cfg(") or attr_text.startswith("cfg_attr("):
        return attr_text
    return None


# ---------------------------------------------------------------------------
# Type-reference extraction
# ---------------------------------------------------------------------------

# Builtin / prelude names that should never be filtered as "user-defined refs"
# but are still recorded as referenced (they participate in the graph as
# external nodes — useful for the audit).
_PRIMITIVES = {
    "u8", "u16", "u32", "u64", "u128", "usize",
    "i8", "i16", "i32", "i64", "i128", "isize",
    "bool", "char", "str", "f32", "f64",
    "int", "nat",  # Verus spec types
}


def _looks_like_const_name(name: str) -> bool:
    """True for SCREAMING_SNAKE_CASE / all-caps names that are extremely
    unlikely to be a type. We use this as a heuristic filter because the
    tree-sitter-verus grammar classifies const arguments inside
    ``Array<T, NUM_PAGES>`` as ``type_identifier`` even though
    ``NUM_PAGES`` is a const item — there is no way to distinguish them
    structurally. Rust convention requires type names to be PascalCase
    and constants to be UPPER_SNAKE, so the rule "no lowercase letter →
    likely a const" is highly reliable in practice (no false positives
    seen in the verusage corpus' ~3.8k user types).
    """
    if not any(c.isalpha() for c in name):
        return False
    return all((not c.isalpha()) or c.isupper() for c in name)


def _collect_type_refs(node: ts.Node, generics: set[str]) -> list[str]:
    """Walk a type-expression subtree collecting every ``type_identifier`` /
    ``scoped_type_identifier`` head that is *not* a generic parameter bound
    in the enclosing scope.

    The result preserves first-seen order and de-dupes. Lifetimes and
    primitive keywords are dropped (lifetimes are not types; primitives
    are intentionally kept *only* if they appear via ``type_identifier``,
    e.g. ``str`` may show up as a primitive_type node and is skipped).
    Const-looking identifiers (UPPER_SNAKE_CASE) are filtered — see
    :func:`_looks_like_const_name`.
    """
    seen: list[str] = []
    seen_set: set[str] = set()

    def add(name: str) -> None:
        if name in generics or name in seen_set:
            return
        if _looks_like_const_name(name):
            return
        seen.append(name)
        seen_set.add(name)

    def visit(n: ts.Node) -> None:
        # Plain `type_identifier` leaves are the names we want, except when
        # they're part of a `scoped_type_identifier` (in which case we
        # already recorded the head).
        if n.type == "type_identifier":
            add(_text(n))
            return
        if n.type == "scoped_type_identifier":
            # Record the *last* identifier (the actual type) and drop the
            # scope path — Phase 1 doesn't resolve scopes. Walk children
            # to also catch generic args inside.
            tids = [c for c in n.named_children if c.type == "type_identifier"]
            if tids:
                add(_text(tids[-1]))
            for c in n.named_children:
                # only descend into type_arguments — the scope path itself
                # has been collapsed
                if c.type == "type_arguments":
                    visit(c)
            return
        if n.type == "function_type":
            # tree-sitter-verus parses ``spec_fn(u8) -> bool`` (and similar
            # Rust function-pointer types) as ``function_type`` whose first
            # named child is a ``type_identifier`` carrying the constructor
            # marker (``spec_fn`` / ``fn``). That marker is not a user type
            # — skip it and recurse only into params / return type.
            seen_head = False
            for c in n.named_children:
                if not seen_head and c.type == "type_identifier":
                    seen_head = True
                    continue
                visit(c)
            return
        if n.type == "primitive_type":
            # Never recorded as user-ref; primitives are leaves.
            return
        if n.type == "lifetime":
            return
        # Generic / reference / pointer / tuple / array / slice / dyn / impl
        # all have nested type expressions — recurse into them.
        for c in n.named_children:
            visit(c)

    visit(node)
    return seen


# ---------------------------------------------------------------------------
# Structured type-expression parsing
# ---------------------------------------------------------------------------


def _parse_type_expr(node: ts.Node, generics: set[str]) -> TypeExpr:
    """Recursively parse a type-expression node into a :class:`TypeExpr`
    tree. Generics bound in the enclosing scope collapse to ``leaf`` /
    ``generic`` with ``head=`` the param name — callers that care can
    filter them out using ``leaves(...)`` and the supplied set.

    Unrecognised or malformed shapes produce a ``"unknown"`` node with
    the raw text preserved, which keeps the walker total.
    """
    if node is None:
        return TypeExpr(kind="unknown", raw="")

    raw = _text(node)
    t = node.type

    if t == "type_identifier":
        name = _text(node)
        return TypeExpr(kind="leaf", head=name, raw=raw)

    if t == "primitive_type":
        return TypeExpr(kind="primitive", head=raw, raw=raw)

    if t == "scoped_type_identifier":
        # Drop the path; keep last ident as the leaf head.
        tids = [c for c in node.named_children if c.type == "type_identifier"]
        head = _text(tids[-1]) if tids else raw
        # Collect args from type_arguments if any.
        ta = _child_by_type(node, "type_arguments")
        if ta is not None:
            args = _parse_type_args(ta, generics)
            return TypeExpr(kind="generic", head=head, args=args, raw=raw)
        return TypeExpr(kind="leaf", head=head, raw=raw)

    if t == "generic_type":
        head_node = (_child_by_type(node, "type_identifier")
                     or _child_by_type(node, "scoped_type_identifier"))
        if head_node is None:
            return TypeExpr(kind="unknown", raw=raw)
        if head_node.type == "scoped_type_identifier":
            tids = [c for c in head_node.named_children
                    if c.type == "type_identifier"]
            head = _text(tids[-1]) if tids else _text(head_node)
        else:
            head = _text(head_node)
        ta = _child_by_type(node, "type_arguments")
        args = _parse_type_args(ta, generics) if ta is not None else []
        return TypeExpr(kind="generic", head=head, args=args, raw=raw)

    if t == "reference_type":
        is_mut = _child_by_type(node, "mutable_specifier") is not None
        # Inner type is the last named non-{lifetime, mutable_specifier} child.
        inner = None
        for c in node.named_children:
            if c.type in ("lifetime", "mutable_specifier"):
                continue
            inner = c
        return TypeExpr(
            kind="ref",
            args=[_parse_type_expr(inner, generics)] if inner else [],
            is_mut=is_mut, raw=raw,
        )

    if t == "pointer_type":
        # `*const T` / `*mut T`. Tree-sitter-verus exposes mutable_specifier
        # for `mut`; the `const` keyword shows up as an unnamed token.
        is_mut = _child_by_type(node, "mutable_specifier") is not None
        inner = None
        for c in node.named_children:
            if c.type == "mutable_specifier":
                continue
            inner = c
        extra = "mut" if is_mut else "const"
        return TypeExpr(
            kind="ptr",
            args=[_parse_type_expr(inner, generics)] if inner else [],
            is_mut=is_mut, extra=extra, raw=raw,
        )

    if t == "tuple_type":
        elements = [_parse_type_expr(c, generics) for c in node.named_children]
        if not elements:
            return TypeExpr(kind="unit", raw=raw)
        return TypeExpr(kind="tuple", args=elements, raw=raw)

    if t == "unit_type":
        return TypeExpr(kind="unit", raw=raw)

    if t == "array_type":
        # `[T; N]` or `[T]`. First named child is the element type; if a
        # second child is present it's the size expression. We don't try
        # to interpret the size — store its raw text in `extra`.
        elem_node = node.named_children[0] if node.named_children else None
        size_text = ""
        if len(node.named_children) > 1:
            size_text = _text(node.named_children[1])
        return TypeExpr(
            kind="array",
            args=[_parse_type_expr(elem_node, generics)] if elem_node else [],
            extra=size_text, raw=raw,
        )

    if t == "function_type":
        # tree-sitter-verus parses `spec_fn(u8) -> bool` with a leading
        # type_identifier marker (`spec_fn` / `fn`). Strip it; collect
        # parameter types and the return type.
        head = ""
        params_node = _child_by_type(node, "parameters")
        params: list[TypeExpr] = []
        if params_node is not None:
            for c in params_node.named_children:
                params.append(_parse_type_expr(c, generics))
        # Return type is the last named child that's not parameters /
        # the head marker.
        ret_node = None
        seen_head = False
        for c in node.named_children:
            if c.type == "type_identifier" and not seen_head:
                head = _text(c)
                seen_head = True
                continue
            if c.type == "parameters":
                continue
            ret_node = c
        ret = (_parse_type_expr(ret_node, generics) if ret_node
               else TypeExpr(kind="unit", raw=""))
        return TypeExpr(kind="fn", head=head, args=params + [ret], raw=raw)

    if t == "dynamic_type":
        # `dyn Trait + 'a`. Trait references look like type_identifier(s)
        # plus optional bounds; collect the type identifiers as args.
        args = []
        for c in node.named_children:
            if c.type == "lifetime":
                continue
            args.append(_parse_type_expr(c, generics))
        return TypeExpr(kind="dyn", args=args, raw=raw)

    if t == "abstract_return_type" or t == "impl_type":
        args = []
        for c in node.named_children:
            if c.type == "lifetime":
                continue
            args.append(_parse_type_expr(c, generics))
        return TypeExpr(kind="impl", args=args, raw=raw)

    if t == "bounded_type":
        # `T + Trait` or `dyn Trait + 'a`. Use the first named child as
        # the principal type.
        if node.named_children:
            return _parse_type_expr(node.named_children[0], generics)
        return TypeExpr(kind="unknown", raw=raw)

    if t in ("identifier", "lifetime"):
        # Const-arg identifier inside type_arguments (parser quirk) or a
        # lifetime — neither contributes to the type tree.
        return TypeExpr(kind="unknown", raw=raw)

    # Fallback — unknown / parser shape we haven't enumerated. Walk
    # children and pick the first parseable one if any, otherwise emit
    # an unknown node.
    for c in node.named_children:
        sub = _parse_type_expr(c, generics)
        if sub.kind != "unknown":
            return sub
    return TypeExpr(kind="unknown", raw=raw, head=raw)


def _parse_type_args(args_node: ts.Node,
                     generics: set[str]) -> list[TypeExpr]:
    """Parse a ``type_arguments`` node into a list of :class:`TypeExpr`.

    The tree-sitter-verus grammar places const arguments here as plain
    ``type_identifier`` (parser quirk — see :func:`_looks_like_const_name`).
    Such entries are kept in the tree but classified as ``unknown`` so
    downstream consumers can either ignore them or report them.
    """
    out: list[TypeExpr] = []
    for c in args_node.named_children:
        if c.type == "type_identifier":
            name = _text(c)
            if _looks_like_const_name(name):
                out.append(TypeExpr(kind="unknown", raw=name, head=name,
                                    extra="const_arg"))
                continue
            if name in generics:
                out.append(TypeExpr(kind="leaf", head=name, raw=name,
                                    extra="generic_param"))
                continue
            out.append(TypeExpr(kind="leaf", head=name, raw=name))
            continue
        if c.type == "lifetime":
            continue
        out.append(_parse_type_expr(c, generics))
    return out


# ---------------------------------------------------------------------------
# Per-item parsing
# ---------------------------------------------------------------------------


def _parse_generics(node: ts.Node) -> tuple[list[GenericParam], set[str]]:
    """Parse a ``type_parameters`` node. Returns the parsed params and the
    set of names that should be filtered out of any field-type ref scan
    (i.e. type+const params; lifetimes are already filtered separately).
    """
    params: list[GenericParam] = []
    bound_names: set[str] = set()
    if node is None or node.type != "type_parameters":
        return params, bound_names
    for c in node.named_children:
        raw = _text(c)
        if c.type == "lifetime_parameter":
            params.append(GenericParam(kind="lifetime", name=raw, raw=raw))
        elif c.type == "type_parameter":
            tid = _child_by_type(c, "type_identifier")
            name = _text(tid) if tid else raw
            tb = _child_by_type(c, "trait_bounds")
            bounds = _collect_type_refs(tb, set()) if tb else []
            params.append(GenericParam(kind="type", name=name, raw=raw,
                                       bounds=bounds))
            bound_names.add(name)
        elif c.type == "const_parameter":
            ids = _children_by_type(c, "identifier")
            name = _text(ids[0]) if ids else raw
            params.append(GenericParam(kind="const", name=name, raw=raw))
            bound_names.add(name)
    return params, bound_names


def _parse_field_declaration(
    fd: ts.Node, generics: set[str]
) -> Optional[FieldDecl]:
    """Parse a ``field_declaration`` (named struct field)."""
    fname = _child_by_type(fd, "field_identifier")
    if not fname:
        return None
    # The field's type is the child after ':'. Find it positionally.
    after_colon = False
    type_node: Optional[ts.Node] = None
    for cc in fd.children:
        if cc.type == ":":
            after_colon = True
            continue
        if after_colon and cc.is_named:
            type_node = cc
            break
    if not type_node:
        return None
    is_pub = _child_by_type(fd, "visibility_modifier") is not None
    return FieldDecl(
        name=_text(fname),
        type_text=_text(type_node),
        type_refs=_collect_type_refs(type_node, generics),
        type_expr=_parse_type_expr(type_node, generics),
        is_pub=is_pub,
        span=(fd.start_byte, fd.end_byte),
    )


def _parse_ordered_fields(
    odl: ts.Node, generics: set[str]
) -> list[FieldDecl]:
    """Parse an ``ordered_field_declaration_list`` (tuple struct / variant).

    Per-field elements are sequences of [optional visibility_modifier]
    [optional attribute_item*] <type-expr>. Positional names use indices.
    """
    out: list[FieldDecl] = []
    pending_pub = False
    for c in odl.children:
        if c.type == "visibility_modifier":
            pending_pub = True
            continue
        if c.type == "attribute_item":
            continue
        if not c.is_named:
            # punctuation '(' ',' ')'
            continue
        if c.type in ("visibility_modifier", "attribute_item"):
            continue
        # type expression
        out.append(FieldDecl(
            name=str(len(out)),
            type_text=_text(c),
            type_refs=_collect_type_refs(c, generics),
            type_expr=_parse_type_expr(c, generics),
            is_pub=pending_pub,
            span=(c.start_byte, c.end_byte),
        ))
        pending_pub = False
    return out


def _parse_variants(
    vl: ts.Node, generics: set[str]
) -> list[VariantDecl]:
    out: list[VariantDecl] = []
    for c in vl.named_children:
        if c.type != "enum_variant":
            continue
        name_node = _child_by_type(c, "identifier") or _child_by_type(c, "type_identifier")
        if not name_node:
            continue
        name = _text(name_node)
        # Variant body: nothing → unit; ordered_field_declaration_list → tuple;
        # field_declaration_list → struct-shaped.
        odl = _child_by_type(c, "ordered_field_declaration_list")
        fdl = _child_by_type(c, "field_declaration_list")
        if fdl is not None:
            fields: list[FieldDecl] = []
            for fc in fdl.named_children:
                if fc.type == "field_declaration":
                    fld = _parse_field_declaration(fc, generics)
                    if fld:
                        fields.append(fld)
            out.append(VariantDecl(name=name, kind="struct", fields=fields))
        elif odl is not None:
            out.append(VariantDecl(
                name=name, kind="tuple",
                fields=_parse_ordered_fields(odl, generics)))
        else:
            out.append(VariantDecl(name=name, kind="unit", fields=[]))
    return out


def _parse_struct_item(
    node: ts.Node, qpath: list[str], src_file: str
) -> TypeDef:
    """Parse a ``struct_item`` (named-fields, tuple, or unit struct)."""
    name = _text(_child_by_type(node, "type_identifier"))
    tp = _child_by_type(node, "type_parameters")
    generics, bound = _parse_generics(tp)
    fields: list[FieldDecl] = []
    fdl = _child_by_type(node, "field_declaration_list")
    odl = _child_by_type(node, "ordered_field_declaration_list")
    if fdl is not None:
        for fc in fdl.named_children:
            if fc.type == "field_declaration":
                fld = _parse_field_declaration(fc, bound)
                if fld:
                    fields.append(fld)
    elif odl is not None:
        fields = _parse_ordered_fields(odl, bound)
    vis_node = _child_by_type(node, "visibility_modifier")
    return TypeDef(
        name=name,
        qualified_name="::".join(qpath + [name]),
        kind="struct",
        generics=generics,
        fields=fields,
        visibility=_text(vis_node) if vis_node else "",
        source_file=src_file,
        source_line=node.start_point[0] + 1,
    )


def _parse_enum_item(
    node: ts.Node, qpath: list[str], src_file: str
) -> TypeDef:
    name = _text(_child_by_type(node, "type_identifier"))
    tp = _child_by_type(node, "type_parameters")
    generics, bound = _parse_generics(tp)
    variants: list[VariantDecl] = []
    vl = _child_by_type(node, "enum_variant_list")
    if vl is not None:
        variants = _parse_variants(vl, bound)
    vis_node = _child_by_type(node, "visibility_modifier")
    return TypeDef(
        name=name,
        qualified_name="::".join(qpath + [name]),
        kind="enum",
        generics=generics,
        variants=variants,
        visibility=_text(vis_node) if vis_node else "",
        source_file=src_file,
        source_line=node.start_point[0] + 1,
    )


def _parse_union_item(
    node: ts.Node, qpath: list[str], src_file: str
) -> TypeDef:
    """Parse a ``union_item``. Treated as a struct-shape with a different
    kind — same field-declaration grammar."""
    name = _text(_child_by_type(node, "type_identifier"))
    tp = _child_by_type(node, "type_parameters")
    generics, bound = _parse_generics(tp)
    fields: list[FieldDecl] = []
    fdl = _child_by_type(node, "field_declaration_list")
    if fdl is not None:
        for fc in fdl.named_children:
            if fc.type == "field_declaration":
                fld = _parse_field_declaration(fc, bound)
                if fld:
                    fields.append(fld)
    vis_node = _child_by_type(node, "visibility_modifier")
    return TypeDef(
        name=name,
        qualified_name="::".join(qpath + [name]),
        kind="union",
        generics=generics,
        fields=fields,
        visibility=_text(vis_node) if vis_node else "",
        source_file=src_file,
        source_line=node.start_point[0] + 1,
    )


def _parse_type_alias(
    node: ts.Node, qpath: list[str], src_file: str
) -> TypeDef:
    """Parse a ``type_item`` (e.g. ``pub type Foo<T> = Vec<T>;``)."""
    name = _text(_child_by_type(node, "type_identifier"))
    tp = _child_by_type(node, "type_parameters")
    generics, bound = _parse_generics(tp)
    # The RHS is the last named child (after '=' and the lhs type_identifier).
    target_node: Optional[ts.Node] = None
    seen_eq = False
    for cc in node.children:
        if cc.type == "=":
            seen_eq = True
            continue
        if seen_eq and cc.is_named and cc.type != "type_parameters":
            target_node = cc
    target_text = _text(target_node) if target_node else None
    target_refs = _collect_type_refs(target_node, bound) if target_node else []
    target_expr = _parse_type_expr(target_node, bound) if target_node else None
    vis_node = _child_by_type(node, "visibility_modifier")
    return TypeDef(
        name=name,
        qualified_name="::".join(qpath + [name]),
        kind="alias",
        generics=generics,
        alias_target=target_text,
        alias_target_refs=target_refs,
        alias_target_expr=target_expr,
        visibility=_text(vis_node) if vis_node else "",
        source_file=src_file,
        source_line=node.start_point[0] + 1,
    )


# ---------------------------------------------------------------------------
# Top-level walker
# ---------------------------------------------------------------------------


_ITEM_TYPES = {
    "struct_item", "enum_item", "union_item", "type_item",
}


def _apply_attrs(td: TypeDef, attr_nodes: list[ts.Node]) -> None:
    """Fold pending attribute nodes into the type def's metadata."""
    derives: list[str] = []
    cfgs: list[str] = []
    for a in attr_nodes:
        text = _attr_text(a)
        derives.extend(_parse_derives(text))
        if _is_external_body(text):
            td.is_external_body = True
        if _is_external_type_specification(text):
            td.is_external_type_specification = True
        c = _extract_cfg(text)
        if c is not None:
            cfgs.append(c)
    td.derives.extend(derives)
    td.cfg.extend(cfgs)


def _walk_items(
    node: ts.Node,
    qpath: list[str],
    src_file: str,
    out_types: list[TypeDef],
) -> None:
    """Walk *children of a container* (source_file / verus_block /
    declaration_list). Honours the ``declaration_with_attrs`` wrapper —
    its first children are ``attribute_item``s, the last is the actual
    item (``struct_item`` / ``mod_item`` / ...).
    """
    for child in node.named_children:
        if child.type == "verus_block":
            _walk_items(child, qpath, src_file, out_types)
        elif child.type == "declaration_with_attrs":
            attrs = [c for c in child.named_children if c.type == "attribute_item"]
            inner = next(
                (c for c in child.named_children if c.type != "attribute_item"),
                None,
            )
            if inner is None:
                continue
            _process_inner(inner, qpath, src_file, out_types, attrs)
        else:
            _process_inner(child, qpath, src_file, out_types, [])


def _process_inner(
    inner: ts.Node,
    qpath: list[str],
    src_file: str,
    out_types: list[TypeDef],
    attrs: list[ts.Node],
) -> None:
    if inner.type == "mod_item":
        # Inner mod: descend with extended path.
        name_node = _child_by_type(inner, "identifier")
        modname = _text(name_node) if name_node else "<anon>"
        body = _child_by_type(inner, "declaration_list")
        if body is not None:
            _walk_items(body, qpath + [modname], src_file, out_types)
        return
    if inner.type not in _ITEM_TYPES:
        return
    if inner.type == "struct_item":
        td = _parse_struct_item(inner, qpath, src_file)
    elif inner.type == "enum_item":
        td = _parse_enum_item(inner, qpath, src_file)
    elif inner.type == "union_item":
        td = _parse_union_item(inner, qpath, src_file)
    elif inner.type == "type_item":
        td = _parse_type_alias(inner, qpath, src_file)
    else:
        return
    _apply_attrs(td, attrs)
    out_types.append(td)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_registry(source: str, source_file: str = "<memory>") -> TypeRegistry:
    """Parse *source* and produce a :class:`TypeRegistry` for its type defs."""
    tree = _parser.parse(source.encode())
    if tree.root_node.has_error:
        logger.warning("parse errors in %s — registry may be incomplete", source_file)
    out: list[TypeDef] = []
    _walk_items(tree.root_node, qpath=[], src_file=source_file, out_types=out)

    reg = TypeRegistry(source_file=source_file)
    if tree.root_node.has_error:
        reg.diagnostics.append(
            f"Parse errors in {source_file}; type registry may be incomplete"
        )
    for td in out:
        reg.types[td.qualified_name] = td
        reg.short_names.setdefault(td.name, []).append(td.qualified_name)
        # Edge set: union of refs in every field / variant / alias target.
        edges: set[str] = set()
        for f in td.fields:
            edges.update(f.type_refs)
        for v in td.variants:
            for f in v.fields:
                edges.update(f.type_refs)
        edges.update(td.alias_target_refs)
        # Don't self-loop on the type's own name.
        edges.discard(td.name)
        reg.edges[td.qualified_name] = edges
    return reg


def build_registry_from_file(path: Path) -> TypeRegistry:
    return build_registry(path.read_text(), source_file=str(path))


# ---------------------------------------------------------------------------
# Dependency graph computation
# ---------------------------------------------------------------------------


# Built-in container heads — used by classify_node() to mark a node as a
# pure container (its semantic value is its element type's value, modulo
# multiplicity / nullability). This is the *minimal* set; Phase 2 will
# augment with prelude View info.
_PRELUDE_CONTAINERS = {
    "Vec", "Box", "Rc", "Arc", "Cell", "RefCell", "UnsafeCell",
    "Option", "Result",
    "Map", "Set", "Seq",
    "HashMap", "HashSet", "BTreeMap", "BTreeSet",
    "Ghost", "Tracked", "PointsTo", "PointsToRaw",
    "Array",
}


def _aggregate_short_edges(reg: TypeRegistry) -> dict[str, set[str]]:
    """Collapse the per-qualified-name edge map down to a per-short-name
    edge map. Multiple cfg-gated definitions of the same short name
    contribute their edges as a union — which matches how a downstream
    consumer (witness gen) usually wants to reason about "what types does
    a ``Foo`` depend on".
    """
    short_edges: dict[str, set[str]] = {}
    for qn, edges in reg.edges.items():
        td = reg.types.get(qn)
        if td is None:
            continue
        short_edges.setdefault(td.name, set()).update(edges)
    # Self-references can arise via short-name collapse — drop them so
    # SCCs correctly capture only *non-trivial* cycles.
    for n, e in short_edges.items():
        e.discard(n)
    return short_edges


def _transitive_closure(adj: dict[str, list[str]]) -> dict[str, list[str]]:
    """Compute reachable-from-each-node sets. Iterative DFS, deterministic
    ordering driven by the input adjacency."""
    closure: dict[str, list[str]] = {}
    nodes = list(adj.keys())
    for n in nodes:
        seen: set[str] = set()
        order: list[str] = []
        stack: list[str] = list(reversed(adj.get(n, [])))
        while stack:
            cur = stack.pop()
            if cur in seen or cur == n:
                continue
            seen.add(cur)
            order.append(cur)
            for nxt in reversed(adj.get(cur, [])):
                if nxt not in seen:
                    stack.append(nxt)
        closure[n] = order
    return closure


def _tarjan_scc(adj: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's strongly-connected-components algorithm. Returns SCCs in
    reverse-topological order (leaf SCCs first), each component sorted
    alphabetically for determinism."""
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlinks[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, []):
            if w not in indices:
                strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif w in on_stack:
                lowlinks[v] = min(lowlinks[v], indices[w])
        if lowlinks[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            components.append(sorted(comp))

    # We need to iterate deterministically over input nodes.
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    for v in sorted(adj.keys()):
        if v not in indices:
            strongconnect(v)
    return components


def _condensation_topo(adj: dict[str, list[str]],
                       sccs: list[list[str]]) -> list[str]:
    """Topological order over the SCC condensation, **leaves first**.

    For witness generation we want to build value witnesses bottom-up:
    construct innermost / leaf types first, then assemble outer types
    using them. This function therefore returns the *reverse*
    topological order — sinks (no outgoing edges) appear first, sources
    last. Within each SCC, members appear in the original Tarjan
    component ordering.
    """
    scc_id: dict[str, int] = {}
    for i, comp in enumerate(sccs):
        for n in comp:
            scc_id[n] = i
    cond_adj: dict[int, set[int]] = {i: set() for i in range(len(sccs))}
    for src, dsts in adj.items():
        i = scc_id.get(src)
        if i is None:
            continue
        for d in dsts:
            j = scc_id.get(d)
            if j is None or j == i:
                continue
            cond_adj[i].add(j)
    # Kahn's standard (sources-first):
    indeg = {i: 0 for i in range(len(sccs))}
    for i, dsts in cond_adj.items():
        for j in dsts:
            indeg[j] += 1
    ready = sorted(i for i, d in indeg.items() if d == 0)
    order_components: list[int] = []
    while ready:
        i = ready.pop(0)
        order_components.append(i)
        for j in sorted(cond_adj[i]):
            indeg[j] -= 1
            if indeg[j] == 0:
                ready.append(j)
                ready.sort()
    flat: list[str] = []
    # Reverse to get leaves-first.
    for ci in reversed(order_components):
        flat.extend(sccs[ci])
    return flat


def classify_node(name: str, reg: TypeRegistry,
                  containers: set[str]) -> str:
    """Tag a node with a coarse classification useful for witness
    generation:

    * ``"container"``  — a known prelude container (Vec, Option, Map, ...)
    * ``"primitive"``  — a primitive type
    * ``"alias"``      — defined locally as a ``type`` alias
    * ``"enum"``       — defined locally as an enum
    * ``"union"``      — defined locally as a union
    * ``"struct"``     — defined locally as a struct
    * ``"external"``   — referenced but not defined in this file
                         (likely from prelude / std / other module)
    """
    if name in containers:
        return "container"
    if name in _PRIMITIVES:
        return "primitive"
    qns = reg.short_names.get(name)
    if not qns:
        return "external"
    # Pick the first def for the short name (cfg dups carry the same kind
    # in practice).
    td = reg.types.get(qns[0])
    if td is None:
        return "external"
    return td.kind


def _typeexpr_to_dict(e: Optional[TypeExpr]) -> Optional[dict]:
    if e is None:
        return None
    return {
        "kind": e.kind,
        "head": e.head,
        "raw": e.raw,
        "is_mut": e.is_mut,
        "extra": e.extra,
        "args": [_typeexpr_to_dict(a) for a in e.args],
    }


def compute_dep_graph(reg: TypeRegistry,
                      extra_containers: Optional[Iterable[str]] = None
                      ) -> DepGraph:
    """Build an enriched dependency view over ``reg``.

    Nodes are *short names* (one node per distinct local-or-referenced
    name). Direct edges union over every TypeDef / variant / alias body
    sharing that short name. From those, transitive closures, SCCs, a
    topological order over the SCC condensation, and per-node
    classification are computed.

    ``extra_containers`` lets callers extend the built-in prelude
    container set with project-specific wrappers (``StaticLinkedList``,
    ``ContainerPtr``, etc) once Phase 2 wires View-impl info in.
    """
    containers = set(_PRELUDE_CONTAINERS)
    if extra_containers:
        containers.update(extra_containers)

    short_edges = _aggregate_short_edges(reg)
    # Union all node names (defined + referenced).
    all_nodes: set[str] = set(short_edges.keys())
    for es in short_edges.values():
        all_nodes.update(es)

    forward = {n: sorted(short_edges.get(n, set())) for n in sorted(all_nodes)}

    reverse: dict[str, list[str]] = {n: [] for n in forward}
    for src, dsts in forward.items():
        for d in dsts:
            reverse.setdefault(d, []).append(src)
    for k, v in reverse.items():
        reverse[k] = sorted(set(v))

    forward_closure = _transitive_closure(forward)
    reverse_closure = _transitive_closure(reverse)
    sccs = _tarjan_scc(forward)
    topo = _condensation_topo(forward, sccs)

    classification = {n: classify_node(n, reg, containers) for n in forward}

    # Per-type structured field paths, keyed by qualified name (so we
    # don't lose cfg duplicates). Each entry is the list of
    # field-or-variant-field type expressions — exactly what witness
    # generation will pattern-match on.
    field_paths: dict[str, list[dict]] = {}
    for qn, td in reg.types.items():
        entries: list[dict] = []
        if td.kind == "alias":
            entries.append({
                "where": "alias_target",
                "name": td.name,
                "type_text": td.alias_target or "",
                "type_expr": _typeexpr_to_dict(td.alias_target_expr),
            })
        for f in td.fields:
            entries.append({
                "where": "field",
                "name": f.name,
                "type_text": f.type_text,
                "type_expr": _typeexpr_to_dict(f.type_expr),
            })
        for v in td.variants:
            for f in v.fields:
                entries.append({
                    "where": f"variant:{v.name}",
                    "name": f.name,
                    "type_text": f.type_text,
                    "type_expr": _typeexpr_to_dict(f.type_expr),
                })
        field_paths[qn] = entries

    return DepGraph(
        nodes=sorted(all_nodes),
        forward=forward,
        forward_closure=forward_closure,
        reverse=reverse,
        reverse_closure=reverse_closure,
        sccs=sccs,
        topological_order=topo,
        classification=classification,
        field_paths=field_paths,
    )


def dep_graph_to_dict(dg: DepGraph) -> dict:
    return {
        "nodes": dg.nodes,
        "forward": dg.forward,
        "forward_closure": dg.forward_closure,
        "reverse": dg.reverse,
        "reverse_closure": dg.reverse_closure,
        "sccs": dg.sccs,
        "topological_order": dg.topological_order,
        "classification": dg.classification,
        "field_paths": dg.field_paths,
    }


def compute_project_dep_graphs(root: Path,
                               limit: Optional[int] = None) -> dict:
    """Walk every .rs under ``root`` and emit one combined per-project
    dependency view.

    The result has two keys:

    * ``per_file`` — registry + dep-graph dict keyed by file path
    * ``aggregate`` — a *project-wide* dep graph, computed from the union
      of all per-file edge sets, treating every short name as a single
      node regardless of which file defined it.
    """
    per_file: dict[str, dict] = {}
    project_edges: dict[str, set[str]] = {}
    project_short_names: dict[str, list[str]] = {}
    project_kinds: dict[str, str] = {}

    files = list(_iter_rs_files(root))
    if limit:
        files = files[:limit]

    for f in files:
        try:
            reg = build_registry_from_file(f)
        except Exception as e:
            per_file[str(f)] = {"error": str(e)}
            continue
        try:
            dg = compute_dep_graph(reg)
        except Exception as e:
            per_file[str(f)] = {"error": f"dep_graph: {e}"}
            continue
        per_file[str(f)] = {
            "registry": _registry_to_dict(reg),
            "dep_graph": dep_graph_to_dict(dg),
        }
        for src, dsts in dg.forward.items():
            project_edges.setdefault(src, set()).update(dsts)
        for short, qns in reg.short_names.items():
            project_short_names.setdefault(short, []).extend(qns)
            for qn in qns:
                td = reg.types[qn]
                # First-seen kind wins; we report it as a hint.
                project_kinds.setdefault(short, td.kind)

    # Build a project-wide graph from the unioned edges. We have to fake
    # a TypeRegistry-shape to reuse classify_node; build one inline.
    pseudo_reg = TypeRegistry(source_file=f"<aggregate:{root}>")
    pseudo_reg.short_names = {k: sorted(set(v))
                              for k, v in project_short_names.items()}
    # Insert a dummy TypeDef per short name carrying its first-seen kind.
    for short, kind in project_kinds.items():
        td = TypeDef(name=short, qualified_name=short, kind=kind)
        pseudo_reg.types[short] = td
    pseudo_reg.edges = project_edges
    aggregate_graph = compute_dep_graph(pseudo_reg)

    return {
        "per_file": per_file,
        "aggregate": dep_graph_to_dict(aggregate_graph),
    }


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _registry_to_dict(reg: TypeRegistry) -> dict:
    return {
        "source_file": reg.source_file,
        "types": {qn: asdict(td) for qn, td in reg.types.items()},
        "short_names": reg.short_names,
        "edges": {qn: sorted(es) for qn, es in reg.edges.items()},
        "diagnostics": list(reg.diagnostics),
    }


def registry_to_json(reg: TypeRegistry) -> str:
    return json.dumps(_registry_to_dict(reg), indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Audit — roll up a directory of registries / source files
# ---------------------------------------------------------------------------


def _iter_rs_files(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*.rs")):
        yield p


def audit_project(root: Path, limit: Optional[int] = None) -> dict:
    """Build per-file registries for every .rs under *root* and roll up
    coverage stats for diagnostic purposes.
    """
    files: list[Path] = list(_iter_rs_files(root))
    if limit:
        files = files[:limit]

    total_types = 0
    by_kind: dict[str, int] = {}
    external_body = 0
    external_type_spec = 0
    has_derive_eq = 0
    in_degree: dict[str, int] = {}
    out_degree_sum = 0
    dangling: dict[str, int] = {}   # short name referenced but never defined here
    parse_errors: list[str] = []

    for f in files:
        try:
            reg = build_registry_from_file(f)
        except Exception as e:
            parse_errors.append(f"{f}: {e}")
            continue
        defined_names = set(reg.short_names.keys())
        for qn, td in reg.types.items():
            total_types += 1
            by_kind[td.kind] = by_kind.get(td.kind, 0) + 1
            if td.is_external_body:
                external_body += 1
            if td.is_external_type_specification:
                external_type_spec += 1
            if any(d in ("Eq", "PartialEq") for d in td.derives):
                has_derive_eq += 1
        for qn, edges in reg.edges.items():
            out_degree_sum += len(edges)
            for r in edges:
                in_degree[r] = in_degree.get(r, 0) + 1
                if r not in defined_names and r not in _PRIMITIVES:
                    dangling[r] = dangling.get(r, 0) + 1

    return {
        "files_scanned": len(files),
        "total_types": total_types,
        "by_kind": by_kind,
        "external_body": external_body,
        "external_type_specification": external_type_spec,
        "has_derive_eq_or_partialeq": has_derive_eq,
        "out_degree_sum": out_degree_sum,
        "in_degree_top20": sorted(in_degree.items(), key=lambda x: -x[1])[:20],
        "dangling_top20": sorted(dangling.items(), key=lambda x: -x[1])[:20],
        "parse_errors": parse_errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_build(args: argparse.Namespace) -> int:
    src = Path(args.source).expanduser().resolve()
    reg = build_registry_from_file(src)
    js = registry_to_json(reg)
    if args.out:
        Path(args.out).expanduser().write_text(js)
    else:
        sys.stdout.write(js + "\n")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    summary = audit_project(root, limit=args.limit)
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return 0
    print(f"Audit: {root}")
    print(f"  files scanned       : {summary['files_scanned']}")
    print(f"  total type defs     : {summary['total_types']}")
    print(f"  by kind             : {summary['by_kind']}")
    print(f"  external_body       : {summary['external_body']}")
    print(f"  ext_type_spec       : {summary['external_type_specification']}")
    print(f"  derive(Eq/PartialEq): {summary['has_derive_eq_or_partialeq']}")
    print(f"  out-degree sum      : {summary['out_degree_sum']}")
    print(f"  top-20 in-degree (most-referenced):")
    for n, d in summary['in_degree_top20']:
        print(f"    {d:5d}  {n}")
    print(f"  top-20 dangling refs (referenced but not defined in this corpus):")
    for n, d in summary['dangling_top20']:
        print(f"    {d:5d}  {n}")
    if summary['parse_errors']:
        print(f"  parse_errors        : {len(summary['parse_errors'])}")
        for e in summary['parse_errors'][:5]:
            print(f"    {e}")
    return 0


def _cmd_deps(args: argparse.Namespace) -> int:
    """Compute structured dependency graphs for every .rs in a project,
    plus a project-wide aggregate. Writes JSON to ``--out`` (or stdout).
    """
    root = Path(args.root).expanduser().resolve()
    result = compute_project_dep_graphs(root, limit=args.limit)
    if args.aggregate_only:
        result = {"aggregate": result["aggregate"],
                  "files_scanned": len(result["per_file"])}
    js = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).expanduser().write_text(js)
    else:
        sys.stdout.write(js + "\n")
    if not args.quiet:
        ag = result["aggregate"]
        n_nodes = len(ag["nodes"])
        n_edges = sum(len(v) for v in ag["forward"].values())
        non_trivial_sccs = [c for c in ag["sccs"] if len(c) > 1]
        sys.stderr.write(
            f"deps: {root.name} — "
            f"{n_nodes} nodes / {n_edges} edges / "
            f"{len(non_trivial_sccs)} non-trivial SCCs\n"
        )
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    """Inline self-check exercising every parse path. Returns 0 on success."""
    cases: list[tuple[str, str, callable]] = []

    def case(name, src, check):
        cases.append((name, src, check))

    case("named-fields struct + generics + visibility",
         "verus!{ pub struct Foo<T, const N: usize> { pub a: T, b: Vec<Bar> } }",
         lambda r: (
             r.types["Foo"].kind == "struct"
             and [g.name for g in r.types["Foo"].generics] == ["T", "N"]
             and r.types["Foo"].fields[0].name == "a"
             and r.types["Foo"].fields[0].is_pub
             and r.types["Foo"].fields[0].type_refs == []  # T filtered as generic
             and r.types["Foo"].fields[1].type_refs == ["Vec", "Bar"]
             and r.types["Foo"].visibility == "pub"
         ))

    case("tuple struct",
         "verus!{ pub struct T(pub u32, Bar, Vec<u8>); }",
         lambda r: (
             r.types["T"].kind == "struct"
             and [f.name for f in r.types["T"].fields] == ["0", "1", "2"]
             and r.types["T"].fields[0].is_pub
             and not r.types["T"].fields[1].is_pub
             and r.types["T"].fields[1].type_refs == ["Bar"]
             and r.types["T"].fields[2].type_refs == ["Vec"]
         ))

    case("unit struct",
         "verus!{ pub struct Unit; }",
         lambda r: (
             r.types["Unit"].kind == "struct"
             and r.types["Unit"].fields == []
             and r.edges["Unit"] == set()
         ))

    case("enum with all three variant shapes",
         "verus!{ pub enum E<T> { A, B(u32, Bar), C { x: T, y: Map<usize, Baz> } } }",
         lambda r: (
             r.types["E"].kind == "enum"
             and {v.name: v.kind for v in r.types["E"].variants} == {
                 "A": "unit", "B": "tuple", "C": "struct",
             }
             and r.types["E"].variants[1].fields[1].type_refs == ["Bar"]
             and r.types["E"].variants[2].fields[0].type_refs == []  # T filtered
             and r.types["E"].variants[2].fields[1].type_refs == ["Map", "Baz"]
             and r.edges["E"] == {"Bar", "Map", "Baz"}
         ))

    case("type alias with generics",
         "verus!{ pub type Alias<T> = Vec<Option<T>>; }",
         lambda r: (
             r.types["Alias"].kind == "alias"
             and r.types["Alias"].alias_target == "Vec<Option<T>>"
             and r.types["Alias"].alias_target_refs == ["Vec", "Option"]
             and r.edges["Alias"] == {"Vec", "Option"}
         ))

    case("derives + external_body + cfg",
         "verus!{ "
         "#[derive(Eq, PartialEq, Clone)] "
         "#[verifier(external_body)] "
         "#[cfg(feature = \"x\")] "
         "pub struct Op { x: u8 } "
         "}",
         lambda r: (
             r.types["Op"].is_external_body
             and "Eq" in r.types["Op"].derives
             and "PartialEq" in r.types["Op"].derives
             and "Clone" in r.types["Op"].derives
             and any("cfg" in c for c in r.types["Op"].cfg)
         ))

    case("modern external_body syntax",
         "verus!{ #[verifier::external_body] pub struct Op2 { x: u8 } }",
         lambda r: r.types["Op2"].is_external_body)

    case("external_type_specification",
         "verus!{ #[verifier::external_type_specification] pub struct Spec { x: u8 } }",
         lambda r: r.types["Spec"].is_external_type_specification)

    case("nested mod path",
         "verus!{ mod outer { pub struct Inner { x: u8 } } }",
         lambda r: (
             "outer::Inner" in r.types
             and r.types["outer::Inner"].name == "Inner"
             and r.short_names["Inner"] == ["outer::Inner"]
         ))

    case("generic params filter — bound names not edges",
         "verus!{ pub struct G<A, B: Clone, const K: usize> { x: A, y: B, z: [A; K] } }",
         lambda r: (
             # A, B, K are bound generics. None should appear in edges.
             # Clone (a trait bound) is captured under generics[1].bounds, not
             # in edges, since we don't record bounds as field type refs.
             r.edges["G"] == set()
             and r.types["G"].generics[1].bounds == ["Clone"]
         ))

    case("scoped type identifier — drop scope, keep tail",
         "verus!{ pub struct S { x: crate::a::b::Foo, y: alloc::vec::Vec<u8> } }",
         lambda r: (
             r.types["S"].fields[0].type_refs == ["Foo"]
             and r.types["S"].fields[1].type_refs == ["Vec"]
         ))

    case("UPPER_SNAKE const-name filter (parser bug workaround)",
         "verus!{ pub struct S { x: Array<Page, NUM_PAGES>, y: [u8; MAX_LEN] } }",
         lambda r: (
             "NUM_PAGES" not in r.edges["S"]
             and "MAX_LEN" not in r.edges["S"]
             and r.edges["S"] == {"Array", "Page"}
         ))

    case("self-loop in edges suppressed",
         "verus!{ pub struct LL { next: Option<Box<LL>> } }",
         lambda r: "LL" not in r.edges["LL"] and {"Option", "Box"} <= r.edges["LL"])

    case("function_type marker (spec_fn) not recorded as ref",
         "verus!{ pub struct S { f: spec_fn(u8) -> Bar, g: fn(Baz) -> u8 } }",
         lambda r: (
             "spec_fn" not in r.edges["S"]
             and "fn" not in r.edges["S"]
             and r.edges["S"] == {"Bar", "Baz"}
         ))

    case("duplicate (cfg-gated) types kept under same short name",
         "verus!{ "
         "#[cfg(a)] pub struct D { x: u8 } "
         "#[cfg(b)] pub struct D { x: u32 } "
         "}",
         lambda r: len(r.short_names["D"]) == 2)

    # ---- TypeExpr parsing ----------------------------------------------------

    def _expr_of_field(r, ty, fname):
        td = r.types[ty]
        for f in td.fields:
            if f.name == fname:
                return f.type_expr
        raise KeyError(fname)

    case("type_expr — leaf user type",
         "verus!{ pub struct S { x: Bar } }",
         lambda r: (
             _expr_of_field(r, "S", "x").kind == "leaf"
             and _expr_of_field(r, "S", "x").head == "Bar"
         ))

    case("type_expr — primitive",
         "verus!{ pub struct S { x: u32 } }",
         lambda r: (
             _expr_of_field(r, "S", "x").kind == "primitive"
             and _expr_of_field(r, "S", "x").head == "u32"
         ))

    case("type_expr — generic with args",
         "verus!{ pub struct S { x: Vec<Option<Bar>> } }",
         lambda r: (
             _expr_of_field(r, "S", "x").kind == "generic"
             and _expr_of_field(r, "S", "x").head == "Vec"
             and _expr_of_field(r, "S", "x").args[0].head == "Option"
             and _expr_of_field(r, "S", "x").args[0].args[0].head == "Bar"
         ))

    case("type_expr — &mut T",
         "verus!{ pub struct S { x: &'a mut Bar } }",
         lambda r: (
             _expr_of_field(r, "S", "x").kind == "ref"
             and _expr_of_field(r, "S", "x").is_mut
             and _expr_of_field(r, "S", "x").args[0].head == "Bar"
         ))

    case("type_expr — *const T pointer",
         "verus!{ pub struct S { x: *const Bar } }",
         lambda r: (
             _expr_of_field(r, "S", "x").kind == "ptr"
             and not _expr_of_field(r, "S", "x").is_mut
             and _expr_of_field(r, "S", "x").args[0].head == "Bar"
         ))

    case("type_expr — tuple",
         "verus!{ pub struct S { x: (Bar, Vec<Baz>, u8) } }",
         lambda r: (
             _expr_of_field(r, "S", "x").kind == "tuple"
             and len(_expr_of_field(r, "S", "x").args) == 3
             and _expr_of_field(r, "S", "x").args[0].head == "Bar"
             and _expr_of_field(r, "S", "x").args[1].head == "Vec"
             and _expr_of_field(r, "S", "x").args[2].head == "u8"
         ))

    case("type_expr — array with const size",
         "verus!{ pub struct S { x: [Bar; 4] } }",
         lambda r: (
             _expr_of_field(r, "S", "x").kind == "array"
             and _expr_of_field(r, "S", "x").args[0].head == "Bar"
             and _expr_of_field(r, "S", "x").extra == "4"
         ))

    case("type_expr — leaves() walker collects all user types",
         "verus!{ pub struct S { x: Map<KeyT, Vec<Box<Inner>>> } }",
         lambda r: set(_expr_of_field(r, "S", "x").leaves())
         == {"Map", "KeyT", "Vec", "Box", "Inner"})

    case("type_expr — alias_target_expr populated",
         "verus!{ pub type A = Vec<Option<Bar>>; }",
         lambda r: (
             r.types["A"].alias_target_expr is not None
             and r.types["A"].alias_target_expr.head == "Vec"
             and r.types["A"].alias_target_expr.args[0].head == "Option"
         ))

    # ---- DepGraph: forward/reverse closures, SCCs, topo, classification ------

    case("dep_graph — forward & reverse closure",
         "verus!{ "
         "pub struct Aa { x: Bb } "
         "pub struct Bb { y: Cc } "
         "pub struct Cc { z: u8 } "
         "}",
         lambda r: (
             # Build dep graph and check transitive reachability
             (lambda dg: (
                 set(dg.forward["Aa"]) == {"Bb"}
                 and set(dg.forward_closure["Aa"]) == {"Bb", "Cc"}
                 and set(dg.reverse["Cc"]) == {"Bb"}
                 and set(dg.reverse_closure["Cc"]) == {"Aa", "Bb"}
             ))(compute_dep_graph(r))
         ))

    case("dep_graph — Tarjan SCC catches a cycle",
         "verus!{ "
         "pub struct Aa { x: Box<Bb> } "
         "pub struct Bb { y: Box<Aa> } "
         "}",
         lambda r: (
             (lambda dg: (
                 any(set(comp) == {"Aa", "Bb"} for comp in dg.sccs)
             ))(compute_dep_graph(r))
         ))

    case("dep_graph — topological order: leaves first",
         "verus!{ "
         "pub struct Aa { x: Bb } "
         "pub struct Bb { y: Cc } "
         "pub struct Cc { z: u8 } "
         "}",
         lambda r: (
             (lambda dg, order=compute_dep_graph(r).topological_order: (
                 # Bb must come before Aa; Cc must come before Bb.
                 order.index("Bb") < order.index("Aa")
                 and order.index("Cc") < order.index("Bb")
             ))(compute_dep_graph(r))
         ))

    case("dep_graph — classification labels",
         "verus!{ "
         "pub struct Inner { x: u8 } "
         "pub struct Outer { v: Vec<Inner>, e: External } "
         "pub enum E { A } "
         "pub type Al = Vec<u8>; "
         "}",
         lambda r: (
             (lambda dg: (
                 dg.classification["Inner"] == "struct"
                 and dg.classification["Outer"] == "struct"
                 and dg.classification["E"] == "enum"
                 and dg.classification["Al"] == "alias"
                 and dg.classification["Vec"] == "container"
                 and dg.classification["External"] == "external"
             ))(compute_dep_graph(r))
         ))

    case("dep_graph — field_paths preserves type_expr per field",
         "verus!{ pub struct S { x: Vec<Bar>, y: u8 } }",
         lambda r: (
             (lambda dg: (
                 [e["where"] for e in dg.field_paths["S"]] == ["field", "field"]
                 and dg.field_paths["S"][0]["type_expr"]["kind"] == "generic"
                 and dg.field_paths["S"][0]["type_expr"]["head"] == "Vec"
                 and dg.field_paths["S"][0]["type_expr"]["args"][0]["head"]
                     == "Bar"
             ))(compute_dep_graph(r))
         ))

    fails: list[tuple[str, str]] = []
    passes = 0
    for name, src, check in cases:
        try:
            reg = build_registry(src, "<self_check>")
            ok = check(reg)
        except Exception as e:
            ok = False
            err = str(e)
        else:
            err = "" if ok else "assertion failed"
        if ok:
            passes += 1
            sys.stdout.write(f"  ok    {name}\n")
        else:
            fails.append((name, err))
            sys.stdout.write(f"  FAIL  {name}: {err}\n")
    sys.stdout.write(f"\n{passes}/{len(cases)} passed\n")
    return 0 if not fails else 1


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="Build a per-file type registry.")
    p_build.add_argument("source", help="Path to a .rs source file.")
    p_build.add_argument("--out", default=None, help="Output JSON path "
                         "(stdout if omitted).")
    p_build.set_defaults(func=_cmd_build)

    p_audit = sub.add_parser("audit",
                             help="Roll up per-file registries into a "
                                  "project-wide diagnostic summary.")
    p_audit.add_argument("root", help="Project root containing .rs files.")
    p_audit.add_argument("--limit", type=int, default=None)
    p_audit.add_argument("--json", action="store_true")
    p_audit.set_defaults(func=_cmd_audit)

    p_deps = sub.add_parser("deps",
                            help="Compute structured dep graph (forward "
                                 "/ reverse closure, SCCs, topological "
                                 "order, classification) for a project.")
    p_deps.add_argument("root", help="Project root containing .rs files.")
    p_deps.add_argument("--out", default=None, help="Output JSON path.")
    p_deps.add_argument("--limit", type=int, default=None)
    p_deps.add_argument("--aggregate-only", action="store_true",
                        help="Drop per-file payload; only emit project "
                             "aggregate graph.")
    p_deps.add_argument("--quiet", action="store_true")
    p_deps.set_defaults(func=_cmd_deps)

    p_test = sub.add_parser("test",
                            help="Run inline self-check on the parser path.")
    p_test.set_defaults(func=_cmd_selftest)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
