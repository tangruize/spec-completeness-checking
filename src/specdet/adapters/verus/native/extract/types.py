"""
spec-determinism: Data types shared across modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .predicates import AssumePred


class TypeKind(Enum):
    INT = "int"
    USIZE = "usize"
    ISIZE = "isize"
    U8 = "u8"
    U16 = "u16"
    U32 = "u32"
    U64 = "u64"
    I8 = "i8"
    I16 = "i16"
    I32 = "i32"
    I64 = "i64"
    BOOL = "bool"
    STR = "str"
    UNIT = "()"
    ENUM = "enum"
    STRUCT = "struct"
    SET = "Set"
    SEQ = "Seq"
    MAP = "Map"
    RESULT = "Result"
    OPTION = "Option"
    # PR-F (A-1) — vstd ghost/proof wrappers. Carry their inner type as
    # type_args[0]; they project via `var@` (Tracked/Ghost) or via
    # `var.value()` / `var.is_init()` / `var.addr()` (PointsTo).
    TRACKED = "Tracked"
    GHOST = "Ghost"
    POINTS_TO = "PointsTo"
    UNKNOWN = "unknown"


@dataclass
class TypeInfo:
    kind: TypeKind
    name: str                          # e.g. "Bitmap", "usize", "Result<usize, Error>"
    fields: list["FieldInfo"] = field(default_factory=list)      # for struct
    variants: list["VariantInfo"] = field(default_factory=list)  # for enum
    type_args: list["TypeInfo"] = field(default_factory=list)    # for generics
    spec_view: Optional["TypeInfo"] = None  # the type returned by @/@view
    # ``#[verifier(external_body)]`` / ``#[verifier::external_body]`` struct.
    # Verus treats the type's body as abstract: field expressions are disallowed
    # in spec contexts ("disallowed: field expression for an opaque datatype"),
    # but ``==`` on the whole value is still permitted (and chains via
    # transitivity through any ensures clause that pins both runs' results
    # to the same spec function call, e.g. ``r1 == spec_from_vec(v)``).
    is_opaque: bool = False
    # ``#[verifier::ext_equal]`` / ``#[verifier(ext_equal)]`` struct or enum.
    # Per the Verus extensional_equality guide, this opts the type into
    # ``=~=`` extensional equality: ``a =~= b`` drills recursively through
    # Seq / Set / Map / spec_fn fields and any nested ext_equal-annotated
    # types, while still collapsing to ``==`` on plain primitive / struct
    # fields. When codegen sees this flag, the STRUCT/ENUM branches in
    # ``build_equal_expr`` can short-circuit to ``lhs =~= rhs`` instead of
    # recursively expanding every field, dramatically shrinking the
    # generated equal_fn body without weakening its semantics.
    is_ext_equal: bool = False

    def to_dict(self) -> dict:
        d = {"kind": self.kind.value, "name": self.name}
        if self.fields:
            d["fields"] = [f.to_dict() for f in self.fields]
        if self.variants:
            d["variants"] = [v.to_dict() for v in self.variants]
        if self.type_args:
            d["type_args"] = [t.to_dict() for t in self.type_args]
        if self.spec_view:
            d["spec_view"] = self.spec_view.to_dict()
        if self.is_opaque:
            d["is_opaque"] = True
        if self.is_ext_equal:
            d["is_ext_equal"] = True
        return d

    @staticmethod
    def from_dict(d: dict) -> "TypeInfo":
        return TypeInfo(
            kind=TypeKind(d["kind"]),
            name=d["name"],
            fields=[FieldInfo.from_dict(f) for f in d.get("fields", [])],
            variants=[VariantInfo.from_dict(v) for v in d.get("variants", [])],
            type_args=[TypeInfo.from_dict(t) for t in d.get("type_args", [])],
            spec_view=TypeInfo.from_dict(d["spec_view"]) if d.get("spec_view") else None,
            is_opaque=bool(d.get("is_opaque", False)),
            is_ext_equal=bool(d.get("is_ext_equal", False)),
        )

    def is_c_like_enum(self) -> bool:
        """True iff this is a unit-only enum where every variant has an
        explicit integer discriminant (e.g. ``enum SlabSize { Slab8 = 8, ... }``).

        Witnesses for such enums should be emitted as integer equalities on
        ``x as int`` rather than as ``x is Variant``, because the spec-level
        reasoning the ensures clauses use is the discriminant value.
        """
        if self.kind != TypeKind.ENUM or not self.variants:
            return False
        return all(
            v.inner is None and v.discriminant is not None
            for v in self.variants
        )

    def ambiguous_struct_variant_fields(self) -> set[str]:
        """Names of struct-form variant fields that occur in more than one
        variant of this enum.

        Verus only auto-generates an ``arrow_<f>`` accessor when the field
        name ``f`` is unique across all variants of the enum. When two or
        more variants share a struct-form field name, ``self_->f`` errors
        with "method ``arrow_f`` not found". Callers that emit per-field
        accessors must fall back to a whole-variant comparison (or skip
        per-field narrowing) for those names.

        Empty set for non-enum types, tuple-form-only enums, and enums
        whose struct-form field names are all distinct.
        """
        if self.kind != TypeKind.ENUM or not self.variants:
            return set()
        seen: set[str] = set()
        ambiguous: set[str] = set()
        for v in self.variants:
            if v.struct_form and v.inner is not None and v.inner.fields:
                for fld in v.inner.fields:
                    if fld.name in seen:
                        ambiguous.add(fld.name)
                    else:
                        seen.add(fld.name)
        return ambiguous


@dataclass
class FieldInfo:
    name: str
    type: TypeInfo

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type.to_dict()}

    @staticmethod
    def from_dict(d: dict) -> "FieldInfo":
        return FieldInfo(name=d["name"], type=TypeInfo.from_dict(d["type"]))


@dataclass
class VariantInfo:
    name: str
    inner: Optional[TypeInfo] = None
    discriminant: Optional[int] = None  # explicit `= N` literal on unit variants
    # True for Rust struct-form variants ``V { f1: T1, f2: T2 }``. The
    # ``inner`` for these is a synthetic STRUCT whose fields are the
    # variant's named fields; codegen accesses them via ``r->fname``
    # (NOT ``r->V_0.fname`` like for tuple-form ``V(T1, T2)``).
    struct_form: bool = False

    def to_dict(self) -> dict:
        d: dict = {"name": self.name}
        if self.inner:
            d["inner"] = self.inner.to_dict()
        if self.discriminant is not None:
            d["discriminant"] = self.discriminant
        if self.struct_form:
            d["struct_form"] = True
        return d

    @staticmethod
    def from_dict(d: dict) -> "VariantInfo":
        return VariantInfo(
            name=d["name"],
            inner=TypeInfo.from_dict(d["inner"]) if d.get("inner") else None,
            discriminant=d.get("discriminant"),
            struct_form=bool(d.get("struct_form", False)),
        )


@dataclass
class ProjectionInfo:
    """One spec-fn projection of an opaque type, e.g. `spec_layout_size(layout) -> usize`.

    Only used to narrow variables whose type the extractor could not
    resolve structurally (``TypeKind.UNKNOWN``). The spec fn must be
    unary (one argument of the opaque type) and return a scalar kind.
    """
    spec_fn: str
    return_type: TypeInfo
    rationale: Optional[str] = None

    def call_expr(self, var: str) -> str:
        return f"{self.spec_fn}({var})"

    def to_dict(self) -> dict:
        d: dict = {"spec_fn": self.spec_fn,
                   "return_type": self.return_type.to_dict()}
        if self.rationale is not None:
            d["rationale"] = self.rationale
        return d

    @staticmethod
    def from_dict(d: dict) -> "ProjectionInfo":
        return ProjectionInfo(
            spec_fn=d["spec_fn"],
            return_type=TypeInfo.from_dict(d["return_type"]),
            rationale=d.get("rationale"),
        )


@dataclass
class TypeProjections:
    """Explicitly supplied projections for one opaque type."""
    status: str                              # "ok" | "empty"
    projections: list[ProjectionInfo] = field(default_factory=list)
    source: str = "manual"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "source": self.source,
            "projections": [p.to_dict() for p in self.projections],
        }

    @staticmethod
    def from_dict(d: dict) -> "TypeProjections":
        return TypeProjections(
            status=d.get("status", "empty"),
            source=d.get("source", "manual"),
            projections=[ProjectionInfo.from_dict(p)
                         for p in d.get("projections", [])],
        )


@dataclass
class Param:
    name: str
    type: TypeInfo
    is_mut_ref: bool = False
    is_ref: bool = False
    is_self: bool = False
    # Source param uses a ghost-carrier destructure pattern like
    # ``Ghost(name): Ghost<T>`` or ``Tracked(name): Tracked<T>``. In the
    # original function body, ``name`` has type ``T`` (the inner type), but
    # the param's outer type is still ``Ghost<T>`` / ``Tracked<T>``. The
    # extractor stores the inner identifier under ``name`` and the outer
    # wrapper type under ``type``; gen_det reads this flag to re-emit the
    # destructure pattern in the synthesized fn signature so the body
    # (substituted requires/ensures) sees ``name: T`` as in the source.
    destructure_ctor: Optional[str] = None  # "Ghost" | "Tracked" | None


@dataclass
class FunctionSpec:
    """Raw extracted spec — used internally by extract + gen_det."""
    name: str
    params: list[Param]
    return_type: TypeInfo
    requires: list[str]
    ensures: list[str]
    type_defs: dict[str, TypeInfo] = field(default_factory=dict)
    # The name Verus binds the return value to in ensures clauses. Comes
    # from either `(name: T)` on the signature or `#[verus_spec(name => ...)]`.
    # Defaults to "result" which matches Verus's implicit binding when the
    # user writes no explicit name (nanvix convention).
    result_binding: str = "result"

    # --- Generic / impl context (for fns inside generic impls) ----------------
    # When the source fn lives inside an `impl<...>` block (or has its own
    # `fn<...>` type parameters / where clause), gen_det needs to lift those
    # onto the synthesized det fn. These fields hold the *raw text* directly
    # from the AST (e.g. "<K: KeyTrait + VerusClone, V: Clone>" /
    # "where K: Ord") so we don't reinvent bound parsing. Empty string means
    # "no generics" / "no where clause"; ``self_type`` is the impl target
    # type as it appears in the source (e.g. "StrictlyOrderedVec<K>") and is
    # ``None`` for free fns.
    generics_decl: str = ""
    where_decl: str = ""
    self_type: Optional[str] = None
    # When the source fn lives inside a `pub trait Foo { ... }` declaration
    # (rather than an `impl ... for ...` block), ``self_type`` is None but
    # the synthesized standalone det fn must still typecheck. We record the
    # enclosing trait's name (including instantiated generic arguments) so gen_det can emit
    # ``<__DetSelf: <trait_name>>`` as the type-parameter bound; without
    # it, any reference to ``Self::method`` in the ensures triggers E0599.
    trait_name: Optional[str] = None

    def input_vars(self) -> list[Param]:
        return list(self.params)

    def output_vars(self) -> list[tuple[str, TypeInfo]]:
        outs = []
        for p in self.params:
            if p.is_mut_ref:
                base = "self_" if p.is_self else p.name
                outs.append((f"post_{base}", p.type))
        outs.append(("result", self.return_type))
        return outs


# ===========================================================================
# Symbol — a variable to be narrowed during binary search
# ===========================================================================

@dataclass
class Symbol:
    """One variable in the symbol table for binary search."""
    name: str           # e.g. "pre_self_@.num_bits", "r1", "post1_self_"
    type: TypeInfo      # type info for strategy dispatch
    phase: str          # "input" | "output_simple" | "output_compound"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type.to_dict(),
            "phase": self.phase,
        }

    @staticmethod
    def from_dict(d: dict) -> "Symbol":
        return Symbol(
            name=d["name"],
            type=TypeInfo.from_dict(d["type"]),
            phase=d["phase"],
        )


# ===========================================================================
# DetCheckSpec — output of Step 1, input to Step 2
# ===========================================================================

@dataclass
class DetCheckSpec:
    """
    Everything the search step needs. JSON-serializable.
    
    Produced by Step 1 (extract + gen_det).
    Consumed by Step 2 (binary search).
    """
    function: str                    # function name
    det_check_template: str          # Verus proof fn with {ASSUMES} placeholder
    symbols: list[Symbol]            # variables to narrow, in order
    verus_config: dict = field(default_factory=dict)  # crate_dir, crate_name, etc.
    # Structural-equality spec fn injected alongside the det check.
    equal_fn_def: str = ""            # Verus source of `spec fn {equal_fn_name}(...) -> bool`
    equal_fn_name: str = ""           # e.g. "set_equal"
    equal_arg_pairs: list[dict] = field(default_factory=list)  # [{"lhs":"r1","rhs":"r2"}, ...]
    check_fn_name: str = ""           # actual `proof fn <name>` emitted; default `det_{function}`
    equal_policy: dict = field(default_factory=dict)  # EqualPolicy.to_dict() — coarsening rules used
    # Projections of opaque (TypeKind.UNKNOWN) types discovered by the
    # LLM hook or configured manually. Keyed by the exact TypeInfo.name
    # string seen in the symbol table (e.g. "Layout").
    type_projections: dict[str, TypeProjections] = field(default_factory=dict)
    # Generic / impl context lifted from the source fn so rebuild_equal_fn
    # (post llm_refine) can keep emitting valid Rust signatures. Empty
    # strings / None mean "no generic context" (free fn, default).
    generics_decl: str = ""
    where_decl: str = ""
    self_type: Optional[str] = None
    # P0 opacity fix: names of ``closed spec fn`` declarations reachable
    # from the target's ensures that gen_det requested to be rewritten
    # to ``#[verifier::opaque] open spec fn``. The inject phase
    # (single_file._inject_into_source) consumes this list to apply the
    # source-level rewrite; the proof body already emits matching
    # ``reveal(<name>);`` statements. Empty when ``source`` was not
    # passed to ``build_det_check_spec``.
    opened_closed_specs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "function": self.function,
            "det_check_template": self.det_check_template,
            "symbols": [s.to_dict() for s in self.symbols],
            "verus_config": self.verus_config,
            "equal_fn_def": self.equal_fn_def,
            "equal_fn_name": self.equal_fn_name,
            "equal_arg_pairs": list(self.equal_arg_pairs),
            "check_fn_name": self.check_fn_name,
            "equal_policy": dict(self.equal_policy),
            "type_projections": {k: v.to_dict()
                                  for k, v in self.type_projections.items()},
            "generics_decl": self.generics_decl,
            "where_decl": self.where_decl,
            "self_type": self.self_type,
            "opened_closed_specs": list(self.opened_closed_specs),
        }

    @staticmethod
    def from_dict(d: dict) -> "DetCheckSpec":
        raw_projs = d.get("type_projections") or {}
        return DetCheckSpec(
            function=d["function"],
            det_check_template=d["det_check_template"],
            symbols=[Symbol.from_dict(s) for s in d["symbols"]],
            verus_config=d.get("verus_config", {}),
            equal_fn_def=d.get("equal_fn_def", ""),
            equal_fn_name=d.get("equal_fn_name", ""),
            equal_arg_pairs=list(d.get("equal_arg_pairs", [])),
            check_fn_name=d.get("check_fn_name", ""),
            equal_policy=dict(d.get("equal_policy") or {}),
            type_projections={k: TypeProjections.from_dict(v)
                              for k, v in raw_projs.items()},
            generics_decl=d.get("generics_decl", ""),
            where_decl=d.get("where_decl", ""),
            self_type=d.get("self_type"),
            opened_closed_specs=list(d.get("opened_closed_specs") or []),
        )

    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict(), indent=2)

    @staticmethod
    def from_json(text: str) -> "DetCheckSpec":
        import json
        return DetCheckSpec.from_dict(json.loads(text))


# ===========================================================================
# Search output types (unchanged)
# ===========================================================================

@dataclass
class Assume:
    """A single narrowing assume() emitted during search.

    The predicate is the single source of truth; the Rust expression is
    derived on demand via :attr:`expression`.  Constructors that want
    only a predicate should use :meth:`from_pred`; the raw tuple
    ``Assume(var, pred, description)`` works too.
    """
    var_name: str
    pred: "AssumePred"
    description: str = ""

    @property
    def expression(self) -> str:
        return self.pred.to_rust()

    @classmethod
    def from_pred(cls, var_name: str, pred: "AssumePred",
                  description: str = "") -> "Assume":
        return cls(var_name=var_name, pred=pred, description=description)

    def to_dict(self) -> dict:
        return {
            "var_name": self.var_name,
            "expression": self.expression,
            "description": self.description,
        }


@dataclass
class VerifyResult:
    status: str           # "pass", "fail", "timeout", "error"
    function: str
    duration_ms: int = 0
    stderr: str = ""


@dataclass
class ConcreteValue:
    var_name: str
    type_name: str
    fields: dict[str, str] = field(default_factory=dict)
    raw: str = ""


@dataclass
class Witness:
    function: str
    inputs: dict[str, ConcreteValue] = field(default_factory=dict)
    output1: dict[str, ConcreteValue] = field(default_factory=dict)
    output2: dict[str, ConcreteValue] = field(default_factory=dict)
    assumes: list[Assume] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    gap_type: str = ""
    gap_description: str = ""
    r0_z3: Optional[str] = ""
    candidate_assumes: list[Assume] = field(default_factory=list)
    search_exhausted: bool = False
    diagnostics: list[str] = field(default_factory=list)
    last_sat_round: Optional[int] = None
