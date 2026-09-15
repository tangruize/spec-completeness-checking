"""Schema enumeration, guarded template rendering, and assume translation.

A "schema" is an independent narrowing step pre-declared as a
(guard: bool, k: int/...) parameter pair on the Verus proof fn. The
guard activates the assume; the k-param (if any) makes the schema
polymorphic in its concrete value so the SAME schema covers all
bisection rounds.

Phase 2 scope
-------------
Schemas are generated **recursively** through the symbol tree:

 - Top-level integer / bool / variant / equal-fn (as in v1)
 - Nested Result/Option/Enum inner types (`r1->Ok_0`, `r1->Err_0`, ...)
 - Struct fields (`r1->Err_0.code`, `r1->Err_0.reason`, `r1->Ok_0@.num_bits`)
 - String literal equality (`reason == "string 1"` etc.)

Each nested schema carries a `parent_chain`: the sequence of variant
assertions that must hold for its assume expression to be well-typed
in Verus (e.g., ``r1 is Err`` must be asserted before accessing
``r1->Err_0.code``).  The generated ``if g_NAME { ... }`` block
re-asserts the parent chain first; these assumes are idempotent, so
repeatedly doing so in the same proof fn is safe.

Full set-literal equality and unsupported compound projections are not
enumerated. Missing translations are reported as unsupported, never as a
proof pass. Reusing one schema with conflicting parameter values is also
unsupported rather than an artificial UNSAT slice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from specdet.adapters.verus.native.extract.types import (
    DetCheckSpec, TypeInfo, TypeKind, VariantInfo, Assume, ProjectionInfo,
)


# ---------------------------------------------------------------------------

class SchemaKind(Enum):
    SCALAR_EQ = "scalar_eq"
    SCALAR_RANGE = "scalar_range"
    VARIANT_IS = "variant_is"
    ENUM_DISC_EQ = "enum_disc_eq"        # C-like enum: `var as int == k`
    BOOL_EQ = "bool_eq"
    STR_EQ = "str_eq"
    SET_EMPTY = "set_empty"
    SET_LEN_GT = "set_len_gt"           # set.len() > 0
    SET_LEN_EQ = "set_len_eq"           # set.len() == k
    SET_LEN_RANGE = "set_len_range"     # set.len() in [k_lo, k_hi]
    SET_CONTAINS = "set_contains"       # set.contains(k)
    SEQ_LEN_EQ = "seq_len_eq"
    SEQ_LEN_RANGE = "seq_len_range"
    NOT_EQUAL_FN = "not_equal_fn"


@dataclass
class SchemaBinding:
    """One guarded schema in the generated template."""
    id: str
    kind: SchemaKind
    rust_var: str                               # full dotted/arrow path
    rust_expr_tmpl: str                         # assume body with {k}/{k_lo}/{k_hi} placeholders
    guard_name: str
    k_params: list[tuple[str, str]] = field(default_factory=list)
    variant: Optional[str] = None               # VARIANT_IS
    bool_value: Optional[bool] = None           # BOOL_EQ
    str_value: Optional[str] = None             # STR_EQ (literal w/out quotes)
    # Chain of (outer_rust_var, variant_name) asserts required first so
    # the assume body is well-typed (e.g., [("r1", "Err")] for r1->Err_0.*).
    parent_chain: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

_INT_KINDS = {
    TypeKind.INT, TypeKind.USIZE, TypeKind.ISIZE,
    TypeKind.U8, TypeKind.U16, TypeKind.U32, TypeKind.U64,
    TypeKind.I8, TypeKind.I16, TypeKind.I32, TypeKind.I64,
}

_STR_LITERALS = ["", "string 1", "string 2"]

# Sentinel "variant name" in `parent_chain` entries that aren't variant
# guards but bool preconditions (e.g. `(pt).is_init() == true` gating
# the `(pt).value()` schemas — ISSUES #9). `_render_body` recognises
# this sentinel and emits `assume({outer});` instead of the variant
# form `assume({outer} is {vname});`. The string is intentionally
# distinct from any plausible Rust identifier so it can't collide
# with a real variant name.
_BOOL_TRUE_GUARD = "__bool_true__"

# Hard cap on nested SEQ/MAP element pre-enumeration. The SEQ/MAP branches
# pre-emit element/value schemas for 8 / 17 indices respectively so that
# `narrow_seq` / `narrow_map`'s `{var}[i]` recursion has a schema to hit.
# Nested containers (Seq<Seq<…>>, Ghost<Seq<Seq<…>>>, Map<K, Map<K2, V>>, …)
# previously produced a cartesian product of these expansions (8^k or 17^k),
# which overflowed Verus's stack while compiling the synthesized det fn.
# Pre-enumeration is now gated on `container_depth == 0`; deeper containers
# emit only their structural-summary schemas (len / dom-empty / len-eq /
# contains), enough for length-only narrowing without the explosion. The
# wrapper kinds (GHOST/TRACKED/POINTS_TO) and STRUCT/VARIANT recursion
# preserve container_depth — they don't add a container layer.
_MAX_NESTED_CONTAINER_DEPTH = 0


def _is_pcell_points_to_type(ty: TypeInfo) -> bool:
    name = (ty.name or "").replace(" ", "")
    return "pcell::PointsTo<" in name or "cell::pcell::PointsTo<" in name


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _emit(
    var: str,
    ty: TypeInfo,
    parent_chain: list[tuple[str, str]],
    out: list[SchemaBinding],
    seen_tags: set[str],
    projections_by_type: Optional[dict[str, list[ProjectionInfo]]] = None,
    container_depth: int = 0,
) -> None:
    """Emit all schemas for (var, ty), recursing through the type tree.

    ``container_depth`` is the number of enclosing SEQ/MAP layers we
    descended through via element/value recursion. Used by the SEQ/MAP
    branches to cap per-element pre-enumeration, preventing cartesian
    blow-up on nested containers (e.g. ``Ghost<Seq<Seq<Seq<…>>>>``).
    """
    tag_base = _sanitize(var)

    def _uniq(tag: str) -> str:
        # Guarantee uniqueness across the whole template.
        base = tag
        i = 1
        while tag in seen_tags:
            i += 1
            tag = f"{base}_{i}"
        seen_tags.add(tag)
        return tag

    projections_by_type = projections_by_type or {}

    # --- Integers ---
    if ty.kind in _INT_KINDS:
        needs_cast = ty.kind != TypeKind.INT
        lhs = f"{var} as int" if needs_cast else var
        sid = _uniq(f"{tag_base}_eq")
        out.append(SchemaBinding(
            id=sid, kind=SchemaKind.SCALAR_EQ, rust_var=var,
            rust_expr_tmpl=f"{lhs} == {{k}}",
            guard_name=f"g_{sid}",
            k_params=[(f"k_{sid}", "int")],
            parent_chain=list(parent_chain),
        ))
        sid = _uniq(f"{tag_base}_rng")
        out.append(SchemaBinding(
            id=sid, kind=SchemaKind.SCALAR_RANGE, rust_var=var,
            rust_expr_tmpl=f"{lhs} >= {{k_lo}} && {lhs} <= {{k_hi}}",
            guard_name=f"g_{sid}",
            k_params=[(f"k_{sid}_lo", "int"), (f"k_{sid}_hi", "int")],
            parent_chain=list(parent_chain),
        ))
        return

    # --- Bool ---
    if ty.kind == TypeKind.BOOL:
        for v in (True, False):
            lit = "true" if v else "false"
            sid = _uniq(f"{tag_base}_is_{lit}")
            out.append(SchemaBinding(
                id=sid, kind=SchemaKind.BOOL_EQ, rust_var=var,
                rust_expr_tmpl=f"{var} == {lit}",
                guard_name=f"g_{sid}", bool_value=v,
                parent_chain=list(parent_chain),
            ))
        return

    # --- Str ---
    if ty.kind == TypeKind.STR:
        # Verus types: `String` (exec), `StrSlice` / `&str` (spec). String
        # literals like `""` have type `StrSlice` in Verus mode. Direct
        # `String == "literal"` is rejected (incompatible types); the
        # cross-form comparison must go through ``.view()``, which is
        # defined on all three (`String::view() -> Seq<char>`,
        # `StrSlice::view() -> Seq<char>`). Always emit the viewed form so
        # the assume body type-checks regardless of whether ``var`` is
        # String or StrSlice (and works in both exec/spec contexts).
        for s in _STR_LITERALS:
            lit_tag = "empty" if s == "" else _sanitize(s)
            sid = _uniq(f"{tag_base}_eq_{lit_tag}")
            out.append(SchemaBinding(
                id=sid, kind=SchemaKind.STR_EQ, rust_var=var,
                rust_expr_tmpl=f'{var}@ == "{s}"@',
                guard_name=f"g_{sid}", str_value=s,
                parent_chain=list(parent_chain),
            ))
        return

    # --- Result / Option / generic Enum (sum types) ---
    if ty.kind in (TypeKind.RESULT, TypeKind.OPTION, TypeKind.ENUM):
        if ty.kind == TypeKind.RESULT:
            # variants + their inner types come from type_args, not ty.variants
            variant_items: list[tuple[str, Optional[TypeInfo]]] = [
                ("Ok", ty.type_args[0] if ty.type_args else None),
                ("Err", ty.type_args[1] if len(ty.type_args) > 1 else None),
            ]
        elif ty.kind == TypeKind.OPTION:
            variant_items = [
                ("Some", ty.type_args[0] if ty.type_args else None),
                ("None", None),
            ]
        else:  # ENUM — may have no inner
            variant_items = [(v.name, v.inner, v.struct_form)
                             for v in (ty.variants or [])]
        # Struct-form field names that occur in 2+ variants — Verus
        # cannot emit ``arrow_f`` for them. Skip the per-field schema
        # so the assume block does not reference uncompilable accessors.
        ambiguous_struct_fields: set[str] = (
            ty.ambiguous_struct_variant_fields()
            if ty.kind == TypeKind.ENUM else set()
        )

        for item in variant_items:
            # variant_items entries are 2-tuples for Result/Option, 3-tuples
            # for ENUM (including struct_form flag); normalise to 3-tuple.
            if len(item) == 2:
                vname, inner_ty = item
                struct_form_v = False
            else:
                vname, inner_ty, struct_form_v = item
            sid = _uniq(f"{tag_base}_is_{vname}")
            out.append(SchemaBinding(
                id=sid, kind=SchemaKind.VARIANT_IS, rust_var=var,
                rust_expr_tmpl=f"{var} is {vname}",
                guard_name=f"g_{sid}", variant=vname,
                parent_chain=list(parent_chain),
            ))
            if inner_ty is not None:
                if struct_form_v and inner_ty.fields:
                    # Skip body schemas when any field name is shared with
                    # another variant (no ``arrow_f`` accessor exists).
                    if any(fld.name in ambiguous_struct_fields
                           for fld in inner_ty.fields):
                        continue
                    # Struct-form variant: each named field is its own
                    # narrow target, accessed directly via ``var->fname``.
                    child_chain = parent_chain + [(var, vname)]
                    for fld in inner_ty.fields:
                        _emit(
                            f"{var}->{fld.name}", fld.type, child_chain,
                            out, seen_tags, projections_by_type,
                            container_depth=container_depth,
                        )
                else:
                    inner_var = f"{var}->{vname}_0"
                    child_chain = parent_chain + [(var, vname)]
                    _emit(inner_var, inner_ty, child_chain, out, seen_tags,
                          projections_by_type, container_depth=container_depth)

        # C-like enums (all unit variants with explicit discriminants, e.g.
        # `enum SlabSize { Slab8 = 8, ... }`) get an additional SCALAR_EQ
        # schema on `{var} as int == k`. Narrowing via this schema produces
        # witnesses framed in the integer representation the spec actually
        # uses (`slab_size as usize == 8`), which is more useful to humans
        # than `var is Slab8`.
        if ty.kind == TypeKind.ENUM and ty.is_c_like_enum():
            sid = _uniq(f"{tag_base}_disc_eq")
            out.append(SchemaBinding(
                id=sid, kind=SchemaKind.ENUM_DISC_EQ, rust_var=var,
                rust_expr_tmpl=f"{var} as int == {{k}}",
                guard_name=f"g_{sid}",
                k_params=[(f"k_{sid}", "int")],
                parent_chain=list(parent_chain),
            ))
        return

    # --- Struct ---
    if ty.kind == TypeKind.STRUCT:
        if ty.is_opaque and ty.spec_view is None:
            return
        if ty.spec_view is not None:
            # If equality is defined by the type's view (`var@`), do not
            # recursively enumerate the viewed struct's internals by default.
            # Large view models (e.g. Kubernetes object specs) can explode into
            # thousands of guards and even overflow Verus before the actual
            # determinism check runs. Container kinds with spec_view (Vec, etc.)
            # are still handled in their own branches below.
            return
        for fld in ty.fields:
            _emit(f"{var}.{fld.name}", fld.type, parent_chain, out,
                  seen_tags, projections_by_type,
                  container_depth=container_depth)
        return

    # --- Set ---
    if ty.kind == TypeKind.SET:
        elem_ty = ty.type_args[0] if ty.type_args else TypeInfo(kind=TypeKind.INT, name="int")
        elem_ty_name = elem_ty.name or "int"

        sid = _uniq(f"{tag_base}_empty")
        out.append(SchemaBinding(
            id=sid, kind=SchemaKind.SET_EMPTY, rust_var=var,
            rust_expr_tmpl=f"{var} == Set::<{elem_ty_name}>::empty()",
            guard_name=f"g_{sid}", parent_chain=list(parent_chain),
        ))

        sid = _uniq(f"{tag_base}_lengt")
        out.append(SchemaBinding(
            id=sid, kind=SchemaKind.SET_LEN_GT, rust_var=var,
            rust_expr_tmpl=f"{var}.len() > 0",
            guard_name=f"g_{sid}", parent_chain=list(parent_chain),
        ))

        sid = _uniq(f"{tag_base}_leneq")
        out.append(SchemaBinding(
            id=sid, kind=SchemaKind.SET_LEN_EQ, rust_var=var,
            rust_expr_tmpl=f"{var}.len() == {{k}}",
            guard_name=f"g_{sid}",
            k_params=[(f"k_{sid}", "nat")],
            parent_chain=list(parent_chain),
        ))

        sid = _uniq(f"{tag_base}_lenrng")
        out.append(SchemaBinding(
            id=sid, kind=SchemaKind.SET_LEN_RANGE, rust_var=var,
            rust_expr_tmpl=f"{var}.len() >= {{k_lo}} && {var}.len() <= {{k_hi}}",
            guard_name=f"g_{sid}",
            k_params=[(f"k_{sid}_lo", "nat"), (f"k_{sid}_hi", "nat")],
            parent_chain=list(parent_chain),
        ))

        sid = _uniq(f"{tag_base}_contains")
        out.append(SchemaBinding(
            id=sid, kind=SchemaKind.SET_CONTAINS, rust_var=var,
            rust_expr_tmpl=f"{var}.contains({{k}})",
            guard_name=f"g_{sid}",
            k_params=[(f"k_{sid}", elem_ty_name)],
            parent_chain=list(parent_chain),
        ))
        return

    # --- Map ---
    # Map<K,V> reuses Set schemas on m.dom() and value-at-key schemas on m[i].
    # No new SchemaKind needed — narrow_map emits SetEmptyPred/SetContainsPred
    # on the virtual .dom() var and EqPred/... on m[k] slots.
    if ty.kind == TypeKind.MAP:
        k_ty = ty.type_args[0] if ty.type_args else TypeInfo(kind=TypeKind.INT, name="int")
        v_ty = ty.type_args[1] if len(ty.type_args) > 1 else TypeInfo(kind=TypeKind.INT, name="int")

        # Dom as Set<K>: emits SET_EMPTY/SET_LEN_GT/SET_LEN_EQ/SET_LEN_RANGE/SET_CONTAINS.
        dom_var = f"{var}.dom()"
        dom_set_ty = TypeInfo(kind=TypeKind.SET, name=f"Set<{k_ty.name}>", type_args=[k_ty])
        _emit(dom_var, dom_set_ty, parent_chain, out, seen_tags,
              projections_by_type, container_depth=container_depth)

        # Value slots: pre-enumerate `{var}[{i}]` for small integer keys so
        # recursive narrow on V has schemas to hit. For non-integer key types
        # we currently skip — narrow_map will still assert `.dom().contains(k)`
        # but can't pin down values (graceful degradation, like Seq > MAX_SEQ_LEN).
        # Cap nesting: only enumerate values at the outermost container layer
        # (see _MAX_NESTED_CONTAINER_DEPTH above). Deeper Maps emit only
        # dom-summary schemas.
        if (k_ty.kind in _INT_KINDS
                and container_depth <= _MAX_NESTED_CONTAINER_DEPTH):
            # Mirror narrow's _SMALL_UNSIGNED/_SMALL_SIGNED ranges so every
            # key the set-element probing may find has a matching value schema.
            unsigned = k_ty.kind in {
                TypeKind.U8, TypeKind.U16, TypeKind.U32, TypeKind.U64, TypeKind.USIZE,
            } or (k_ty.name or "").strip() == "nat"
            key_range = range(0, 17) if unsigned else range(-8, 9)
            for i in key_range:
                _emit(f"{var}[{i}]", v_ty, parent_chain, out, seen_tags,
                      projections_by_type,
                      container_depth=container_depth + 1)
        return

    # --- Seq ---
    if ty.kind == TypeKind.SEQ:
        # ISSUES #14 — Vec<T> is tagged with spec_view=Seq<T> by the
        # extractor to mark "Verus needs `@` to project". Mirror
        # narrow_seq: emit schemas against `{var}@` rather than `{var}`
        # when spec_view is set; native Seq / array / slice keep the
        # bare accessor.
        accessor = f"{var}@" if ty.spec_view else var
        # ISSUES #14 follow-up: when ty.spec_view is set (Vec<T>),
        # narrow_seq emits the LenEqPred / LenRangePred with var=accessor
        # (the path-with-@). The schema's rust_var must MATCH that
        # exactly for match_and_bind to succeed — otherwise every length
        # probe is unsupported and cannot refine the candidate.
        # SCALAR_EQ schemas for elements already use
        # the with-@ path (see _emit recursion below), so they're fine.
        sid = _uniq(f"{tag_base}_leneq")
        out.append(SchemaBinding(
            id=sid, kind=SchemaKind.SEQ_LEN_EQ, rust_var=accessor,
            rust_expr_tmpl=f"{accessor}.len() == {{k}}",
            guard_name=f"g_{sid}",
            k_params=[(f"k_{sid}", "nat")],
            parent_chain=list(parent_chain),
        ))
        sid = _uniq(f"{tag_base}_lenrng")
        out.append(SchemaBinding(
            id=sid, kind=SchemaKind.SEQ_LEN_RANGE, rust_var=accessor,
            rust_expr_tmpl=f"{accessor}.len() >= {{k_lo}} && {accessor}.len() <= {{k_hi}}",
            guard_name=f"g_{sid}",
            k_params=[(f"k_{sid}_lo", "nat"), (f"k_{sid}_hi", "nat")],
            parent_chain=list(parent_chain),
        ))
        # Pre-enumerate element schemas for the first MAX_SEQ_LEN indices so
        # narrow_seq's `{accessor}[i]` recursion has a schema to hit. Cap
        # nesting at the outermost container layer (see
        # _MAX_NESTED_CONTAINER_DEPTH); nested Seqs emit only len schemas,
        # preventing 8^k cartesian blowup on types like
        # `Ghost<Seq<Seq<Seq<…>>>>`.
        if ty.type_args and container_depth <= _MAX_NESTED_CONTAINER_DEPTH:
            MAX_SEQ_LEN = 8
            elem_ty = ty.type_args[0]
            for i in range(MAX_SEQ_LEN):
                _emit(f"{accessor}[{i}]", elem_ty, parent_chain, out, seen_tags,
                      projections_by_type,
                      container_depth=container_depth + 1)
        return

    # PR-F: Tracked<T> / Ghost<T> — emit schemas for the projected value
    # `(var)@` as if it were the inner T. narrow.narrow_tracked_or_ghost
    # uses this same projection, so the assumes it emits match the
    # schemas enumerated here. Wrappers preserve container_depth — they
    # don't introduce a new container layer themselves.
    if ty.kind in (TypeKind.TRACKED, TypeKind.GHOST):
        if ty.type_args:
            inner_ty = ty.type_args[0]
            _emit(f"({var})@", inner_ty, parent_chain, out, seen_tags,
                  projections_by_type, container_depth=container_depth)
        return

    # PR-F: PointsTo<V> — emit schemas for `(var).is_init()` (bool),
    # `(var).value()` (V, only meaningful when is_init), `(var).ptr().addr()`
    # (usize). Match the projection expressions narrow_points_to emits.
    # Like Ghost/Tracked, PointsTo is a wrapper — preserve container_depth.
    #
    # ISSUES #9: `pt.value()` is only semantically meaningful when
    # `pt.is_init() == true`. Tie inner V schemas to that precondition
    # via a `_BOOL_TRUE_GUARD` parent_chain entry. Activating any
    # value()-derived guard then forces `(pt).is_init()` to be assumed
    # alongside, so the search will not pick value() facts in
    # is_init=false branches. ptr().addr() carries no such precondition.
    if ty.kind == TypeKind.POINTS_TO:
        if _is_pcell_points_to_type(ty):
            return
        init_var = f"({var}).is_init()"
        _emit(init_var,
              TypeInfo(kind=TypeKind.BOOL, name="bool"),
              parent_chain, out, seen_tags, projections_by_type,
              container_depth=container_depth)
        if ty.type_args:
            value_chain = list(parent_chain) + [(init_var, _BOOL_TRUE_GUARD)]
            _emit(f"({var}).value()", ty.type_args[0],
                  value_chain, out, seen_tags, projections_by_type,
                  container_depth=container_depth)
        _emit(f"({var}).ptr().addr()",
              TypeInfo(kind=TypeKind.USIZE, name="usize"),
              parent_chain, out, seen_tags, projections_by_type,
              container_depth=container_depth)
        return

    # Other kinds (Unit/Unknown) — skipped, except UNKNOWN with registered
    # projections, which we route through the projection's spec-fn call.
    if ty.kind == TypeKind.UNKNOWN and ty.name in projections_by_type:
        for proj in projections_by_type[ty.name]:
            proj_var = proj.call_expr(var)
            # Emit schemas for the projection return value. We reuse the
            # same parent_chain — projections are total over the opaque
            # type (no variant guard needed to call them).
            _emit(proj_var, proj.return_type, parent_chain, out, seen_tags,
                  projections_by_type, container_depth=container_depth)
        return

    return


def enumerate_schemas(det_spec: DetCheckSpec) -> list[SchemaBinding]:
    """Walk the symbol table and emit all schemas (recursive)."""
    schemas: list[SchemaBinding] = []
    seen_tags: set[str] = set()
    seen_vars: set[str] = set()
    # Flatten det_spec.type_projections into a name->list[ProjectionInfo]
    # dict keyed by TypeInfo.name, dropping "empty" entries (they have no
    # projections to offer). Missing entries mean "not attempted" — the
    # schema layer treats them identically to "empty" for schema emission.
    projections_by_type: dict[str, list[ProjectionInfo]] = {
        name: tp.projections
        for name, tp in det_spec.type_projections.items()
        if tp.projections
    }
    for sym in det_spec.symbols:
        if sym.name in seen_vars:
            continue
        seen_vars.add(sym.name)
        _emit(sym.name, sym.type, [], schemas, seen_tags, projections_by_type)

    schemas.append(SchemaBinding(
        id="neq_tuple",
        kind=SchemaKind.NOT_EQUAL_FN,
        rust_var="__tuple__",
        rust_expr_tmpl="!{equal_fn_call}",
        guard_name="g_neq_tuple",
    ))
    return schemas


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

def render_schema_expression(
    schema: SchemaBinding, equal_fn_call: str = "", *, bindings: Optional[dict[str, int]] = None,
) -> str:
    """Use the same placeholder mapping for compilation and witness rendering."""
    names = [name for name, _ in schema.k_params]
    if bindings is not None:
        if set(bindings) != set(names) or any(type(value) is not int for value in bindings.values()):
            raise ValueError("Schema values must bind exactly its compiled integer parameters")
        values = [str(bindings[name]) for name in names]
    else:
        values = names
    body = schema.rust_expr_tmpl
    if schema.kind == SchemaKind.NOT_EQUAL_FN:
        if not equal_fn_call:
            raise ValueError("Distinctness schema requires its frozen equality call")
        body = body.replace("{equal_fn_call}", equal_fn_call)
    for marker, index in (("{k}", 0), ("{k_lo}", 0), ("{k_hi}", 1)):
        if marker in body:
            if len(values) <= index:
                raise ValueError(f"Schema {schema.id} has no parameter for {marker}")
            body = body.replace(marker, values[index])
    for name, value in zip(names, values):
        body = body.replace("{" + name + "}", value)
    return body


def _render_body(schemas: list[SchemaBinding], equal_fn_call: str) -> str:
    lines: list[str] = []
    for s in schemas:
        body = render_schema_expression(s, equal_fn_call)

        # Re-assert parent chain so inner arrow projections are well-typed.
        # Bool-precondition entries (sentinel vname = _BOOL_TRUE_GUARD)
        # render as a plain `assume(expr);` — used to tie `(pt).value()`
        # schemas to `(pt).is_init() == true` (ISSUES #9).
        chain_asserts = "".join(
            f" assume({outer});" if vname == _BOOL_TRUE_GUARD
            else f" assume({outer} is {vname});"
            for (outer, vname) in s.parent_chain
        )
        lines.append(f"    if {s.guard_name} {{{chain_asserts} assume({body}); }}")
    return "\n".join(lines)


def _inject_extra_params(tmpl: str, extra_str: str) -> str:
    """Insert ``extra_str`` at the start of the proof fn's parameter list.

    Walks the template manually so generics (``proof fn det_x<T: Foo<U>>(...)``)
    don't trip up the matcher — we balance angle brackets to find the real
    opening ``(`` of the params list.
    """
    if not extra_str:
        return tmpl
    m = re.search(r"\bproof\s+fn\s+\w+", tmpl)
    if not m:
        return tmpl
    pos = m.end()
    while pos < len(tmpl) and tmpl[pos].isspace():
        pos += 1
    if pos < len(tmpl) and tmpl[pos] == "<":
        depth = 0
        while pos < len(tmpl):
            c = tmpl[pos]
            if c == "<":
                depth += 1
            elif c == ">" and not (pos > 0 and tmpl[pos - 1] == "-"):
                depth -= 1
                if depth == 0:
                    pos += 1
                    break
            pos += 1
    while pos < len(tmpl) and tmpl[pos].isspace():
        pos += 1
    if pos >= len(tmpl) or tmpl[pos] != "(":
        return tmpl
    after = pos + 1
    while after < len(tmpl) and tmpl[after].isspace():
        after += 1
    sep = "" if (after < len(tmpl) and tmpl[after] == ")") else ", "
    return tmpl[:pos + 1] + extra_str + sep + tmpl[pos + 1:]


def _split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for idx, ch in enumerate(text):
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append(text[start:idx].strip())
            start = idx + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def render_guarded_template(
    det_spec: DetCheckSpec,
    schemas: list[SchemaBinding],
    *,
    proof_prelude: Optional[str] = None,
) -> str:
    """Render the synthetic ``proof fn det_<f>`` source.

    ``proof_prelude`` is retained for call compatibility, but nonempty
    proof blocks are unsupported here: an arbitrary failed assertion is
    not a counterexample to the fixed global goal. Proof proposals belong
    in a separately generated and explicitly checked proof artifact.
    """
    if proof_prelude and proof_prelude.strip():
        raise ValueError(
            "Schema search uses a fixed global goal; check proof blocks in a separate artifact"
        )
    extra_params: list[str] = []
    for s in schemas:
        extra_params.append(f"{s.guard_name}: bool")
        for (pname, pty) in s.k_params:
            extra_params.append(f"{pname}: {pty}")
    extra_str = ", ".join(extra_params)

    def equal_fn_turbofish() -> str:
        if not det_spec.equal_fn_def:
            return ""
        m = re.search(
            rf"\bspec\s+fn\s+{re.escape(det_spec.equal_fn_name)}\s*(<[^(\n]+>)?",
            det_spec.equal_fn_def,
        )
        if not m or not m.group(1):
            return ""
        args: list[str] = []
        inner = m.group(1).strip()[1:-1]
        for part in _split_top_level_commas(inner):
            part = part.strip()
            if not part:
                continue
            if part.startswith("'"):
                continue
            if part.startswith("const "):
                ident = re.match(r"const\s+([A-Za-z_][A-Za-z0-9_]*)", part)
                if ident is not None:
                    args.append(ident.group(1))
                continue
            ident = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", part)
            if ident is not None:
                args.append(ident.group(1))
        return f"::<{', '.join(args)}>" if args else ""

    eq_args: list[str] = []
    for pair in det_spec.equal_arg_pairs:
        eq_args.extend([pair["lhs"], pair["rhs"]])
    equal_call = f"{det_spec.equal_fn_name}{equal_fn_turbofish()}({', '.join(eq_args)})"

    body = _render_body(schemas, equal_call)
    tmpl = det_spec.det_check_template
    new_tmpl = _inject_extra_params(tmpl, extra_str)
    return new_tmpl.replace("{ASSUMES}", body + "\n")


# ---------------------------------------------------------------------------
# Translation: structured AssumePred -> schema activation
# ---------------------------------------------------------------------------
#
# Generic dispatch: each pred class knows how to match itself against a
# SchemaBinding (see predicates.py :: match_and_bind).  Adding a new pred
# kind therefore requires ZERO edits to this file.


def translate_assume(
    assume: Assume,
    schemas: list[SchemaBinding],
    equal_fn_name: str = "",
) -> Optional[tuple[str, dict[str, int]]]:
    """Match ``assume.pred`` to the first applicable schema.

    Returns ``(schema_id, k_bindings)`` if a schema matches; ``None``
    otherwise (an unsupported candidate, not a solver verdict).
    """
    pred = assume.pred
    if pred is None:
        return None
    if (equal_fn_name and hasattr(pred, "call")
            and not re.match(rf"^{re.escape(equal_fn_name)}\s*(?:::<|\()", pred.call)):
        return None
    for s in schemas:
        kb = pred.match_and_bind(s)
        if kb is not None:
            return (s.id, kb)
    return None
