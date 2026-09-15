"""L3 — scan `impl View for X` / `impl PartialEq for X` / `impl Eq for X` blocks.

Walks Verus source files and records every trait impl that matters for
view-aware equality:

* :class:`ViewImpl`     — ``impl View for X { type V = ...; fn view(...) {...} }``
* :class:`PartialEqImpl` / :class:`EqImpl` — explicit eq impls

Both generic-target (``impl<T> View for Wrap<T>``) and non-generic
forms are captured, with the target's parsed
:class:`specdet.adapters.verus.native.type_registry.TypeExpr` preserved for later
generic-args substitution.

The scanner *also* extracts the body of the ``view(&self) -> V`` method
verbatim — so when the resolver picks L3 it can splice the existing
source into the synthesised spec without re-deriving it.

A project-wide audit subcommand rolls up coverage stats per project and
cross-references with the Phase-1.5 dep graph to estimate how many types
remain for L4 (LLM).
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

from specdet.adapters.verus.native.extract.extractor import (
    _child_by_type,
    _children_by_type,
    _parser,
    _text,
)
from specdet.adapters.verus.native.extract.type_registry import (
    GenericParam,
    TypeExpr,
    TypeRegistry,
    _parse_generics,
    _parse_type_expr,
    build_registry_from_file,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ViewImpl:
    """A discovered ``impl View for <Target>`` block."""
    target_name: str                 # short name (head of Target)
    target_expr: Optional[dict]      # parsed TypeExpr dict (preserves generics)
    impl_generics: list[dict]        # parsed GenericParam dicts from `impl<...>`
    view_assoc_type: str             # text of `type V = ...;` RHS, empty if absent
    view_assoc_type_expr: Optional[dict]  # parsed TypeExpr of the RHS
    view_fn_signature: str           # `closed spec fn view(&self) -> V`
    view_fn_body: str                # raw body of the view fn (between `{` and `}`)
    is_closed: bool                  # closed vs open spec fn
    is_inherent: bool = False        # inherent `impl T { spec fn view(...) }`
    cfg: list[str] = field(default_factory=list)
    source_file: str = ""
    source_line: int = 0


@dataclass
class EqImpl:
    """A discovered ``impl PartialEq for X`` or ``impl Eq for X`` block."""
    target_name: str
    target_expr: Optional[dict]
    impl_generics: list[dict]
    trait_name: str                  # "PartialEq" | "Eq"
    cfg: list[str] = field(default_factory=list)
    source_file: str = ""
    source_line: int = 0


@dataclass
class ImplScan:
    """Per-file impl-scan output."""
    source_file: str
    views: dict[str, list[ViewImpl]] = field(default_factory=dict)
    eqs: dict[str, list[EqImpl]] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)


# Traits we care about. The View trait is the load-bearing one;
# PartialEq / Eq inform whether structural `==` is even meaningful.
_TRACKED_TRAITS = {"View", "PartialEq", "Eq"}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _attrs_around(node: ts.Node) -> list[ts.Node]:
    """Collect ``attribute_item`` siblings preceding ``node`` inside the
    same ``declaration_with_attrs`` wrapper.

    Mirrors :mod:`specdet.adapters.verus.native.type_registry` — Verus attaches
    attributes to *sibling* attribute_item nodes, not children of the
    item itself. We walk back through earlier named children of the
    wrapping ``declaration_with_attrs``.
    """
    parent = node.parent
    if parent is None or parent.type != "declaration_with_attrs":
        return []
    attrs: list[ts.Node] = []
    for c in parent.named_children:
        if c is node:
            break
        if c.type == "attribute_item":
            attrs.append(c)
    return attrs


def _attr_text(attr: ts.Node) -> str:
    return _text(attr).strip("#![] ").strip()


def _extract_cfg(node: ts.Node) -> list[str]:
    cfgs: list[str] = []
    for a in _attrs_around(node):
        t = _attr_text(a)
        if t.startswith("cfg(") or t.startswith("cfg_attr("):
            cfgs.append(t)
    return cfgs


def _parse_target(node: ts.Node, generics: set[str]
                  ) -> tuple[str, Optional[TypeExpr]]:
    """From the *target* slot of an impl_item (either a ``type_identifier``,
    a ``scoped_type_identifier``, or a ``generic_type``), return the
    short target name and parsed :class:`TypeExpr`.
    """
    if node.type == "type_identifier":
        return _text(node), TypeExpr(kind="leaf", head=_text(node),
                                     raw=_text(node))
    if node.type == "scoped_type_identifier":
        tids = [c for c in node.named_children if c.type == "type_identifier"]
        head = _text(tids[-1]) if tids else _text(node)
        return head, TypeExpr(kind="leaf", head=head, raw=_text(node))
    if node.type == "generic_type":
        expr = _parse_type_expr(node, generics)
        return expr.head, expr
    # Fallback
    return _text(node), TypeExpr(kind="unknown", raw=_text(node))


def _parse_trait_name(node: ts.Node) -> str:
    if node.type == "type_identifier":
        return _text(node)
    if node.type == "scoped_type_identifier":
        tids = [c for c in node.named_children if c.type == "type_identifier"]
        return _text(tids[-1]) if tids else _text(node)
    if node.type == "generic_type":
        head = _child_by_type(node, "type_identifier")
        return _text(head) if head else _text(node)
    return _text(node)


def _split_impl_header(impl_node: ts.Node
                       ) -> tuple[Optional[ts.Node], list[ts.Node]]:
    """Return (type_parameters_node_or_None, non-generic-header-children).

    The non-generic-header children are the slot nodes that carry the
    trait + target (or just target for inherent impls). They appear in
    source order before the ``declaration_list``.
    """
    tp: Optional[ts.Node] = None
    rest: list[ts.Node] = []
    for c in impl_node.named_children:
        if c.type == "type_parameters":
            tp = c
        elif c.type == "declaration_list":
            break
        elif c.type == "where_clause":
            continue
        else:
            rest.append(c)
    return tp, rest


def _find_view_assoc_and_fn(decl_list: ts.Node
                            ) -> tuple[Optional[ts.Node], Optional[ts.Node]]:
    """Inside an impl's declaration_list, find the ``type V = ...;``
    assoc type and the ``fn view(&self) -> V`` function. Both are
    wrapped in ``declaration_with_attrs``.
    """
    assoc: Optional[ts.Node] = None
    view_fn: Optional[ts.Node] = None
    for child in decl_list.named_children:
        # unwrap declaration_with_attrs
        inner = child
        if child.type == "declaration_with_attrs":
            for c in child.named_children:
                if c.type in ("type_item", "function_item"):
                    inner = c
                    break
        if inner.type == "type_item":
            assoc = inner
        elif inner.type == "function_item":
            name_id = _child_by_type(inner, "identifier")
            if name_id and _text(name_id) == "view":
                view_fn = inner
    return assoc, view_fn


def _block_body_text(block: ts.Node) -> str:
    """Return the text between the outer ``{`` and ``}`` of a block."""
    txt = _text(block)
    if txt.startswith("{") and txt.endswith("}"):
        return txt[1:-1].strip()
    return txt.strip()


def _is_self_only_view_fn(fn_node: ts.Node) -> bool:
    """True iff ``view`` takes only ``self``/``&self``.

    Some codebases define helper methods named ``view`` that require extra
    context (for example ``subregion.view(wrpm)``). Those are not ordinary
    value views and cannot be rendered as ``(x).view()``/``x@`` by equality
    generation.
    """
    params = _child_by_type(fn_node, "parameters")
    if params is None:
        return False
    real_params = [
        c for c in params.children
        if c.type in ("self_parameter", "parameter")
    ]
    if len(real_params) != 1:
        return False
    return _text(real_params[0]).lstrip().startswith("self") or real_params[0].type == "self_parameter"


def _view_fn_return_text(fn_node: ts.Node) -> tuple[str, Optional[ts.Node]]:
    """Best-effort return type extraction for an inherent ``view`` fn."""
    seen_arrow = False
    for c in fn_node.children:
        if c.type == "->":
            seen_arrow = True
            continue
        if seen_arrow:
            if c.type in ("where_clause", "block"):
                break
            if c.is_named:
                return _text(c), c
    raw = _text(fn_node)
    m = re.search(r"\bfn\s+view(?:\s*<[^>]*>)?\s*\([^)]*\)\s*->\s*([^{;\n]+)", raw)
    if m:
        return m.group(1).strip(), None
    return "", None


# ---------------------------------------------------------------------------
# Per-impl parsing
# ---------------------------------------------------------------------------


def _parse_impl(impl_node: ts.Node, mod_path: list[str],
                source_file: str) -> tuple[Optional[ViewImpl],
                                            Optional[EqImpl]]:
    """Parse a single ``impl_item``. Returns at most one of
    (ViewImpl, EqImpl) depending on the trait.

    In addition to explicit ``impl View for T``, treat an inherent
    self-only ``spec fn view(&self/self) -> V`` as a view provider. This
    matches the equality convention that semantic views are preferred
    whenever source code exposes one, even if the project did not wire it
    through the standard ``View`` trait.
    """
    tp, header = _split_impl_header(impl_node)
    impl_generics, bound = _parse_generics(tp) if tp else ([], set())

    # An impl with two header slots is `impl Trait for Target`. With one
    # slot it's `impl Target { ... }` (inherent).
    if len(header) == 1:
        target_node = header[0]
        target_name, target_expr = _parse_target(target_node, bound)
        decl = _child_by_type(impl_node, "declaration_list")
        if not target_name or decl is None:
            return None, None
        _, view_fn = _find_view_assoc_and_fn(decl)
        if view_fn is None or not _is_self_only_view_fn(view_fn):
            return None, None
        block = _child_by_type(view_fn, "block")
        if block is None:
            return None, None
        sig_end = block.start_byte
        sig_text = view_fn.text[: sig_end - view_fn.start_byte].decode().rstrip()
        body_text = _block_body_text(block)
        publish = _child_by_type(view_fn, "publish")
        is_closed = bool(publish and _text(publish) == "closed")
        assoc_text, assoc_expr_node = _view_fn_return_text(view_fn)
        assoc_expr = _parse_type_expr(assoc_expr_node, bound) if assoc_expr_node is not None else None
        impl = ViewImpl(
            target_name=target_name,
            target_expr=_typeexpr_to_dict(target_expr) if target_expr else None,
            impl_generics=[asdict(g) for g in impl_generics],
            view_assoc_type=assoc_text,
            view_assoc_type_expr=(_typeexpr_to_dict(assoc_expr) if assoc_expr else None),
            view_fn_signature=sig_text,
            view_fn_body=body_text,
            is_closed=is_closed,
            is_inherent=True,
            cfg=_extract_cfg(impl_node),
            source_file=source_file,
            source_line=view_fn.start_point[0] + 1,
        )
        return impl, None
    if len(header) < 2:
        return None, None
    trait_node, target_node = header[0], header[1]
    trait_name = _parse_trait_name(trait_node)
    if trait_name not in _TRACKED_TRAITS:
        return None, None
    target_name, target_expr = _parse_target(target_node, bound)
    if not target_name:
        return None, None

    cfg = _extract_cfg(impl_node)
    line = impl_node.start_point[0] + 1

    if trait_name == "View":
        decl = _child_by_type(impl_node, "declaration_list")
        assoc_text = ""
        assoc_expr_node = None
        sig_text = ""
        body_text = ""
        is_closed = False
        if decl is not None:
            assoc_node, view_fn = _find_view_assoc_and_fn(decl)
            if assoc_node is not None:
                # `type V = <type>;` — RHS is the named child after `=`.
                rhs: Optional[ts.Node] = None
                seen_eq = False
                for cc in assoc_node.children:
                    if cc.type == "=":
                        seen_eq = True
                        continue
                    if seen_eq and cc.is_named:
                        rhs = cc
                if rhs is not None:
                    assoc_text = _text(rhs)
                    assoc_expr_node = rhs
            if view_fn is not None:
                # Signature = everything up to (but not including) the block.
                block = _child_by_type(view_fn, "block")
                if block is not None:
                    sig_end = block.start_byte
                    sig_text = view_fn.text[: sig_end - view_fn.start_byte
                                            ].decode().rstrip()
                    body_text = _block_body_text(block)
                publish = _child_by_type(view_fn, "publish")
                is_closed = bool(publish and _text(publish) == "closed")

        view_assoc_expr = (
            _parse_type_expr(assoc_expr_node, bound)
            if assoc_expr_node is not None else None
        )
        impl = ViewImpl(
            target_name=target_name,
            target_expr=_typeexpr_to_dict(target_expr) if target_expr else None,
            impl_generics=[asdict(g) for g in impl_generics],
            view_assoc_type=assoc_text,
            view_assoc_type_expr=(_typeexpr_to_dict(view_assoc_expr)
                                  if view_assoc_expr else None),
            view_fn_signature=sig_text,
            view_fn_body=body_text,
            is_closed=is_closed,
            is_inherent=False,
            cfg=cfg,
            source_file=source_file,
            source_line=line,
        )
        return impl, None

    # PartialEq / Eq
    eq_impl = EqImpl(
        target_name=target_name,
        target_expr=_typeexpr_to_dict(target_expr) if target_expr else None,
        impl_generics=[asdict(g) for g in impl_generics],
        trait_name=trait_name,
        cfg=cfg,
        source_file=source_file,
        source_line=line,
    )
    return None, eq_impl


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


# ---------------------------------------------------------------------------
# Top-level walker
# ---------------------------------------------------------------------------


def _walk_impls(root: ts.Node, mod_path: list[str], source_file: str,
                out: ImplScan) -> None:
    """Walk all named children of ``root`` looking for ``impl_item``.

    Recurses transparently into ``verus_block``, ``declaration_with_attrs``,
    and ``mod_item`` wrappers — these are the three node types Verus
    source can put between us and an impl_item.
    """
    for child in root.named_children:
        t = child.type
        if t == "impl_item":
            view, eq = _parse_impl(child, mod_path, source_file)
            if view is not None:
                out.views.setdefault(view.target_name, []).append(view)
            if eq is not None:
                out.eqs.setdefault(eq.target_name, []).append(eq)
        elif t in ("verus_block", "declaration_with_attrs"):
            _walk_impls(child, mod_path, source_file, out)
        elif t == "mod_item":
            _walk_mod(child, mod_path, source_file, out)


def _walk_mod(mod_node: ts.Node, mod_path: list[str], source_file: str,
              out: ImplScan) -> None:
    name_node = _child_by_type(mod_node, "identifier")
    name = _text(name_node) if name_node else "<mod>"
    body = _child_by_type(mod_node, "declaration_list")
    if body is not None:
        _walk_impls(body, mod_path + [name], source_file, out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_source(source: str, source_file: str = "<memory>") -> ImplScan:
    tree = _parser.parse(source.encode())
    out = ImplScan(source_file=source_file)
    if tree.root_node.has_error:
        message = f"Parse errors in {source_file}; view impl scan may be incomplete"
        logger.warning(message)
        out.diagnostics.append(message)
    _walk_impls(tree.root_node, mod_path=[], source_file=source_file, out=out)
    return out


def scan_file(path: Path) -> ImplScan:
    return scan_source(path.read_text(), source_file=str(path))


def scan_to_dict(s: ImplScan) -> dict:
    return {
        "source_file": s.source_file,
        "views": {k: [asdict(v) for v in vs] for k, vs in s.views.items()},
        "eqs": {k: [asdict(e) for e in es] for k, es in s.eqs.items()},
        "diagnostics": list(s.diagnostics),
    }


# ---------------------------------------------------------------------------
# Project-wide audit
# ---------------------------------------------------------------------------


def _iter_rs_files(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*.rs")):
        yield p


def audit_project(root: Path, limit: Optional[int] = None) -> dict:
    """Roll up View / PartialEq / Eq impl coverage across a project.

    Cross-references with the Phase-1.5 :class:`TypeRegistry` to compute:

    * number of *defined* user types with at least one View impl
    * number with derive(Eq) / derive(PartialEq)
    * **uncovered** user types (no L1 / L2 / L3 hit) — these are the
      candidates for L4 LLM generation, and their count is the key
      cost-of-LLM signal.
    """
    files = list(_iter_rs_files(root))
    if limit:
        files = files[:limit]

    # Pass 1: aggregate impl scan + registry across the project
    views_by_type: dict[str, list[ViewImpl]] = {}
    eqs_by_type: dict[str, list[EqImpl]] = {}
    defined_short_names: set[str] = set()
    derive_eq: set[str] = set()
    derive_partial_eq: set[str] = set()
    kinds: dict[str, str] = {}
    parse_errors: list[str] = []

    for f in files:
        try:
            reg = build_registry_from_file(f)
            scan = scan_file(f)
        except Exception as e:
            parse_errors.append(f"{f}: {e}")
            continue
        for short, qns in reg.short_names.items():
            defined_short_names.add(short)
            for qn in qns:
                td = reg.types[qn]
                kinds.setdefault(short, td.kind)
                if "Eq" in td.derives:
                    derive_eq.add(short)
                if "PartialEq" in td.derives:
                    derive_partial_eq.add(short)
        for t, impls in scan.views.items():
            views_by_type.setdefault(t, []).extend(impls)
        for t, impls in scan.eqs.items():
            eqs_by_type.setdefault(t, []).extend(impls)

    # Pass 2: classify each defined short name
    n_total = len(defined_short_names)
    have_view = {t for t in defined_short_names if t in views_by_type}
    have_eq_impl = {t for t in defined_short_names if t in eqs_by_type}
    have_derive_eq = {t for t in defined_short_names
                      if t in derive_eq or t in derive_partial_eq}

    # Types that are aliases are an L2 hit for free, so flag them.
    alias_types = {t for t in defined_short_names if kinds.get(t) == "alias"}

    # Heuristic for L3 coverage: a type is "L3-covered" if there exists
    # an impl View whose target name matches its short name (we don't
    # try to align generic args here — the resolver does that).
    l3_covered = have_view
    l1_to_l3_uncovered = (
        defined_short_names - alias_types - l3_covered
    )

    return {
        "files_scanned": len(files),
        "total_defined_types": n_total,
        "by_kind": _count_by_kind(kinds, defined_short_names),
        "have_view_impl": sorted(have_view),
        "have_view_impl_count": len(have_view),
        "have_partialeq_or_eq_impl_count": len(have_eq_impl),
        "have_derive_eq_or_partialeq_count": len(have_derive_eq),
        "alias_types_count": len(alias_types),
        "l1_l2_l3_uncovered_count": len(l1_to_l3_uncovered),
        "l1_l2_l3_uncovered_top50": sorted(l1_to_l3_uncovered)[:50],
        "view_impls_total": sum(len(v) for v in views_by_type.values()),
        "eq_impls_total": sum(len(v) for v in eqs_by_type.values()),
        "parse_errors": parse_errors,
    }


def _count_by_kind(kinds: dict[str, str], names: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in names:
        k = kinds.get(n, "<unknown>")
        out[k] = out.get(k, 0) + 1
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_scan(args: argparse.Namespace) -> int:
    s = scan_file(Path(args.source).expanduser().resolve())
    js = json.dumps(scan_to_dict(s), indent=2, sort_keys=True)
    if args.out:
        Path(args.out).expanduser().write_text(js)
    else:
        sys.stdout.write(js + "\n")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    summary = audit_project(root, limit=args.limit)
    if args.out:
        Path(args.out).expanduser().write_text(
            json.dumps(summary, indent=2, sort_keys=True)
        )
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return 0
    print(f"impl_scanner audit: {root}")
    print(f"  files scanned                : {summary['files_scanned']}")
    print(f"  total defined user types     : {summary['total_defined_types']}")
    print(f"  by kind                      : {summary['by_kind']}")
    print(f"  have_view_impl (L3 hit)      : {summary['have_view_impl_count']}")
    print(f"  derive(Eq|PartialEq)         : {summary['have_derive_eq_or_partialeq_count']}")
    print(f"  alias types (L2 free)        : {summary['alias_types_count']}")
    print(f"  L1+L2+L3 uncovered           : {summary['l1_l2_l3_uncovered_count']}")
    print(f"  view_impls_total             : {summary['view_impls_total']}")
    print(f"  eq_impls_total               : {summary['eq_impls_total']}")
    if summary['have_view_impl']:
        print(f"  example View targets         : {summary['have_view_impl'][:10]}")
    if summary['l1_l2_l3_uncovered_top50']:
        print(f"  uncovered (top50)            : {summary['l1_l2_l3_uncovered_top50']}")
    if summary['parse_errors']:
        print(f"  parse_errors                 : {len(summary['parse_errors'])}")
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    cases: list[tuple[str, str, callable]] = []

    def case(name, src, check):
        cases.append((name, src, check))

    case("plain View impl",
         "verus!{ pub struct Page { x: u32 }"
         " impl View for Page {"
         "   type V = SPage;"
         "   closed spec fn view(&self) -> SPage { SPage { x: self.x as int } }"
         " } }",
         lambda s: (
             "Page" in s.views
             and len(s.views["Page"]) == 1
             and s.views["Page"][0].view_assoc_type == "SPage"
             and "SPage" in s.views["Page"][0].view_fn_body
             and s.views["Page"][0].is_closed
         ))

    case("open spec fn view",
         "verus!{ pub struct A {}"
         " impl View for A {"
         "   type V = int;"
         "   open spec fn view(&self) -> int { 0 }"
         " } }",
         lambda s: (
             "A" in s.views
             and not s.views["A"][0].is_closed
             and s.views["A"][0].view_assoc_type == "int"
         ))

    case("generic impl<T> View for Wrap<T>",
         "verus!{ pub struct Wrap<T> { x: T }"
         " impl<T: Clone> View for Wrap<T> {"
         "   type V = Seq<T>;"
         "   open spec fn view(&self) -> Seq<T> { Seq::empty() }"
         " } }",
         lambda s: (
             "Wrap" in s.views
             and len(s.views["Wrap"][0].impl_generics) == 1
             and s.views["Wrap"][0].impl_generics[0]["name"] == "T"
             and s.views["Wrap"][0].view_assoc_type == "Seq<T>"
             and s.views["Wrap"][0].target_expr["kind"] == "generic"
             and s.views["Wrap"][0].target_expr["head"] == "Wrap"
         ))

    case("PartialEq impl",
         "verus!{ pub struct B {}"
         " impl PartialEq for B { fn eq(&self, other: &Self) -> bool { true } } }",
         lambda s: (
             "B" in s.eqs
             and s.eqs["B"][0].trait_name == "PartialEq"
         ))

    case("Eq impl",
         "verus!{ pub struct C {}"
         " impl Eq for C {} }",
         lambda s: "C" in s.eqs and s.eqs["C"][0].trait_name == "Eq")

    case("inherent impl ignored",
         "verus!{ pub struct D {}"
         " impl D { fn f(&self) {} } }",
         lambda s: "D" not in s.views and "D" not in s.eqs)

    case("scoped target type (drop scope path)",
         "verus!{ "
         " impl View for crate::a::b::Page { type V = int;"
         "   closed spec fn view(&self) -> int { 0 } } }",
         lambda s: "Page" in s.views)

    case("cfg-gated impl picks up cfg attr",
         "verus!{ pub struct E {}"
         " #[cfg(feature = \"x\")]"
         " impl View for E { type V = int;"
         "   closed spec fn view(&self) -> int { 0 } } }",
         lambda s: (
             "E" in s.views and any("cfg" in c for c in s.views["E"][0].cfg)
         ))

    case("nested mod impl found",
         "verus!{ mod outer { pub struct F {}"
         "   impl View for F { type V = int;"
         "     closed spec fn view(&self) -> int { 0 } } } }",
         lambda s: "F" in s.views)

    case("multiple impls under same target name aggregated",
         "verus!{ pub struct G {}"
         " #[cfg(a)] impl View for G { type V = int; closed spec fn view(&self) -> int { 0 } }"
         " #[cfg(b)] impl View for G { type V = nat; closed spec fn view(&self) -> nat { 0 } } }",
         lambda s: len(s.views["G"]) == 2)

    case("view fn body extraction strips outer braces",
         "verus!{ pub struct H {}"
         " impl View for H { type V = int;"
         "   closed spec fn view(&self) -> int { self.f() + 1 } } }",
         lambda s: s.views["H"][0].view_fn_body == "self.f() + 1")

    case("view fn signature captured (without body)",
         "verus!{ pub struct I {}"
         " impl View for I { type V = int;"
         "   closed spec fn view(&self) -> int { 0 } } }",
         lambda s: (
             "closed spec fn view" in s.views["I"][0].view_fn_signature
             and "{" not in s.views["I"][0].view_fn_signature
         ))

    case("untracked trait (Clone) ignored",
         "verus!{ pub struct J {} impl Clone for J { fn clone(&self) -> Self { J{} } } }",
         lambda s: "J" not in s.views and "J" not in s.eqs)

    passes = 0
    fails: list[tuple[str, str]] = []
    for name, src, check in cases:
        try:
            s = scan_source(src, "<self>")
            ok = check(s)
            err = "" if ok else "assertion failed"
        except Exception as e:
            ok = False
            err = f"{type(e).__name__}: {e}"
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

    p_scan = sub.add_parser("scan", help="Scan a single source file.")
    p_scan.add_argument("source")
    p_scan.add_argument("--out", default=None)
    p_scan.set_defaults(func=_cmd_scan)

    p_audit = sub.add_parser("audit",
                             help="Roll up impl coverage for a project.")
    p_audit.add_argument("root")
    p_audit.add_argument("--limit", type=int, default=None)
    p_audit.add_argument("--out", default=None)
    p_audit.add_argument("--json", action="store_true")
    p_audit.set_defaults(func=_cmd_audit)

    p_test = sub.add_parser("test", help="Run inline self-check.")
    p_test.set_defaults(func=_cmd_selftest)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
