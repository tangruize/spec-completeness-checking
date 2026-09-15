"""Structured predicates emitted by narrow strategies.

Before the refactor, narrow_* strategies emitted ``Assume.expression`` as
a Rust source string (``"var.contains(3)"``), and the schema-search backend
parsed those strings back with regex.  That made Step 2 (narrow) and
Step 1 (schema translation) implicitly coupled through a small,
brittle surface grammar.

Now narrow emits a structured :data:`AssumePred` object.
``Assume.pred`` carries the structured form; ``Assume.expression`` is
still populated via :meth:`to_rust`, so Verus-subprocess backends and
the witness renderer keep working verbatim.  The schema-search backend
matches on the pred type directly and never parses the Rust string.

Adding a new pred kind is a **one-place** change: define a frozen
dataclass with two methods and add it to :data:`AssumePred`.

    class MyPred:
        var: str
        ...
        def to_rust(self) -> str:
            ...                  # Rust rendering (used by Verus backends + reports)
        def match_and_bind(self, schema) -> Optional[dict]:
            ...                  # Return k-bindings if `schema` matches; else None

The schema-search translator iterates over available schemas and calls
``pred.match_and_bind(schema)`` on each; the first non-None wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from specdet.adapters.verus.native.schema_search.schemas import SchemaBinding


# Schema kind name strings (avoid importing SchemaKind to prevent layering
# inversions — predicates is a leaf module).
_SCALAR_EQ = "SCALAR_EQ"
_SCALAR_RANGE = "SCALAR_RANGE"
_VARIANT_IS = "VARIANT_IS"
_ENUM_DISC_EQ = "ENUM_DISC_EQ"
_BOOL_EQ = "BOOL_EQ"
_STR_EQ = "STR_EQ"
_SET_EMPTY = "SET_EMPTY"
_SET_LEN_GT = "SET_LEN_GT"
_SET_LEN_EQ = "SET_LEN_EQ"
_SEQ_LEN_EQ = "SEQ_LEN_EQ"
_SET_LEN_RANGE = "SET_LEN_RANGE"
_SEQ_LEN_RANGE = "SEQ_LEN_RANGE"
_SET_CONTAINS = "SET_CONTAINS"
_NOT_EQUAL_FN = "NOT_EQUAL_FN"


@dataclass(frozen=True)
class EqPred:
    """`var == value` for an integer-typed ``var``."""
    var: str
    value: int
    def to_rust(self) -> str:
        return f"{self.var} == {self.value}"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if s.kind.name == _SCALAR_EQ and s.rust_var == self.var:
            return {s.k_params[0][0]: self.value}
        return None


@dataclass(frozen=True)
class RangePred:
    """`var >= lo && var <= hi` for an integer-typed ``var``."""
    var: str
    lo: int
    hi: int
    def to_rust(self) -> str:
        return f"{self.var} >= {self.lo} && {self.var} <= {self.hi}"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if s.kind.name == _SCALAR_RANGE and s.rust_var == self.var:
            return {s.k_params[0][0]: self.lo, s.k_params[1][0]: self.hi}
        return None


@dataclass(frozen=True)
class DiscEqPred:
    """`var as int == value` for a C-like enum variable.

    Matches an ``ENUM_DISC_EQ`` schema whose template already contains the
    ``as int`` cast (emitted by the schema enumerator for C-like enums).
    Rendering includes the cast so the logged/witness form is
    Verus-valid (``r1->Ok_0 as int == 8``) rather than the raw form
    ``r1->Ok_0 == 8`` which would be ill-typed.
    """
    var: str
    value: int
    def to_rust(self) -> str:
        return f"{self.var} as int == {self.value}"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if s.kind.name == _ENUM_DISC_EQ and s.rust_var == self.var:
            return {s.k_params[0][0]: self.value}
        return None


@dataclass(frozen=True)
class VariantIsPred:
    """`var is Variant` for Result/Option/enum."""
    var: str
    variant: str
    def to_rust(self) -> str:
        return f"{self.var} is {self.variant}"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if (s.kind.name == _VARIANT_IS and s.rust_var == self.var
                and s.variant == self.variant):
            return {}
        return None


@dataclass(frozen=True)
class BoolPred:
    """`var == true` / `var == false`."""
    var: str
    value: bool
    def to_rust(self) -> str:
        return f"{self.var} == {'true' if self.value else 'false'}"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if (s.kind.name == _BOOL_EQ and s.rust_var == self.var
                and s.bool_value == self.value):
            return {}
        return None


@dataclass(frozen=True)
class StrEqPred:
    """`var@ == "literal"@` — string content narrowing."""
    var: str
    value: str
    def to_rust(self) -> str:
        return f'({self.var})@ == "{self.value}"@'
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if (s.kind.name == _STR_EQ and s.rust_var == self.var
                and s.str_value == self.value):
            return {}
        return None


@dataclass(frozen=True)
class SetEmptyPred:
    """`var == Set::<T>::empty()`."""
    var: str
    elem_ty_name: str
    def to_rust(self) -> str:
        return f"{self.var} == Set::<{self.elem_ty_name}>::empty()"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if s.kind.name == _SET_EMPTY and s.rust_var == self.var:
            return {}
        return None


@dataclass(frozen=True)
class SetLenGtPred:
    """`var.len() > 0` — used to rule out infinite-set witness."""
    var: str
    def to_rust(self) -> str:
        return f"{self.var}.len() > 0"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if s.kind.name == _SET_LEN_GT and s.rust_var == self.var:
            return {}
        return None


@dataclass(frozen=True)
class LenEqPred:
    """`var.len() == n` — Set or Seq cardinality."""
    var: str
    n: int
    def to_rust(self) -> str:
        return f"{self.var}.len() == {self.n}"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if (s.kind.name in (_SET_LEN_EQ, _SEQ_LEN_EQ)
                and s.rust_var == self.var):
            return {s.k_params[0][0]: self.n}
        return None


@dataclass(frozen=True)
class LenRangePred:
    """`var.len() >= lo && var.len() <= hi`."""
    var: str
    lo: int
    hi: int
    def to_rust(self) -> str:
        return f"{self.var}.len() >= {self.lo} && {self.var}.len() <= {self.hi}"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if (s.kind.name in (_SET_LEN_RANGE, _SEQ_LEN_RANGE)
                and s.rust_var == self.var):
            return {s.k_params[0][0]: self.lo, s.k_params[1][0]: self.hi}
        return None


@dataclass(frozen=True)
class SetContainsPred:
    """`var.contains(elem)` — probe a set for membership."""
    var: str
    elem: int
    def to_rust(self) -> str:
        return f"{self.var}.contains({self.elem})"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if s.kind.name == _SET_CONTAINS and s.rust_var == self.var:
            return {s.k_params[0][0]: self.elem}
        return None


@dataclass(frozen=True)
class SetLiteralPred:
    """`var == Set::<T>::empty().insert(e1).insert(e2)...` — final
    confirmation after contains-probing enumerated all elements.

    No schema exists for full-set equality (it would need one schema per
    cardinality); the schema-search translator treats this pred as
    unsupported; this is not a proof result.
    """
    var: str
    elem_ty_name: str
    elements: tuple[int, ...]
    def to_rust(self) -> str:
        expr = f"Set::<{self.elem_ty_name}>::empty()"
        for e in self.elements:
            expr += f".insert({e})"
        return f"{self.var} == {expr}"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        return None


@dataclass(frozen=True)
class NotEqualFnPred:
    """Distinctness step: `!fn(arg1, arg2, ...)` — the final witness
    that the two output tuples are non-equivalent under the chosen
    equality relation.  Carries the pre-rendered call so we don't have
    to re-synthesise the argument list here."""
    call: str
    def to_rust(self) -> str:
        return f"!{self.call}"
    def match_and_bind(self, s: "SchemaBinding") -> Optional[dict]:
        if s.kind.name == _NOT_EQUAL_FN:
            return {}
        return None


AssumePred = Union[
    EqPred, RangePred, DiscEqPred, VariantIsPred, BoolPred, StrEqPred,
    SetEmptyPred, SetLenGtPred, LenEqPred, LenRangePred,
    SetContainsPred, SetLiteralPred, NotEqualFnPred,
]
