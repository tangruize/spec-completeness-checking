"""L1 — built-in container / prelude view rules.

Most Verus prelude container types have a canonical view: ``Vec<T>`` is
viewed as ``Seq<T@>``, ``Option<T>`` as ``Option<T@>``, ``Map<K,V>`` as
``Map<K@, V@>``, ``&T`` / ``&mut T`` / ``Box<T>`` / ``Arc<T>`` / ``Rc<T>``
view transparently (just unwrap), ``Ghost<T>`` / ``Tracked<T>`` unwrap
to ``T@``. Primitive types and ``int`` / ``nat`` view as themselves.

This module encodes those rules as a small functional table operating
on :class:`specdet.adapters.verus.native.type_registry.TypeExpr` trees. The output
is a *rendered* view-type expression plus a *rendered* expression that
applies it to an arbitrary lhs/rhs binding — the two artefacts
:mod:`.registry` needs to splice into the generated equality check.

We deliberately do *not* generate full ``spec fn view(...)`` source
here — the prelude rules are pure type-level rewriting on the form
``T → T_viewed`` and ``e → e_viewed``. That's because the prelude
types already *have* a working ``view()`` in Verus' stdlib; we only
need to drive recursion into their type arguments.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from specdet.adapters.verus.native.extract.type_registry import TypeExpr


@dataclass(frozen=True)
class PreludeRule:
    """A single container / wrapper rule.

    ``head`` matches a :class:`TypeExpr` whose ``head`` field equals
    this string (and ``kind`` ∈ {generic, leaf, ref, ptr, array}). The
    rule fires when invoked with that type-expr, returning a *viewed*
    type-expression and a function that wraps a Verus expression-string
    in the appropriate ``view`` projection.
    """
    head: str
    kind_matches: tuple[str, ...]
    # (type_expr, recurse_view_of_arg) -> viewed_type_expr_text
    view_type: Callable[[TypeExpr, Callable[[TypeExpr], str]], str]
    # (expr_text, type_expr, recurse_view_of_arg) -> viewed_expr_text
    view_expr: Callable[[str, TypeExpr, Callable[[str, TypeExpr], str]], str]


def _arg(e: TypeExpr, i: int) -> Optional[TypeExpr]:
    return e.args[i] if i < len(e.args) else None


# Helpers for the common rendering patterns -------------------------------


def _transparent_type(e: TypeExpr, view_of: Callable[[TypeExpr], str]) -> str:
    """``&T`` / ``Box<T>`` / ``Ghost<T>`` etc — view of inner argument."""
    inner = _arg(e, 0)
    return view_of(inner) if inner else "()"


def _transparent_expr(expr: str, e: TypeExpr,
                      view_of: Callable[[str, TypeExpr], str]) -> str:
    inner = _arg(e, 0)
    # For references the wrapped expression is already a deref-friendly
    # form in Verus spec; we just push the view inwards.
    return view_of(expr, inner) if inner else expr


def _ghost_or_tracked_expr(expr: str, e: TypeExpr,
                           view_of: Callable[[str, TypeExpr], str]) -> str:
    # `Ghost<T>` / `Tracked<T>` — semantically the underlying T.
    # Verus exposes the inner value via `@`.
    inner = _arg(e, 0)
    inner_expr = f"({expr})@"
    return view_of(inner_expr, inner) if inner else inner_expr


# Rule table --------------------------------------------------------------


PRELUDE_RULES: tuple[PreludeRule, ...] = (
    # References expose their value; raw pointers expose pointer data, not memory.
    PreludeRule(
        head="", kind_matches=("ref",),
        view_type=_transparent_type,
        view_expr=lambda expr, e, v: _transparent_expr(f"*({expr})", e, v),
    ),
    PreludeRule(
        head="", kind_matches=("ptr",),
        view_type=lambda e, v: f"vstd::raw_ptr::PtrData<{_arg(e, 0).raw}>" if _arg(e, 0) else "vstd::raw_ptr::PtrData<()>",
        view_expr=lambda expr, e, v: f"({expr})@",
    ),
    # Vec<T> -> Seq<T@>
    PreludeRule(
        head="Vec", kind_matches=("generic",),
        view_type=lambda e, v: f"Seq<{v(_arg(e, 0))}>" if _arg(e, 0) else "Seq<()>",
        view_expr=lambda expr, e, v: f"({expr})@",
    ),
    # Option<T> -> Option<T@>
    PreludeRule(
        head="Option", kind_matches=("generic", "leaf"),
        view_type=lambda e, v: (f"Option<{v(_arg(e, 0))}>" if _arg(e, 0)
                                else "Option<()>"),
        view_expr=lambda expr, e, v: f"({expr})@",
    ),
    # Map<K, V> -> Map<K@, V@>  (Verus `vstd::map::Map` is a spec primitive
    # with no `.view()` method — its view is identity. Emitting `({expr})@`
    # here trips E0599 "no method named `view` on vstd::map::Map<K,V>".)
    PreludeRule(
        head="Map", kind_matches=("generic",),
        view_type=lambda e, v: (
            f"Map<{v(_arg(e, 0))}, {v(_arg(e, 1))}>"
            if _arg(e, 0) and _arg(e, 1) else "Map<(), ()>"),
        view_expr=lambda expr, e, v: f"({expr})",
    ),
    # Seq<T> -> Seq<T@>  (view of Seq is already itself but its elements
    # need viewing)
    PreludeRule(
        head="Seq", kind_matches=("generic",),
        view_type=lambda e, v: (f"Seq<{v(_arg(e, 0))}>" if _arg(e, 0)
                                else "Seq<()>"),
        view_expr=lambda expr, e, v: f"({expr})",
    ),
    # Set<T> -> Set<T@>
    PreludeRule(
        head="Set", kind_matches=("generic",),
        view_type=lambda e, v: (f"Set<{v(_arg(e, 0))}>" if _arg(e, 0)
                                else "Set<()>"),
        view_expr=lambda expr, e, v: f"({expr})",
    ),
    # Box<T> / Rc<T> / Arc<T> -> transparent
    PreludeRule(
        head="Box", kind_matches=("generic",),
        view_type=_transparent_type, view_expr=_transparent_expr,
    ),
    PreludeRule(
        head="Rc", kind_matches=("generic",),
        view_type=_transparent_type, view_expr=_transparent_expr,
    ),
    PreludeRule(
        head="Arc", kind_matches=("generic",),
        view_type=_transparent_type, view_expr=_transparent_expr,
    ),
    # Ghost<T> / Tracked<T> — unwrap via @
    PreludeRule(
        head="Ghost", kind_matches=("generic",),
        view_type=_transparent_type, view_expr=_ghost_or_tracked_expr,
    ),
    PreludeRule(
        head="Tracked", kind_matches=("generic",),
        view_type=_transparent_type, view_expr=_ghost_or_tracked_expr,
    ),
    # Array<T, N> -> Seq<T@>
    PreludeRule(
        head="Array", kind_matches=("generic",),
        view_type=lambda e, v: (f"Seq<{v(_arg(e, 0))}>" if _arg(e, 0)
                                else "Seq<()>"),
        view_expr=lambda expr, e, v: f"({expr})@",
    ),
    # [T; N] / [T] -> Seq<T@>
    PreludeRule(
        head="", kind_matches=("array",),
        view_type=lambda e, v: (f"Seq<{v(_arg(e, 0))}>" if _arg(e, 0)
                                else "Seq<()>"),
        view_expr=lambda expr, e, v: f"({expr})@",
    ),
)


def find_rule(e: TypeExpr) -> Optional[PreludeRule]:
    """Look up the prelude rule that matches type-expression ``e``.

    Match logic: ``kind`` must be in ``rule.kind_matches``; if the rule
    has a non-empty ``head``, the type-expression's ``head`` must equal
    it. Returns ``None`` if no rule applies.
    """
    for r in PRELUDE_RULES:
        if e.kind not in r.kind_matches:
            continue
        if r.head and e.head != r.head:
            continue
        return r
    return None


def is_primitive(e: TypeExpr) -> bool:
    """Primitives view as themselves and don't need a rule."""
    return e.kind == "primitive"


def is_unit(e: TypeExpr) -> bool:
    return e.kind == "unit"
