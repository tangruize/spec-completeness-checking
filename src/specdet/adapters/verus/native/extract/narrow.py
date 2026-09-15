"""
Module 4: narrow — Type-Guided Witness Narrowing

Core data structure: AssumeTree — a tree where each node holds one assume.
Same-node refinement replaces; different nodes accumulate.

Decorator-based strategy registry. Each strategy is recursive.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .types import (
    TypeKind, TypeInfo, FieldInfo, Param,
    FunctionSpec, Assume, VerifyResult, Witness,
    Symbol, DetCheckSpec,
)
from .predicates import (
    EqPred, RangePred, VariantIsPred, DiscEqPred, BoolPred, StrEqPred,
    SetEmptyPred, SetLenGtPred, LenEqPred, LenRangePred,
    SetContainsPred, SetLiteralPred, NotEqualFnPred,
)
logger = logging.getLogger(__name__)


# ===========================================================================
# SearchCtx — structural interface consumed by the narrow_* strategies
# ===========================================================================
#
# The narrow_* functions below don't care which concrete search driver
# is orchestrating them; they only need an object with `.tree`,
# `.det_spec`, and a `.test_and_set(node, assume, phase="")` method
# that returns True iff the new assume was kept. Today there is exactly
# one implementation: `schema_search.SchemaSearchContext`.
# A kept assume may be exploratory UNKNOWN; the driver owns SAT evidence.
class SearchContext(Protocol):
    tree: "AssumeNode"
    det_spec: DetCheckSpec
    trace: list[dict]

    def test_and_set(self, node: "AssumeNode", assume: Assume, phase: str = "") -> bool: ...


# ===========================================================================
# AssumeTree
# ===========================================================================

@dataclass
class AssumeNode:
    """
    A node in the assume tree.
    
    Each node represents one narrowing dimension for a variable/field.
    Its `assume` is the current constraint at this level.
    Refinement (e.g. [0,8] → [3,4] → 3) replaces `assume` in-place.
    Children represent sub-structure (fields, inner types, elements).
    
    Example tree for `alloc(&mut self) -> Result<usize, Error>`:
    
      root
      ├── pre_self_ (Bitmap)
      │   └── @view (BitmapView)
      │       ├── num_bits: assume(== 8)
      │       └── set_bits: assume(== Set::empty())
      ├── r1 (Result)
      │   ├── [variant]: assume(r1 is Ok)
      │   └── Ok_0 (usize): assume(== 0)
      └── r2 (Result)
          ├── [variant]: assume(r2 is Ok)
          └── Ok_0 (usize): assume(== 1)
    """
    key: str                                    # node identifier within parent
    assume: Optional[Assume] = None             # current constraint (replaced on refinement)
    children: dict[str, "AssumeNode"] = field(default_factory=dict)

    def get_or_create(self, key: str) -> "AssumeNode":
        """Get or create a child node."""
        if key not in self.children:
            self.children[key] = AssumeNode(key=key)
        return self.children[key]

    def collect_assumes(self) -> list[Assume]:
        """DFS: collect all non-None assumes from this subtree."""
        result = []
        if self.assume is not None:
            result.append(self.assume)
        for child in self.children.values():
            result.extend(child.collect_assumes())
        return result

    def __repr__(self):
        parts = [f"AssumeNode({self.key!r}"]
        if self.assume:
            parts.append(f", assume={self.assume.expression!r}")
        if self.children:
            parts.append(f", children={list(self.children.keys())}")
        return "".join(parts) + ")"


# ===========================================================================
# Strategy registry
# ===========================================================================

_registry: dict[TypeKind, Callable] = {}


def strategy_for(*type_kinds: TypeKind):
    """Register a narrowing strategy for one or more type kinds."""
    def decorator(fn):
        for kind in type_kinds:
            _registry[kind] = fn
        return fn
    return decorator


def _no_strategy(ty: TypeInfo, var: str, node: "AssumeNode", ctx: "SearchContext"):
    message = (
        f"No narrow strategy for {ty.kind}:{ty.name} (var={var}); "
        f"narrowing is unsupported for this dimension."
    )
    logger.warning(message)
    if hasattr(ctx, "diagnostics"):
        ctx.diagnostics.append(message)


def narrow(ty: TypeInfo, var: str, node: "AssumeNode", ctx: "SearchContext"):
    """Dispatch to the registered strategy. Each strategy gets its own tree node."""
    handler = _registry.get(ty.kind, _no_strategy)
    handler(ty, var, node, ctx)


# ===========================================================================
# Integer range helpers
# ===========================================================================

_SMALL_UNSIGNED = (0, 17)      # [0, 16] inclusive
_SMALL_SIGNED = (-8, 9)       # [-8, 8] inclusive

_FULL_RANGE: dict[TypeKind, tuple[int, int]] = {
    TypeKind.U8:    (0, 256),
    TypeKind.U16:   (0, 65536),
    TypeKind.U32:   (0, 2**32),
    TypeKind.U64:   (0, 2**64),
    TypeKind.USIZE: (0, 2**64),
    TypeKind.I8:    (-128, 128),
    TypeKind.I16:   (-32768, 32768),
    TypeKind.I32:   (-(2**31), 2**31),
    TypeKind.I64:   (-(2**63), 2**63),
    TypeKind.ISIZE: (-(2**63), 2**63),
    TypeKind.INT:   (-(2**31), 2**31),
}


_INT_RANGE_KINDS = frozenset({
    TypeKind.INT, TypeKind.USIZE, TypeKind.ISIZE,
    TypeKind.U8, TypeKind.U16, TypeKind.U32, TypeKind.U64,
    TypeKind.I8, TypeKind.I16, TypeKind.I32, TypeKind.I64,
})


def _int_range(ty: TypeInfo) -> tuple[int, int]:
    """Small initial range (lo inclusive, hi exclusive)."""
    if (ty.name or "").strip() == "nat":
        return _SMALL_UNSIGNED
    if ty.kind in (TypeKind.U8, TypeKind.U16, TypeKind.U32,
                   TypeKind.U64, TypeKind.USIZE):
        return _SMALL_UNSIGNED
    return _SMALL_SIGNED


def _full_int_range(ty: TypeInfo) -> tuple[int, int]:
    if (ty.name or "").strip() == "nat":
        return (0, 2**32)
    return _FULL_RANGE.get(ty.kind, (-256, 256))


# ===========================================================================
# Concrete strategies
# ===========================================================================

@strategy_for(TypeKind.RESULT)
def narrow_result(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    """Narrow Result<T, E>: try Ok first, then Err if Ok PASS."""
    variant_node = node.get_or_create("variant")
    ok_assume = Assume.from_pred(var, VariantIsPred(var, "Ok"), "variant: Ok")
    if ctx.test_and_set(variant_node, ok_assume):
        # FAIL → nondeterminism with this var as Ok. Narrow Ok inner.
        if ty.type_args:
            inner_node = node.get_or_create("Ok_0")
            narrow(ty.type_args[0], f"{var}->Ok_0", inner_node, ctx)
    else:
        # PASS → Ok doesn't exhibit nondeterminism here. Try Err.
        err_assume = Assume.from_pred(var, VariantIsPred(var, "Err"), "variant: Err")
        if ctx.test_and_set(variant_node, err_assume):
            if len(ty.type_args) > 1:
                inner_node = node.get_or_create("Err_0")
                narrow(ty.type_args[1], f"{var}->Err_0", inner_node, ctx)


@strategy_for(TypeKind.OPTION)
def narrow_option(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    """Narrow Option<T>: try Some first, then None if Some PASS."""
    variant_node = node.get_or_create("variant")
    some_assume = Assume.from_pred(var, VariantIsPred(var, "Some"), "variant: Some")
    if ctx.test_and_set(variant_node, some_assume):
        if ty.type_args:
            inner_node = node.get_or_create("Some_0")
            narrow(ty.type_args[0], f"{var}->Some_0", inner_node, ctx)
    else:
        # PASS → Some doesn't exhibit nondeterminism. Try None.
        none_assume = Assume.from_pred(var, VariantIsPred(var, "None"), "variant: None")
        ctx.test_and_set(variant_node, none_assume)


@strategy_for(TypeKind.ENUM)
def narrow_enum(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    """Narrow an enum.

    For **C-like enums** (unit variants with explicit integer
    discriminants, e.g. ``enum SlabSize { Slab8 = 8, ... }``) we narrow
    on the discriminant integer in a single step. Variant tag
    (``x is Slab8``) and discriminant (``x as int == 8``) are
    equivalent information for C enums; the integer form is closer to
    how the spec's ensures use them (``slab_size as usize == 8``), and
    stays valid across cfg gates that may remove individual variants
    from the active build.

    For general enums (variants with inner data) we pin the variant
    tag, then recursively narrow the inner data.
    """
    if ty.is_c_like_enum():
        disc_node = node.get_or_create("discriminant")
        for variant in ty.variants:
            dv = variant.discriminant
            assert dv is not None  # guaranteed by is_c_like_enum()
            assume = Assume.from_pred(
                var, DiscEqPred(var, dv),
                f"discriminant: {variant.name} = {dv}",
            )
            if ctx.test_and_set(disc_node, assume):
                return
        return

    variant_node = node.get_or_create("variant")
    ambiguous_struct_fields = ty.ambiguous_struct_variant_fields()
    for variant in ty.variants:
        assume = Assume.from_pred(var, VariantIsPred(var, variant.name),
                                  f"variant: {variant.name}")
        if ctx.test_and_set(variant_node, assume):
            if variant.inner:
                if variant.struct_form and variant.inner.fields:
                    # Struct-form variant: fields accessed via ``var->fname``.
                    # Skip per-field narrowing when any field name is
                    # ambiguous across variants — Verus cannot generate the
                    # ``arrow_f`` accessor for those, so emitting the assume
                    # would yield uncompilable det fns. The variant tag is
                    # still pinned via the assume above.
                    if any(fld.name in ambiguous_struct_fields
                           for fld in variant.inner.fields):
                        return
                    # Recurse into each named field independently so the
                    # narrow strategies can probe them.
                    for fld in variant.inner.fields:
                        field_node = node.get_or_create(
                            f"{variant.name}_0_{fld.name}"
                        )
                        narrow(
                            fld.type,
                            f"{var}->{fld.name}",
                            field_node,
                            ctx,
                        )
                else:
                    inner_node = node.get_or_create(f"{variant.name}_0")
                    narrow(variant.inner, f"{var}->{variant.name}_0", inner_node, ctx)
            return


@strategy_for(TypeKind.STRUCT)
def narrow_struct(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    """Narrow a struct: recurse into each field."""
    view = ty.spec_view or ty
    accessor = f"{var}@" if ty.spec_view else var

    for fld in view.fields:
        field_node = node.get_or_create(fld.name)
        narrow(fld.type, f"{accessor}.{fld.name}", field_node, ctx)


@strategy_for(
    TypeKind.INT, TypeKind.USIZE, TypeKind.ISIZE,
    TypeKind.U8, TypeKind.U16, TypeKind.U32, TypeKind.U64,
    TypeKind.I8, TypeKind.I16, TypeKind.I32, TypeKind.I64,
)
def narrow_integer(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    """Narrow integer: small range first, then full type range, then bisect."""
    small_lo, small_hi = _int_range(ty)
    hi_inclusive = small_hi - 1
    small_assume = Assume.from_pred(
        var, RangePred(var, small_lo, hi_inclusive),
        f"small range: [{small_lo}, {hi_inclusive}]",
    )
    if ctx.test_and_set(node, small_assume):
        _bisect_range(var, small_lo, hi_inclusive, node, ctx)
        return

    # Small range PASS — rare case, just bisect full type range directly
    full_lo, full_hi = _full_int_range(ty)
    full_hi_inclusive = full_hi - 1
    full_assume = Assume.from_pred(
        var, RangePred(var, full_lo, full_hi_inclusive),
        f"full range: [{full_lo}, {full_hi_inclusive}]",
    )
    if ctx.test_and_set(node, full_assume):
        _bisect_range(var, full_lo, full_hi_inclusive, node, ctx)


def _bisect_range(var: str, lo: int, hi: int, node: AssumeNode, ctx: "SearchContext") -> int | None:
    """Recursive bisection on an integer variable in [lo, hi]. Returns
    the exact value found, or None.
    """
    if lo == hi:
        accepted = ctx.test_and_set(
            node, Assume.from_pred(var, EqPred(var, lo), f"exact: {lo}")
        )
        return lo if accepted else None

    mid = (lo + hi) // 2
    if lo == mid:
        left = Assume.from_pred(var, EqPred(var, lo), f"exact: {lo}")
    else:
        left = Assume.from_pred(var, RangePred(var, lo, mid), f"range: [{lo}, {mid}]")

    if ctx.test_and_set(node, left):
        if lo == mid:
            return lo  # exact value already confirmed, skip redundant recursion
        return _bisect_range(var, lo, mid, node, ctx)
    else:
        return _bisect_range(var, mid + 1, hi, node, ctx)


def _bisect_len_range(base: str, lo: int, hi: int, node: AssumeNode,
                      ctx: "SearchContext") -> int | None:
    """Same shape as :func:`_bisect_range`, but emits LenEq/LenRange
    preds — used for Set/Seq length narrowing where ``base`` is the
    collection variable (not the ``.len()`` expression).
    """
    var_len_expr = f"{base}.len()"

    if lo == hi:
        accepted = ctx.test_and_set(node, Assume.from_pred(
            var_len_expr, LenEqPred(base, lo), f"len: {lo}"))
        return lo if accepted else None

    mid = (lo + hi) // 2
    if lo == mid:
        left = Assume.from_pred(var_len_expr, LenEqPred(base, lo), f"len: {lo}")
    else:
        left = Assume.from_pred(
            var_len_expr, LenRangePred(base, lo, mid),
            f"len range: [{lo}, {mid}]")

    if ctx.test_and_set(node, left):
        if lo == mid:
            return lo
        return _bisect_len_range(base, lo, mid, node, ctx)
    else:
        return _bisect_len_range(base, mid + 1, hi, node, ctx)


def _narrow_length(base: str, node: AssumeNode, ctx: "SearchContext",
                   max_bound: int = 2 ** 20) -> int | None:
    """Narrow collection length: exact probes for [0..4], then bisect.

    ``base`` is the collection variable (NOT a ``.len()`` expression).
    Returns the exact length found, or None.
    """
    var_len_expr = f"{base}.len()"

    # Phase 1: exact probes for common small values
    EXACT_LIMIT = 4
    for n in range(EXACT_LIMIT + 1):
        assume = Assume.from_pred(var_len_expr, LenEqPred(base, n), f"len: {n}")
        if ctx.test_and_set(node, assume):
            return n

    # Phase 2: not in [0..4], bisect the full range
    lo = EXACT_LIMIT + 1
    full_assume = Assume.from_pred(
        var_len_expr, LenRangePred(base, lo, max_bound),
        f"len range: [{lo}, {max_bound}]",
    )
    if ctx.test_and_set(node, full_assume):
        return _bisect_len_range(base, lo, max_bound, node, ctx)

    return None


@strategy_for(TypeKind.BOOL)
def narrow_bool(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    assume_true = Assume.from_pred(var, BoolPred(var, True), "bool: true")
    if not ctx.test_and_set(node, assume_true):
        ctx.test_and_set(node, Assume.from_pred(var, BoolPred(var, False), "bool: false"))


@strategy_for(TypeKind.SET)
def narrow_set(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    elem_ty = ty.type_args[0] if ty.type_args else TypeInfo(kind=TypeKind.INT, name="int")
    elem_ty_name = elem_ty.name

    # Verus `Set::<T>::len()` returns 0 for BOTH empty and infinite sets, so
    # len-first probing is ambiguous. Instead, split into two disjoint finite
    # cases — `s == empty` or `s.len() > 0` — and skip the "infinite set"
    # witness (Verus admits it, but it carries no useful signal for the
    # developer and cannot be printed concretely).

    # Case 1: empty set
    empty = Assume.from_pred(var, SetEmptyPred(var, elem_ty_name), "set: empty")
    if ctx.test_and_set(node, empty):
        return

    # Case 2: finite non-empty. Establish len > 0 as a precondition; once that
    # sticks, len() is the true cardinality and we can enumerate elements.
    pos_len = Assume.from_pred(var, SetLenGtPred(var), "set: non-empty (finite)")
    if not ctx.test_and_set(node, pos_len):
        # Spec admits neither empty nor any finite non-empty witness — the
        # only remaining nondeterminism is via infinite sets, which we don't
        # try to pin down concretely.
        return

    # Now narrow length. _narrow_length probes from small upward; its results
    # are meaningful because we've already committed to len() > 0.
    len_node = node.get_or_create("len")
    length = _narrow_length(var, len_node, ctx)
    if length is None or length == 0:
        return

    # Non-integer element types: keep empty / non-empty / length witnesses,
    # but don't try to discover concrete elements. `_bisect_set_element`
    # below probes `[-8, 8]` integer literals regardless of `elem_ty`, which
    # for `Set<Foo>` produces ill-typed `s.contains(-8)` / `Set::<Foo>::
    # empty().insert(-8)` assumptions and a Z3 sort mismatch when binding
    # the schema's `k_..._contains: Foo` param to a Python int. Matches the
    # graceful degradation `narrow_map` already does for non-integer keys
    # (`if k_ty.kind not in _INT_RANGE_KINDS: return`).
    if elem_ty.kind not in _INT_RANGE_KINDS:
        return

    # Find elements via contains() probing, skipping already-found values
    elements: list[int] = []
    for i in range(length):
        val = _bisect_set_element(var, elem_ty, node, i, ctx,
                                  skip_vals=frozenset(elements))
        if val is not None:
            elements.append(val)
        else:
            break

    # One final confirmation with full set expression; clears intermediate
    # children only if the confirmation sticks.
    if elements:
        sorted_elems = tuple(sorted(elements))
        desc = ", ".join(str(e) for e in sorted_elems)
        lit_assume = Assume.from_pred(
            var, SetLiteralPred(var, elem_ty_name, sorted_elems),
            f"set: {{{desc}}}",
        )
        if ctx.test_and_set(node, lit_assume):
            node.children.clear()  # len + elem children subsumed by full set expr


def _bisect_set_element(
    var: str,
    elem_ty: TypeInfo,
    parent_node: AssumeNode,
    elem_idx: int,
    ctx: "SearchContext",
    skip_vals: frozenset[int] = frozenset(),
) -> int | None:
    """Find the next element of a set via contains() probing, skipping known elements."""
    elem_node = parent_node.get_or_create(f"elem_{elem_idx}")

    small_lo, small_hi = _int_range(elem_ty)
    found_val = _bisect_contains(var, small_lo, small_hi - 1, elem_node, ctx, skip_vals)

    if found_val is None:
        full_lo, full_hi = _full_int_range(elem_ty)
        found_val = _bisect_contains(var, full_lo, full_hi - 1, elem_node, ctx, skip_vals)

    return found_val


def _bisect_contains(var: str, lo: int, hi: int, node: AssumeNode, ctx: "SearchContext",
                     skip: frozenset[int] = frozenset()) -> int | None:
    """Find a value that the set contains via linear probing, skipping known elements."""
    for val in range(lo, min(lo + 17, hi + 1)):
        if val in skip:
            continue
        assume = Assume.from_pred(var, SetContainsPred(var, val), f"contains {val}")
        if ctx.test_and_set(node, assume):
            return val
    return None


@strategy_for(TypeKind.SEQ)
def narrow_seq(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    """Narrow Seq<T>: length first, then elements.

    ISSUES #14 — when ``ty.spec_view`` is set (Vec<T> tagged with a
    synthetic Seq<T> view by the extractor, signalling "Verus needs
    `@` to project at the spec level"), prepend an ``@`` to the
    accessor so emissions are ``var@.len()`` / ``var@[i]`` rather
    than ``var.len()`` / ``var[i]``. Native Seq<T> / fixed array
    [T; N] / slice [T] keep spec_view=None and use the bare accessor.
    """
    accessor = f"{var}@" if ty.spec_view else var
    len_node = node.get_or_create("len")
    length = _narrow_length(accessor, len_node, ctx)

    if length is None:
        return

    if ty.type_args and length > 0:
        for i in range(length):
            elem_node = node.get_or_create(f"elem_{i}")
            narrow(ty.type_args[0], f"{accessor}[{i}]", elem_node, ctx)


@strategy_for(TypeKind.MAP)
def narrow_map(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    """Narrow Map<K, V>.

    Dimension-separated (NOT <k,v> paired), finite by default (no infinite
    case split — per user decision):
      1. Empty: `m.dom() == Set::<K>::empty()` (implies m is empty).
      2. Domain length: probe `m.dom().len() == n`.
      3. Keys: probe `m.dom().contains(k)` for n keys via Set-element bisect.
      4. Values: for each found key k, recurse narrow on V with var `m[k]`.
         `m.dom().contains(k)` stays in the global assume set, so the value
         narrow runs under that precondition automatically.

    Reuses `SetEmptyPred`/`LenEqPred`/`SetContainsPred` on the virtual
    `{var}.dom()` — no Map-specific predicates needed.
    """
    k_ty = ty.type_args[0] if ty.type_args else TypeInfo(kind=TypeKind.INT, name="int")
    v_ty = ty.type_args[1] if len(ty.type_args) > 1 else TypeInfo(kind=TypeKind.INT, name="int")
    k_ty_name = k_ty.name or "int"

    dom_var = f"{var}.dom()"
    dom_node = node.get_or_create("dom")

    # --- Step 1: empty dom (⇔ empty map) ---
    empty = Assume.from_pred(
        dom_var, SetEmptyPred(dom_var, k_ty_name), "map: empty (dom)")
    if ctx.test_and_set(dom_node, empty):
        return

    # --- Step 2: length probe ---
    # Map is assumed finite by default; skip the SetLenGt case split and go
    # straight to length narrowing.
    len_node = dom_node.get_or_create("len")
    length = _narrow_length(dom_var, len_node, ctx)
    if length is None or length == 0:
        return

    # --- Step 3: key probing (reuse Set element bisect) ---
    if k_ty.kind not in _INT_RANGE_KINDS:
        # Non-integer key types: can still assert dom() length but can't
        # bisect concrete keys. Graceful degradation.
        return

    keys: list[int] = []
    for i in range(length):
        k = _bisect_set_element(dom_var, k_ty, dom_node, i, ctx,
                                skip_vals=frozenset(keys))
        if k is None:
            break
        keys.append(k)

    # --- Step 4: value at each found key ---
    # `m.dom().contains(k)` is already in the global assume conjunction from
    # step 3, so narrow(v_ty, f"{var}[{k}]", ...) emits value assumes under
    # that precondition without us having to thread a prefix.
    for k in keys:
        val_node = node.get_or_create(f"val_{k}")
        narrow(v_ty, f"{var}[{k}]", val_node, ctx)


@strategy_for(TypeKind.UNIT)
def narrow_unit(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    pass


# ---------------------------------------------------------------------------
# PR-F (A-1) — Tracked<T> / Ghost<T> / PointsTo<V> projection-based narrows
# ---------------------------------------------------------------------------

@strategy_for(TypeKind.TRACKED, TypeKind.GHOST)
def narrow_tracked_or_ghost(ty: TypeInfo, var: str, node: AssumeNode,
                            ctx: "SearchContext"):
    """Narrow ``Tracked<T>`` / ``Ghost<T>``.

    Both vstd wrappers expose their inner spec value via ``@`` (i.e.
    ``g@`` has type ``T`` for ``g: Ghost<T>`` and ``g: Tracked<T>``).
    We project once and recurse on the inner ``T``. If the type_args
    are missing (e.g. opaque inner) we fall through to the UNKNOWN
    projection path so any registered ``spec fn`` on the wrapper can
    still drive narrowing.
    """
    if not ty.type_args:
        narrow_unknown(ty, var, node, ctx)
        return
    inner_ty = ty.type_args[0]
    inner_var = f"({var})@"
    inner_node = node.get_or_create("view")
    narrow(inner_ty, inner_var, inner_node, ctx)


@strategy_for(TypeKind.POINTS_TO)
def narrow_points_to(ty: TypeInfo, var: str, node: AssumeNode,
                     ctx: "SearchContext"):
    """Narrow ``vstd::raw_ptr::PointsTo<V>``.

    vstd exposes three spec projections that uniquely characterise a
    ``PointsTo``:

    * ``pt.is_init() -> bool`` — true iff the permission owns an
      initialised value.
    * ``pt.value() -> V`` — the inner value. Semantically meaningful
      only when ``is_init()`` is true; when uninitialised the result
      is an unspecified V (vstd defines ``value`` as a total spec fn).
    * ``pt.ptr().addr() -> usize`` — the pointer address (always valid).

    Probe order (ISSUES #9):

    1. Try ``is_init() == true``. If it sticks, recurse on the inner
       ``V`` via ``(pt).value()`` — those assumes are paired with the
       ``is_init() == true`` precondition in their schemas (see
       ``schemas._emit`` POINTS_TO branch), so activating them in the
       proof forces the same precondition.
    2. Otherwise try ``is_init() == false`` and skip ``value()``
       narrowing entirely — pinning a value() fact while in the
       uninitialised branch is meaningless and (if rendered as a
       guarded assume) would contradict the witness.
    3. Always narrow ``addr()`` — independent of init state.
    """
    name = (ty.name or "").replace(" ", "")
    if "pcell::PointsTo<" in name or "cell::pcell::PointsTo<" in name:
        print(
            f"No narrow strategy for pcell PointsTo permission {ty.name} "
            f"(var={var}); witness will be partial for this dimension."
        )
        return
    init_var = f"({var}).is_init()"
    init_node = node.get_or_create("is_init")

    # 1. Try is_init == true.
    init_true = Assume.from_pred(
        init_var, BoolPred(init_var, True), "is_init: true",
    )
    init_is_true = not ctx.test_and_set(init_node, init_true)

    if init_is_true:
        # 2a. value() — only well-defined under is_init() == true.
        if ty.type_args:
            v_ty = ty.type_args[0]
            v_var = f"({var}).value()"
            v_node = node.get_or_create("value")
            narrow(v_ty, v_var, v_node, ctx)
    else:
        # 2b. is_init=true didn't narrow; commit to is_init=false and
        # skip value() narrowing — meaningless and would contradict
        # the guarded-value schemas if activated together.
        ctx.test_and_set(
            init_node,
            Assume.from_pred(init_var, BoolPred(init_var, False),
                             "is_init: false"),
        )

    # 3. ptr().addr() — usize range narrow, always valid.
    addr_var = f"({var}).ptr().addr()"
    addr_node = node.get_or_create("addr")
    narrow(TypeInfo(kind=TypeKind.USIZE, name="usize"),
           addr_var, addr_node, ctx)


@strategy_for(TypeKind.UNKNOWN)
def narrow_unknown(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    """Narrow an opaque / unresolved type via its registered projections.

    When the extractor couldn't resolve a type structurally (external
    core/std types like ``core::alloc::Layout``) we leave it as
    ``TypeKind.UNKNOWN``. If the det_spec records spec-fn projections
    for this type name (``spec_layout_size(layout) -> usize``, etc.)
    we narrow on each projection's return value. The projection
    expression (``spec_layout_size(layout)``) must be byte-identical
    to what ``schema_search.schemas._emit`` enumerates so the assume
    can match a schema; the ProjectionInfo.call_expr helper guarantees
    this.
    """
    projs = ctx.det_spec.type_projections.get(ty.name)
    if projs is None or not projs.projections:
        _no_strategy(ty, var, node, ctx)
        return

    for proj in projs.projections:
        proj_var = proj.call_expr(var)
        child_node = node.get_or_create(proj.spec_fn)
        narrow(proj.return_type, proj_var, child_node, ctx)


_STR_CANDIDATES = ('""', '"string 1"', '"string 2"')


@strategy_for(TypeKind.STR)
def narrow_str(ty: TypeInfo, var: str, node: AssumeNode, ctx: "SearchContext"):
    """Narrow a string: try the three allowed literal values.

    These are exploratory probes, not an exhaustive string domain.
    """
    for lit in _STR_CANDIDATES:
        # Strip the surrounding quotes to get the raw string value.
        value = lit[1:-1]
        assume = Assume.from_pred(var, StrEqPred(var, value), f'str: {lit}')
        if ctx.test_and_set(node, assume):
            return


def _add_distinctness_witnesses(ctx, det_spec: DetCheckSpec):
    """Probe `!{fn_name}_equal(...)` under the current exploratory constraints.

    Only a SAT query confirms a witness. An UNKNOWN result may retain the
    candidate for exploration, but does not establish nondeterminism.

    This is a single-shot check against the same user-replaceable equal fn
    used by the conclusion, so changing the equality policy only requires
    editing `{fn_name}_equal`.
    """
    if not det_spec.equal_fn_name:
        return
    call_args = []
    for pair in det_spec.equal_arg_pairs:
        call_args.append(pair["lhs"])
        call_args.append(pair["rhs"])
    call = f"{det_spec.equal_fn_name}({', '.join(call_args)})"
    node = ctx.tree.get_or_create("tuple_not_equal")
    assume = Assume.from_pred(
        "tuple",
        NotEqualFnPred(call=call),
        f"distinctness: output tuple not equal under {det_spec.equal_fn_name}",
    )
    ctx.test_and_set(node, assume, phase="distinct")


# ---------------------------------------------------------------------------
# Self-tests — invoke via `python -m specdet.adapters.verus.native.extract.narrow test`.
# These exercise the narrow registry (PR-F additions) using a stub
# SearchContext that records assumes without invoking Z3.
# ---------------------------------------------------------------------------

def _run_self_tests() -> int:
    failures: list[str] = []

    class _StubCtx:
        """Minimal SearchContext: records every (node, assume.expression)
        that narrow_* hands to test_and_set, and lets the caller script
        the True/False replies via a queue."""
        def __init__(self, replies: Optional[list[bool]] = None):
            from specdet.adapters.verus.native.extract.types import DetCheckSpec
            self.tree = AssumeNode(key="root")
            self.det_spec = DetCheckSpec(function="stub", det_check_template="",
                                          equal_fn_name=None, symbols=[])
            self.trace = []
            self._replies = list(replies or [])
            self.recorded: list[tuple[str, str]] = []

        def test_and_set(self, node, assume, phase=""):
            self.recorded.append((node.key, assume.expression))
            node.assume = assume
            if self._replies:
                return self._replies.pop(0)
            # Default: PASS (so the caller stops branching).
            return False

    def expect_recorded(label, recorded, expected_keys, expected_substrs):
        keys = [k for (k, _) in recorded]
        for ek in expected_keys:
            if ek not in keys:
                failures.append(f"{label}: expected node key {ek!r}, got {keys}")
        joined = " | ".join(expr for (_, expr) in recorded)
        for s in expected_substrs:
            if s not in joined:
                failures.append(f"{label}: expected substring {s!r} in:\n  {joined}")

    # Tracked<u32> → recurses on (var)@ as u32 → integer narrow probes.
    u32_ty = TypeInfo(kind=TypeKind.U32, name="u32")
    tracked_u32 = TypeInfo(kind=TypeKind.TRACKED, name="Tracked<u32>",
                           type_args=[u32_ty])
    ctx = _StubCtx()
    narrow(tracked_u32, "t", AssumeNode(key="t"), ctx)
    # Integer narrow tries equality against small values; at least one
    # assume should mention `(t)@`.
    expect_recorded("Tracked<u32> projects through @",
                    ctx.recorded, ["view"], ["(t)@"])

    # Ghost<Seq<u32>> → projects, then Seq narrows length first via len().
    # The "view" node is created but `test_and_set` is invoked on its
    # children (the Seq's `len` node), so we only assert the projected
    # var (g)@ appears in the recorded len-probe assumes.
    seq_u32 = TypeInfo(kind=TypeKind.SEQ, name="Seq<u32>", type_args=[u32_ty])
    ghost_seq = TypeInfo(kind=TypeKind.GHOST, name="Ghost<Seq<u32>>",
                         type_args=[seq_u32])
    ctx = _StubCtx()
    narrow(ghost_seq, "g", AssumeNode(key="g"), ctx)
    expect_recorded("Ghost<Seq<u32>> projects to view then Seq narrow",
                    ctx.recorded, ["len"], ["(g)@"])

    # PointsTo<u32>: emits is_init, value(), ptr().addr() projections.
    points_to_u32 = TypeInfo(kind=TypeKind.POINTS_TO, name="PointsTo<u32>",
                             type_args=[u32_ty])
    ctx = _StubCtx()
    narrow(points_to_u32, "p", AssumeNode(key="p"), ctx)
    expect_recorded("PointsTo<u32> emits is_init/value/ptr().addr",
                    ctx.recorded,
                    ["is_init", "value", "addr"],
                    ["(p).is_init()", "(p).value()", "(p).ptr().addr()"])

    # ISSUES #9 — when is_init=true does not narrow (PASS), narrow_points_to
    # must skip value() recursion entirely (semantically meaningless in the
    # uninitialised branch and would contradict the value() schemas'
    # is_init==true precondition if activated together).
    # replies: is_init=true → PASS (True); is_init=false → FAIL (False kept);
    # addr probes default to False.
    ctx = _StubCtx(replies=[True, False])
    narrow(points_to_u32, "p", AssumeNode(key="p"), ctx)
    keys = [k for (k, _) in ctx.recorded]
    if "value" in keys:
        failures.append(
            "PointsTo: value() narrow must be skipped when is_init=false is "
            f"the kept branch; recorded keys: {keys}"
        )
    if "(p).value()" in " | ".join(expr for (_, expr) in ctx.recorded):
        failures.append(
            "PointsTo: no value() assume should be recorded when is_init=false; "
            f"recorded: {ctx.recorded}"
        )

    # Tracked<T> with no type_args degrades to UNKNOWN's projection path
    # (which warns when type_projections is empty). Should not raise.
    opaque_tracked = TypeInfo(kind=TypeKind.TRACKED, name="Tracked<Foo>")
    ctx = _StubCtx()
    try:
        narrow(opaque_tracked, "t", AssumeNode(key="t"), ctx)
    except Exception as e:
        failures.append(f"Tracked<…> no type_args: raised {e!r}")

    # Registry sanity: all 3 new kinds resolve to a real handler.
    for kind in (TypeKind.TRACKED, TypeKind.GHOST, TypeKind.POINTS_TO):
        handler = _registry.get(kind)
        if handler is None or handler is _no_strategy:
            failures.append(f"No narrow handler registered for {kind}")

    # ISSUES #10 — narrow_set must skip element-level probes for non-integer
    # element types (otherwise it would emit `s.contains(-8)` /
    # `Set::<Foo>::empty().insert(-8)`, which are ill-typed against a
    # `Foo`-sorted Z3 element and produce sort mismatches at schema search).
    foo_ty = TypeInfo(kind=TypeKind.STRUCT, name="Foo")
    set_foo_ty = TypeInfo(kind=TypeKind.SET, name="Set<Foo>", type_args=[foo_ty])
    # replies: empty=F, pos_len=T, len=0=F, len=1=T → length committed to 1
    ctx = _StubCtx(replies=[False, True, False, True])
    narrow(set_foo_ty, "s", AssumeNode(key="s"), ctx)
    joined = " | ".join(expr for (_, expr) in ctx.recorded)
    if ".contains(" in joined or "::empty().insert(" in joined:
        failures.append(
            "Set<Foo>: element-probing must be skipped for non-int elements; "
            f"recorded:\n  {joined}"
        )

    # Control: Set<u32> with the same replies SHOULD reach element probing
    # (verifies the early-return is gated specifically on element kind, not
    # on length narrowing returning None).
    u32_ty_for_set = TypeInfo(kind=TypeKind.U32, name="u32")
    set_u32_ty = TypeInfo(kind=TypeKind.SET, name="Set<u32>", type_args=[u32_ty_for_set])
    ctx = _StubCtx(replies=[False, True, False, True])
    narrow(set_u32_ty, "s", AssumeNode(key="s"), ctx)
    joined = " | ".join(expr for (_, expr) in ctx.recorded)
    if ".contains(" not in joined:
        failures.append(
            "Set<u32>: element-probing regressed for integer elements; "
            f"recorded:\n  {joined}"
        )

    # ISSUES #14 — tuple types are modelled as STRUCT with positional
    # field names "0", "1", ... so the existing narrow_struct strategy
    # decomposes them into per-position accessors `var.0`, `var.1`.
    # Without this, fns returning `(usize, usize)` get a useless witness
    # that only asserts `r1 != r2` without instantiating either side.
    usize_ty = TypeInfo(kind=TypeKind.USIZE, name="usize")
    tuple_uu = TypeInfo(
        kind=TypeKind.STRUCT, name="(usize, usize)",
        fields=[FieldInfo(name="0", type=usize_ty),
                FieldInfo(name="1", type=usize_ty)],
    )
    ctx = _StubCtx()
    narrow(tuple_uu, "r1", AssumeNode(key="r1"), ctx)
    joined = " | ".join(expr for (_, expr) in ctx.recorded)
    if "r1.0" not in joined or "r1.1" not in joined:
        failures.append(
            "Tuple-as-STRUCT: narrow must recurse into r1.0 and r1.1; "
            f"recorded:\n  {joined}"
        )

    # ISSUES #14 — fixed-size / slice array types `[T; N]` / `[T]` are
    # modelled as TypeKind.SEQ with type_args=[T]. Verus accepts direct
    # `arr.len()` / `arr[i]` indexing in spec contexts, so the existing
    # narrow_seq strategy decomposes self.mask: [usize; 8] into per-index
    # probes. Without this, fns with array-typed fields would never have
    # their state instantiated in the witness (mask stayed UNKNOWN, so
    # narrow_struct's recursion into self_.mask dead-ended).
    arr_u32 = TypeInfo(
        kind=TypeKind.SEQ, name="[u32; 4]",
        type_args=[TypeInfo(kind=TypeKind.U32, name="u32")],
    )
    # replies: len: 0=F, 1=F, 2=F, 3=F, 4=T  → length committed to 4
    ctx = _StubCtx(replies=[False, False, False, False, True])
    narrow(arr_u32, "self_.mask", AssumeNode(key="self_.mask"), ctx)
    joined = " | ".join(expr for (_, expr) in ctx.recorded)
    if "self_.mask.len()" not in joined:
        failures.append(
            "Array-as-SEQ: narrow must probe self_.mask.len(); "
            f"recorded:\n  {joined}"
        )
    if "self_.mask[0]" not in joined:
        failures.append(
            "Array-as-SEQ: narrow must recurse into self_.mask[0]; "
            f"recorded:\n  {joined}"
        )

    # ISSUES #14 follow-up — Vec<T> is modelled as TypeKind.SEQ with a
    # synthetic spec_view=Seq<T> tag. narrow_seq must honour that tag
    # and emit `var@.len()` / `var@[i]`, NOT bare `var.len()` / `var[i]`.
    # The latter form is invalid Verus spec syntax on Vec<T>, so the
    # synthesised proof fn would compile-fail. Native Seq<T> / [T] / [T; N]
    # (spec_view=None) must continue to emit bare accessors.
    #
    # This locks in vest::set_range as a permanent regression:
    # before the fix, `pre_data: Vec<u8>` resolved to UNKNOWN and produced
    # a weak 3-assume witness; after the fix it lifts to Seq<u8> at the
    # equal-fn level and Verus closes via the seq-equal axiom (rounds=1,
    # assumes=0, no witness). Companion docs: docs/incompleteness-examples.md §2.
    u8_ty = TypeInfo(kind=TypeKind.U8, name="u8")
    vec_u8 = TypeInfo(
        kind=TypeKind.SEQ, name="Vec<u8>",
        type_args=[u8_ty],
        spec_view=TypeInfo(kind=TypeKind.SEQ, name="Seq<u8>", type_args=[u8_ty]),
    )
    # replies: len: 0=F, 1=F, 2=T → narrow commits length=2
    ctx = _StubCtx(replies=[False, False, True])
    narrow(vec_u8, "pre_data", AssumeNode(key="pre_data"), ctx)
    joined = " | ".join(expr for (_, expr) in ctx.recorded)
    if "pre_data@.len()" not in joined:
        failures.append(
            "Vec<u8> w/ spec_view: must emit pre_data@.len() (the `@` accessor); "
            f"recorded:\n  {joined}"
        )
    if "pre_data@[0]" not in joined:
        failures.append(
            "Vec<u8> w/ spec_view: must recurse into pre_data@[0]; "
            f"recorded:\n  {joined}"
        )
    if "pre_data.len()" in joined or "pre_data[0]" in joined:
        failures.append(
            "Vec<u8> w/ spec_view: must NOT emit bare pre_data.len() or pre_data[0] "
            "(Verus rejects these on Vec in spec); "
            f"recorded:\n  {joined}"
        )
    # And the dual: a native Seq<u8> (spec_view=None) must keep bare form.
    bare_seq = TypeInfo(kind=TypeKind.SEQ, name="Seq<u8>", type_args=[u8_ty])
    ctx = _StubCtx(replies=[False, True])  # len: 0=F, 1=T
    narrow(bare_seq, "s", AssumeNode(key="s"), ctx)
    joined = " | ".join(expr for (_, expr) in ctx.recorded)
    if "s.len()" not in joined or "s@" in joined:
        failures.append(
            "Native Seq<u8>: must emit bare s.len(), NOT s@.len(); "
            f"recorded:\n  {joined}"
        )

    # ISSUES #14 — full memory-allocator::CommitMask::next_run shape
    # regression. The composite test combines STRUCT-with-array-field
    # for `self: CommitMask { mask: [usize; N] }`. The actual next_run
    # uses N=8, but length 8 lies in narrow's bisect phase and would
    # require ~20 scripted replies — to keep this self-test focused
    # we model `mask: [usize; 2]` (Phase-1 exact-probe range). If this
    # STRUCT-then-array path regresses, the next_run witness reverts
    # to the useless 2-assume "just r1 != r2" form (no self.mask state,
    # no per-position tuple probes).
    arr_usize_2 = TypeInfo(
        kind=TypeKind.SEQ, name="[usize; 2]",
        type_args=[TypeInfo(kind=TypeKind.USIZE, name="usize")],
    )
    commit_mask = TypeInfo(
        kind=TypeKind.STRUCT, name="CommitMask",
        fields=[FieldInfo(name="mask", type=arr_usize_2)],
    )
    # mask.len() probes: 0=F, 1=F, 2=T → commit length=2
    ctx = _StubCtx(replies=[False, False, True])
    narrow(commit_mask, "self_", AssumeNode(key="self_"), ctx)
    joined = " | ".join(expr for (_, expr) in ctx.recorded)
    for needle in ("self_.mask.len()", "self_.mask[0]", "self_.mask[1]"):
        if needle not in joined:
            failures.append(
                f"CommitMask regression: missing {needle!r}; recorded:\n  {joined}"
            )

    # Bug A — struct-form enum with field name shared across variants
    # must NOT emit per-field assumes for the ambiguous variants (Verus
    # cannot synthesize ``arrow_v`` when ``v`` is in 2+ variants). The
    # variant-tag assume itself is still emitted.
    from specdet.adapters.verus.native.extract.types import VariantInfo as _VI
    sr_inner = TypeInfo(kind=TypeKind.STRUCT, name="CMsg::SetRequest",
                        fields=[FieldInfo(name="v",
                                          type=TypeInfo(kind=TypeKind.U32,
                                                        name="u32"))])
    rep_inner = TypeInfo(kind=TypeKind.STRUCT, name="CMsg::Reply",
                         fields=[FieldInfo(name="v",
                                           type=TypeInfo(kind=TypeKind.U32,
                                                         name="u32"))])
    del_inner = TypeInfo(kind=TypeKind.STRUCT, name="CMsg::Delegate",
                         fields=[FieldInfo(name="h",
                                           type=TypeInfo(kind=TypeKind.U32,
                                                         name="u32"))])
    cmsg_ty = TypeInfo(
        kind=TypeKind.ENUM, name="CMsg",
        variants=[
            _VI("SetRequest", sr_inner, struct_form=True),
            _VI("Reply", rep_inner, struct_form=True),
            _VI("Delegate", del_inner, struct_form=True),
        ],
    )
    # SetRequest matches first → ambiguous, skip body.
    ctx = _StubCtx(replies=[True])
    narrow(cmsg_ty, "self_", AssumeNode(key="self_"), ctx)
    joined = " | ".join(expr for (_, expr) in ctx.recorded)
    if "self_->v" in joined:
        failures.append(
            "Bug A: ambiguous struct-form field ``v`` must not be "
            f"narrowed; recorded:\n  {joined}"
        )
    if "self_ is SetRequest" not in joined:
        failures.append(
            "Bug A: variant tag for ambiguous variant must still be "
            f"emitted; recorded:\n  {joined}"
        )
    # Delegate has only ``h`` (unambiguous) → body should narrow.
    ctx = _StubCtx(replies=[False, False, True])
    narrow(cmsg_ty, "self_", AssumeNode(key="self_"), ctx)
    joined = " | ".join(expr for (_, expr) in ctx.recorded)
    if "self_->h" not in joined:
        failures.append(
            "Bug A: unambiguous struct-form field ``h`` should still be "
            f"narrowed; recorded:\n  {joined}"
        )

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All narrow self-tests passed.")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        raise SystemExit(_run_self_tests())
    print("usage: python -m specdet.adapters.verus.native.extract.narrow test")
    raise SystemExit(2)
