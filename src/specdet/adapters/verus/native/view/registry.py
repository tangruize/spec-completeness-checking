"""L1+L2+L3 view-resolver — wraps prelude / alias deref / impl_scanner.

Given a :class:`TypeExpr` (from
:mod:`specdet.adapters.verus.native.type_registry`) the :class:`ViewRegistry`
returns a :class:`Resolution` telling the caller:

* which **layer** matched — ``primitive`` / ``unit`` / ``L1`` (prelude
  container) / ``L2`` (alias deref) / ``L3`` (discovered ``impl
  View``) / ``L4`` (an explicitly accepted view) / ``uncovered``.
* a **view-expression renderer** — a function that, given a Verus
  expression text bound to a value of the type, returns a Verus
  expression text for the value's view. For primitives and unit this
  is the identity.
* a best-effort **viewed type text** for diagnostics / type ascription.
* a **rationale** string explaining the decision (which file the impl
  was found in, which alias chain was followed, etc.). Used by the
  audit + by PR-C's diff output.
* a **needs** list — uncovered type names, for the caller to resolve.

The resolver is single-level: ``resolve(Vec<Page>)`` returns a
view-expr that renders ``Vec<Page>`` → ``(x)@`` (a ``Seq<Page>``). It
does **not** recursively rewrite the element type — Verus's default
``Seq`` equality compares elements with ``==``, so deeper view
projection is only needed when the *element* itself is a non-primitive
type whose ensures uses ``@``. PR-C decides where to drill.

There is no discovery provider or disk-cache lookup. ``uncovered`` is a
diagnostic, not an action.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from unicodedata import normalize

from specdet.adapters.verus.native.extract.extractor import _parser
from specdet.adapters.verus.native.extract.type_registry import (
    TypeDef,
    TypeExpr,
    TypeRegistry,
    build_registry,
    build_registry_from_file,
)
from specdet.adapters.verus.native.view import prelude
from specdet.adapters.verus.native.view.impl_scanner import ImplScan, ViewImpl, scan_file, scan_source

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resolution result
# ---------------------------------------------------------------------------


@dataclass
class Resolution:
    """Outcome of resolving a single :class:`TypeExpr`."""
    layer: str                               # see module docstring
    view_type_text: str                      # rendered viewed-type text
    view_expr: Callable[[str], str]          # expr-text → viewed expr-text
    rationale: str = ""
    needs: list[str] = field(default_factory=list)  # uncovered short names
    prelude_decl: Optional[str] = None       # Accepted Verus `impl View …`
                                             # block the caller must emit
                                             # before the equal-fn.

    @property
    def is_resolved(self) -> bool:
        return self.layer != "uncovered"


def _identity(x: str) -> str:
    return x


@dataclass(frozen=True)
class AcceptedView:
    """A caller-approved view; provenance validation belongs to the caller."""
    viewed_type: str
    view_decl: str
    rationale: str = ""
    depends_on_views_of: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceBoundView:
    """A callable spec ``view`` established by the function's source bounds.

    Unlike ``AcceptedView``, this is evidence about an existing method, not an
    impl to inject. A trait's own ``view`` need not implement vstd's ``View``.
    """
    viewed_type: str
    rationale: str
    trait: str
    source_file: str = ""
    source_line: int = 0
    signature: str = ""
    canonical_view: bool = False


def _source_bound_key(text: str) -> tuple[str, ...]:
    tree = _parser.parse(f"type __SourceBound = {text};".encode())
    if tree.root_node.has_error:
        raise ValueError(f"Invalid source-bound type: {text!r}")
    declarations = tree.root_node.named_children
    if len(declarations) != 1:
        raise ValueError(f"Invalid source-bound type: {text!r}")
    items = declarations[0].named_children
    if len(items) != 1 or items[0].type != "type_item":
        raise ValueError(f"Invalid source-bound type: {text!r}")
    node = items[0].child_by_field_name("type")
    if node is None:
        raise ValueError(f"Invalid source-bound type: {text!r}")
    tokens: list[str] = []

    def visit(n) -> None:
        if n.type in {"line_comment", "block_comment"}:
            return
        if not n.children:
            token = n.text.decode()
            if n.type in {"identifier", "type_identifier"}:
                token = normalize("NFC", token.removeprefix("r#"))
            tokens.append(token)
        else:
            for child in n.children:
                visit(child)

    visit(node)
    return tuple(tokens)


# ---------------------------------------------------------------------------
# ViewRegistry
# ---------------------------------------------------------------------------


class ViewRegistry:
    """Layer-1+2+3+4 view resolver.

    Parameters
    ----------
    types : ``dict[short_name, list[TypeDef]]``
        Per-short-name list of registered type definitions (one per
        qualified-name match — handles cfg-gated duplicates). Built by
        :meth:`from_project`.
    scan :
        Aggregated :class:`ImplScan` across the project — the L3 input.
    accepted_views :
        Explicit mapping of exact type text (or an explicitly approved short
        name) to :class:`AcceptedView` or dictionaries containing
        ``viewed_type`` and ``view_decl``. The caller must validate artifact
        provenance before supplying this mapping. No entries are discovered.
    bound_views :
        Source-derived generic/trait method evidence, normally returned by
        ``type_context.bound_views``. No impls or additional bounds are emitted.
    """

    def __init__(self,
                 types: dict[str, list[TypeDef]],
                 scan: ImplScan,
                 accepted_views: Optional[Mapping[str, AcceptedView | Mapping]] = None,
                 diagnostics: Optional[list[str]] = None,
                 *,
                 bound_views: Optional[Mapping[str, SourceBoundView]] = None) -> None:
        self.types_by_short = types
        self.scan = scan
        self.diagnostics = list(diagnostics or []) + list(scan.diagnostics)
        self.bound_views: dict[str, SourceBoundView] = {}
        self._bound_view_keys: dict[tuple[str, ...], SourceBoundView] = {}
        for name, entry in (bound_views or {}).items():
            if (not isinstance(entry, SourceBoundView) or not entry.viewed_type.strip()
                    or not entry.rationale.strip() or not entry.trait.strip()):
                raise ValueError(f"Source-bound view for {name!r} lacks source method evidence")
            key = _source_bound_key(name)
            previous = self._bound_view_keys.get(key)
            if previous is not None and previous != entry:
                raise ValueError(f"Ambiguous source-bound view for {name!r}")
            self.bound_views[name] = entry
            self._bound_view_keys[key] = entry
        self.accepted_views: dict[str, AcceptedView] = {}
        for name, entry in (accepted_views or {}).items():
            if isinstance(entry, Mapping):
                if entry.get("accepted") is False:
                    raise ValueError(f"View for {name!r} was not accepted")
                declared_name = entry.get("type_short", name)
                if declared_name != name:
                    raise ValueError(f"Accepted view name mismatch: {name!r} != {declared_name!r}")
                entry = AcceptedView(
                    viewed_type=entry.get("viewed_type", ""),
                    view_decl=entry.get("view_decl", ""),
                    rationale=entry.get("rationale", ""),
                    depends_on_views_of=tuple(entry.get("depends_on_views_of", ())),
                )
            if not isinstance(entry, AcceptedView) or not entry.view_decl.strip():
                raise ValueError(f"Accepted view for {name!r} has no declaration")
            self.accepted_views[name] = entry

    # --- factory ---------------------------------------------------------

    @classmethod
    def from_sources(
        cls,
        source: str,
        additional_sources: Iterable[str] = (),
        *,
        accepted_views: Optional[Mapping[str, AcceptedView | Mapping]] = None,
        bound_views: Optional[Mapping[str, SourceBoundView]] = None,
    ) -> "ViewRegistry":
        """Resolve only caller-supplied source strings; performs no filesystem I/O."""
        types: dict[str, list[TypeDef]] = {}
        merged = ImplScan(source_file="<sources>")
        diagnostics: list[str] = []
        for index, text in enumerate((source, *additional_sources)):
            label = f"<source:{index}>"
            registry = build_registry(text, source_file=label)
            scan = scan_source(text, source_file=label)
            diagnostics.extend(registry.diagnostics)
            diagnostics.extend(scan.diagnostics)
            for short, names in registry.short_names.items():
                types.setdefault(short, []).extend(registry.types[name] for name in names)
            for name, views in scan.views.items():
                merged.views.setdefault(name, []).extend(views)
            for name, equalities in scan.eqs.items():
                merged.eqs.setdefault(name, []).extend(equalities)
        return cls(types, merged, accepted_views=accepted_views, diagnostics=diagnostics,
                   bound_views=bound_views)

    @classmethod
    def from_project(cls, root: Path,
                     limit: Optional[int] = None,
                     accepted_views: Optional[Mapping[str, AcceptedView | Mapping]] = None,
                     *,
                     bound_views: Optional[Mapping[str, SourceBoundView]] = None,
                     ) -> "ViewRegistry":
        """Build a ``ViewRegistry`` for an entire project directory.

        Walks every ``.rs`` file under ``root``, merges the
        per-file :class:`TypeRegistry` and :class:`ImplScan` into a
        single in-memory registry. Parse failures are logged and retained in
        ``diagnostics``; no implicit cache or provider is consulted.
        """
        root = Path(root)
        if not root.is_dir():
            raise ValueError(f"View project root is not a directory: {root}")
        types_by_short: dict[str, list[TypeDef]] = {}
        merged = ImplScan(source_file=str(root))
        diagnostics: list[str] = []
        files = sorted(root.rglob("*.rs"))
        if limit:
            files = files[:limit]
        for f in files:
            try:
                reg = build_registry_from_file(f)
            except Exception as exc:
                message = f"Cannot parse types in {f}: {exc}"
                logger.warning(message)
                diagnostics.append(message)
                continue
            diagnostics.extend(reg.diagnostics)
            try:
                scan = scan_file(f)
            except Exception as exc:
                message = f"Cannot parse view impls in {f}: {exc}"
                logger.warning(message)
                diagnostics.append(message)
                scan = ImplScan(source_file=str(f))
            diagnostics.extend(scan.diagnostics)
            for short, qns in reg.short_names.items():
                bucket = types_by_short.setdefault(short, [])
                for qn in qns:
                    bucket.append(reg.types[qn])
            for t, impls in scan.views.items():
                merged.views.setdefault(t, []).extend(impls)
            for t, impls in scan.eqs.items():
                merged.eqs.setdefault(t, []).extend(impls)
        return cls(types_by_short, merged, accepted_views=accepted_views,
                   diagnostics=diagnostics, bound_views=bound_views)

    # --- public API ------------------------------------------------------

    def resolve(self, e: TypeExpr,
                visited: frozenset[str] = frozenset()) -> Resolution:
        """Single-level view resolution. ``visited`` tracks short
        names already on the alias-deref stack to guard against
        cycles."""
        if self._bound_view_keys and e.kind in {"leaf", "generic"}:
            try:
                bound = self._bound_view_keys.get(_source_bound_key(e.raw or e.head))
            except ValueError:
                bound = None
            if bound is not None:
                return Resolution(
                    layer="source-bound",
                    view_type_text=bound.viewed_type,
                    view_expr=lambda expr: f"({expr}).view()",
                    rationale=bound.rationale,
                )
        # 1. primitives / unit / unknown shape
        if prelude.is_primitive(e):
            return Resolution(
                layer="primitive",
                view_type_text=e.head or e.raw,
                view_expr=_identity,
                rationale="primitive type — view is identity",
            )
        if prelude.is_unit(e):
            return Resolution(
                layer="unit",
                view_type_text="()",
                view_expr=_identity,
                rationale="unit type — view is identity",
            )

        # 2. L1 prelude rule
        rule = prelude.find_rule(e)
        if rule is not None:
            viewed_type = rule.view_type(e, self._view_type_of)
            view_expr = self._make_l1_view_expr(rule, e)
            return Resolution(
                layer="L1",
                view_type_text=viewed_type,
                view_expr=view_expr,
                rationale=f"prelude rule {rule.head or e.kind!r}",
            )

        # 3. L2 alias deref + L3 impl View (only meaningful for named
        #    type references — leaf / generic kinds with a head name)
        if e.kind in ("leaf", "generic") and e.head:
            if e.head in visited:
                # Alias cycle — bail out, mark as uncovered.
                return Resolution(
                    layer="uncovered",
                    view_type_text=e.raw,
                    view_expr=_identity,
                    rationale=f"alias cycle through {e.head!r}",
                    needs=[e.head],
                )
            # L3 — explicit impl View?
            impls = self.scan.views.get(e.head, [])
            if impls:
                return self._resolve_l3(e, impls)
            # L2 — alias chain?
            decls = self.types_by_short.get(e.head, [])
            alias = next((d for d in decls if d.kind == "alias"), None)
            if alias is not None and alias.alias_target_expr is not None:
                inner = self.resolve(alias.alias_target_expr,
                                     visited | {e.head})
                if inner.is_resolved:
                    return Resolution(
                        layer="L2",
                        view_type_text=inner.view_type_text,
                        view_expr=inner.view_expr,
                        rationale=f"alias {e.head} → {alias.alias_target} "
                                  f"({inner.layer})",
                        needs=inner.needs,
                        prelude_decl=inner.prelude_decl,
                    )
                # An explicitly accepted view may cover the alias itself.
                l4 = self._resolve_l4(e)
                if l4 is not None:
                    return l4
                return Resolution(
                    layer="uncovered",
                    view_type_text=e.raw,
                    view_expr=_identity,
                    rationale=(f"alias {e.head} → {alias.alias_target} "
                               f"unresolved: {inner.rationale}"),
                    needs=inner.needs or [e.head],
                )

            # L4 — only entries explicitly accepted by the caller.
            l4 = self._resolve_l4(e)
            if l4 is not None:
                return l4

        # Fallthrough — struct/enum/union with no L3/L4 hit, fn pointer,
        # dyn/impl trait object, unknown shape.
        head = e.head or e.raw
        return Resolution(
            layer="uncovered",
            view_type_text=e.raw,
            view_expr=_identity,
            rationale=f"no L1/L2/L3/L4 rule for {head} (kind={e.kind})",
            needs=[head] if head else [],
        )

    def view_text(self, expr: str, e: TypeExpr) -> str:
        """Convenience: resolve ``e`` and apply to expression text."""
        return self.resolve(e).view_expr(expr)

    def equal_expr(self, lhs: str, rhs: str,
                   e: TypeExpr) -> Optional[str]:
        """View-aware equality. Returns ``None`` when uncovered — the
        caller (gen_det) should fall back to its structural-equal
        builder.

        For L4 hits the equality text is the same ``(lhs).view() ==
        (rhs).view()`` shape as L3 — the caller must consult
        :meth:`resolve` directly to recover the ``prelude_decl`` that
        defines ``impl View`` for the type.

        The output text is parenthesized to be safe inside arbitrary
        contexts (and-chains, comma lists, etc.).
        """
        r = self.resolve(e)
        if not r.is_resolved:
            return None
        return f"({r.view_expr(lhs)} =~= {r.view_expr(rhs)})"

    # --- helpers ---------------------------------------------------------

    def _view_type_of(self, arg: Optional[TypeExpr]) -> str:
        """Recursion callback passed to L1 prelude rules — renders the
        viewed type for an inner argument.
        """
        if arg is None:
            return "()"
        r = self.resolve(arg)
        return r.view_type_text or arg.raw

    def _make_l1_view_expr(self, rule: "prelude.PreludeRule",
                           e: TypeExpr) -> Callable[[str], str]:
        """Capture ``e`` and the prelude rule, return a deferred
        expression renderer. The renderer's recurse callback is bound
        to :meth:`view_text` so deeper drills also flow through the
        registry.
        """

        def render(expr: str) -> str:
            return rule.view_expr(expr, e, self.view_text)

        return render

    def _resolve_l3(self, e: TypeExpr,
                    impls: list[ViewImpl]) -> Resolution:
        """Use a discovered ``impl View for T`` block.

        We pick the *first* matching impl (cfg-gated duplicates rarely
        diverge in their view semantics). The view-expression is the
        canonical ``({x}).view()`` projection — Verus resolves the
        actual associated type from the impl block.

        For the viewed type text we use the impl's
        ``view_assoc_type`` text verbatim; if the impl is generic and
        the assoc type references the impl's type parameter (e.g.
        ``RepeatN<<C as View>::V>`` for ``impl<C: View> View for
        RepeatN<C>``) we keep the raw text — it's only used for
        diagnostics, Verus does its own type-checking.
        """
        impl = impls[0]
        head = e.head or impl.target_name
        viewed_type = impl.view_assoc_type or f"<{head} as View>::V"

        def render(expr: str) -> str:
            return f"({expr}).view()"

        kind = "inherent view for" if getattr(impl, "is_inherent", False) else "impl View for"
        rationale = (f"{kind} {impl.target_name} "
                     f"({Path(impl.source_file).name}:"
                     f"{impl.source_line})")
        return Resolution(
            layer="L3",
            view_type_text=viewed_type,
            view_expr=render,
            rationale=rationale,
        )

    def _resolve_l4(self, e: TypeExpr) -> Optional[Resolution]:
        """Resolve only the explicit mapping, never a disk cache."""
        if not e.head:
            return None
        entry = self.accepted_views.get(e.raw) or self.accepted_views.get(e.head)
        if entry is None:
            return None
        return self._l4_resolution_from_entry(e, entry)

    @staticmethod
    def _l4_resolution_from_entry(e: TypeExpr,
                                  entry: AcceptedView) -> Resolution:
        """Convert a caller-approved entry into a resolution."""
        viewed_type = entry.viewed_type or f"<{e.head} as View>::V"

        def render(expr: str) -> str:
            return f"({expr}).view()"

        rationale = entry.rationale or f"Explicitly accepted view (deps={entry.depends_on_views_of})"
        return Resolution(
            layer="L4",
            view_type_text=viewed_type,
            view_expr=render,
            rationale=rationale,
            prelude_decl=entry.view_decl,
        )


# ---------------------------------------------------------------------------
# Project-wide audit (single-pass coverage)
# ---------------------------------------------------------------------------


def audit_resolver(reg: ViewRegistry) -> dict:
    """For each registered short name, run :meth:`ViewRegistry.resolve`
    on the synthetic ``TypeExpr(kind="leaf", head=name)`` and bucket
    the result by layer. This is a *first-pass* coverage estimate; the
    actual call-site distribution is computed by PR-C against the
    target ensures.
    """
    counts: dict[str, int] = {}
    by_layer: dict[str, list[str]] = {}
    needs: set[str] = set()
    for short in sorted(reg.types_by_short):
        synth = TypeExpr(kind="leaf", head=short, raw=short)
        r = reg.resolve(synth)
        counts[r.layer] = counts.get(r.layer, 0) + 1
        by_layer.setdefault(r.layer, []).append(short)
        if not r.is_resolved:
            needs.update(r.needs)
    return {
        "by_layer_count": counts,
        "by_layer_examples": {k: v[:15] for k, v in by_layer.items()},
        "uncovered_top50": sorted(by_layer.get("uncovered", []))[:50],
        "uncovered_count": len(by_layer.get("uncovered", [])),
        "needs_top50": sorted(needs)[:50],
    }


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------


def _cmd_selftest(args: argparse.Namespace) -> int:
    from specdet.adapters.verus.native.extract.type_registry import build_registry

    cases: list[tuple[str, Callable[[], bool]]] = []

    def case(name: str, fn: Callable[[], bool]) -> None:
        cases.append((name, fn))

    # --- helpers ---------------------------------------------------------
    def _empty_reg(short_to_decls: dict[str, list[TypeDef]] = None) -> ViewRegistry:
        scan = ImplScan(source_file="<mem>")
        return ViewRegistry(types=short_to_decls or {}, scan=scan)

    def _t(name: str, kind: str = "leaf",
           args: Optional[list[TypeExpr]] = None) -> TypeExpr:
        return TypeExpr(kind=kind, head=name, args=args or [], raw=name)

    # --- L0: primitive / unit -------------------------------------------
    case("primitive resolves to identity",
         lambda: (
             (lambda r: r.layer == "primitive"
              and r.view_expr("x") == "x"
              and r.view_type_text == "u32")(
                 _empty_reg().resolve(TypeExpr(kind="primitive",
                                               head="u32", raw="u32")))
         ))
    case("unit resolves to identity",
         lambda: (
             (lambda r: r.layer == "unit" and r.view_expr("x") == "x")(
                 _empty_reg().resolve(TypeExpr(kind="unit", raw="()")))
         ))

    # --- L1: prelude containers -----------------------------------------
    case("Vec<T> uses @ view",
         lambda: (
             (lambda r: r.layer == "L1"
              and r.view_expr("v") == "(v)@"
              and "Seq<" in r.view_type_text)(
                 _empty_reg().resolve(TypeExpr(
                     kind="generic", head="Vec",
                     args=[_t("u32", kind="primitive")], raw="Vec<u32>")))
         ))
    case("Option<T> uses @ view",
         lambda: (
             (lambda r: r.layer == "L1" and r.view_expr("x") == "(x)@")(
                 _empty_reg().resolve(TypeExpr(
                     kind="generic", head="Option",
                     args=[_t("u32", kind="primitive")], raw="Option<u32>")))
         ))
    case("Map<K,V> is identity (vstd::map::Map is spec primitive, no .view())",
         lambda: (
             (lambda r: r.layer == "L1"
              and r.view_expr("m") == "(m)"
              and "Map<" in r.view_type_text)(
                 _empty_reg().resolve(TypeExpr(
                     kind="generic", head="Map",
                     args=[_t("u32", kind="primitive"),
                           _t("u64", kind="primitive")], raw="Map<u32,u64>")))
         ))
    case("&T is transparent — view delegates to inner",
         lambda: (
             (lambda r: r.layer == "L1"
              and r.view_expr("p") == "*(p)")(
                 _empty_reg().resolve(TypeExpr(
                     kind="ref", args=[_t("u32", kind="primitive")],
                     raw="&u32")))
         ))
    case("Ghost<T> uses inner-via-@",
         lambda: (
             (lambda r: r.layer == "L1" and r.view_expr("g") == "(g)@")(
                 _empty_reg().resolve(TypeExpr(
                     kind="generic", head="Ghost",
                     args=[_t("u32", kind="primitive")], raw="Ghost<u32>")))
         ))

    # --- L2: alias deref -------------------------------------------------
    case("alias to primitive resolves through to primitive",
         lambda: (lambda src: (
             lambda reg: (
                 lambda r: r.layer == "L2"
                 and r.view_expr("p") == "p"
                 and "alias" in r.rationale
             )(reg.resolve(_t("Pcid")))
         )(ViewRegistry(
             types={"Pcid": [build_registry(
                 src).types["Pcid"]]},
             scan=ImplScan(source_file="<m>"))))(
             "verus!{ type Pcid = usize; }"
         ))

    case("alias to Vec<T> resolves to L1",
         lambda: (lambda src: (
             lambda reg: (
                 lambda r: r.layer == "L2" and r.view_expr("p") == "(p)@"
             )(reg.resolve(_t("Bag")))
         )(ViewRegistry(
             types={"Bag": [build_registry(
                 src).types["Bag"]]},
             scan=ImplScan(source_file="<m>"))))(
             "verus!{ type Bag = Vec<u32>; }"
         ))

    case("alias to user type stays uncovered if user type has no impl",
         lambda: (lambda src: (
             lambda reg: (
                 lambda r: r.layer == "uncovered"
                 and "Page" in r.needs
             )(reg.resolve(_t("PageHandle")))
         )(ViewRegistry(
             types={"PageHandle": [build_registry(
                 src).types["PageHandle"]]},
             scan=ImplScan(source_file="<m>"))))(
             "verus!{ type PageHandle = Page; }"
         ))

    # --- L3: discovered impl View ----------------------------------------
    case("L3 — type with impl View resolves via .view()",
         lambda: (
             lambda reg: (
                 lambda r: r.layer == "L3"
                 and r.view_expr("p") == "(p).view()"
                 and "impl View for Page" in r.rationale
             )(reg.resolve(_t("Page")))
         )(ViewRegistry(
             types={"Page": []},
             scan=ImplScan(
                 source_file="<m>",
                 views={"Page": [ViewImpl(
                     target_name="Page", target_expr=None, impl_generics=[],
                     view_assoc_type="SPage", view_assoc_type_expr=None,
                     view_fn_signature="closed spec fn view(&self) -> SPage",
                     view_fn_body="SPage { x: self.x as int }",
                     is_closed=True, cfg=[],
                     source_file="page.rs", source_line=10,
                 )]}))))

    # --- cycles ----------------------------------------------------------
    case("alias self-cycle bails out as uncovered",
         lambda: (lambda src: (
             lambda reg: (
                 lambda r: r.layer == "uncovered" and "cycle" in r.rationale
             )(reg.resolve(_t("A")))
         )(ViewRegistry(
             types={"A": [build_registry(src).types["A"]]},
             scan=ImplScan(source_file="<m>"))))(
             "verus!{ type A = A; }"
         ))

    # --- equal_expr smoke ----------------------------------------------
    case("equal_expr on Vec<u32>",
         lambda: (
             _empty_reg().equal_expr(
                 "lhs", "rhs",
                 TypeExpr(kind="generic", head="Vec",
                          args=[_t("u32", kind="primitive")],
                          raw="Vec<u32>"))
             == "((lhs)@ =~= (rhs)@)"
         ))

    case("equal_expr on primitive",
         lambda: (
             _empty_reg().equal_expr(
                 "lhs", "rhs",
                 TypeExpr(kind="primitive", head="u32", raw="u32"))
             == "(lhs =~= rhs)"
         ))

    case("equal_expr returns None for uncovered struct",
         lambda: (
             _empty_reg().equal_expr(
                 "lhs", "rhs", _t("Page")) is None
         ))

    # --- L4: explicit accepted entries ---------------------------------
    case("Accepted view returns L4 layer with prelude_decl",
         lambda: (
             (lambda r: (r.layer == "L4"
                         and r.prelude_decl == "impl View for Page { …}"
                         and r.view_expr("x") == "(x).view()"
                         and r.view_type_text == "PageView"))(
                 ViewRegistry(
                     types={"Page": []},
                     scan=ImplScan(source_file="<mem>"),
                     accepted_views={
                         "Page": AcceptedView("PageView",
                                              "impl View for Page { …}"),
                     },
                 ).resolve(_t("Page"))
             )
         ))

    case("Missing accepted view falls through to uncovered",
         lambda: (
             (lambda r: r.layer == "uncovered")(
                 ViewRegistry(
                     types={"Page": []},
                     scan=ImplScan(source_file="<mem>"),
                     accepted_views={},
                 ).resolve(_t("Page"))
             )
         ))

    def invalid_view_rejected() -> bool:
        try:
            ViewRegistry({}, ImplScan("<mem>"),
                         accepted_views={"Page": AcceptedView("PageView", "")})
        except ValueError:
            return True
        return False

    case("Empty accepted view declaration is rejected", invalid_view_rejected)

    case("L4 hit pushes prelude_decl via Resolution",
         lambda: (
             (lambda r: r.prelude_decl == "impl View for Foo { type V = u32; }")(
                 ViewRegistry(
                     types={"Foo": []},
                     scan=ImplScan(source_file="<mem>"),
                     accepted_views={
                         "Foo": AcceptedView(
                             "u32",
                             "impl View for Foo { type V = u32; }"),
                     },
                 ).resolve(_t("Foo"))
             )
         ))

    # --- run -------------------------------------------------------------
    passes = 0
    fails: list[tuple[str, str]] = []
    for name, fn in cases:
        try:
            ok = fn()
            err = "" if ok else "assertion failed"
        except Exception as e:
            ok = False
            err = f"{type(e).__name__}: {e}"
        if ok:
            passes += 1
            print(f"  ok    {name}")
        else:
            fails.append((name, err))
            print(f"  FAIL  {name}: {err}")
    print(f"\n{passes}/{len(cases)} passed")
    return 0 if not fails else 1


# ---------------------------------------------------------------------------
# Project-level audit CLI
# ---------------------------------------------------------------------------


def _cmd_audit(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    reg = ViewRegistry.from_project(root, limit=args.limit)
    summary = audit_resolver(reg)
    if args.out:
        Path(args.out).expanduser().write_text(
            json.dumps(summary, indent=2, sort_keys=True))
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return 0
    print(f"resolver audit: {root}")
    bc = summary["by_layer_count"]
    total = sum(bc.values())
    for layer in ("primitive", "unit", "L1", "L2", "L3", "uncovered"):
        n = bc.get(layer, 0)
        pct = 100.0 * n / total if total else 0.0
        print(f"  {layer:10} : {n:4d}  ({pct:5.1f}%)")
    print(f"  total      : {total}")
    if summary["uncovered_top50"]:
        print(f"  uncovered top50: {summary['uncovered_top50']}")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    """Quick: resolve a single type by short name in a project.

    Example: ``python -m specdet.adapters.verus.native.view.registry resolve \\
                 ../verusage/source-projects/atmosphere Page``
    """
    root = Path(args.root).expanduser().resolve()
    reg = ViewRegistry.from_project(root)
    e = TypeExpr(kind="leaf", head=args.type, raw=args.type)
    r = reg.resolve(e)
    print(json.dumps({
        "layer": r.layer,
        "view_type_text": r.view_type_text,
        "view_expr_sample": r.view_expr("x"),
        "rationale": r.rationale,
        "needs": r.needs,
    }, indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_test = sub.add_parser("test")
    p_test.set_defaults(func=_cmd_selftest)
    p_aud = sub.add_parser("audit")
    p_aud.add_argument("root")
    p_aud.add_argument("--limit", type=int, default=None)
    p_aud.add_argument("--out", default=None)
    p_aud.add_argument("--json", action="store_true")
    p_aud.set_defaults(func=_cmd_audit)
    p_res = sub.add_parser("resolve")
    p_res.add_argument("root")
    p_res.add_argument("type")
    p_res.set_defaults(func=_cmd_resolve)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
