"""Pure source preparation and injection helpers; no compiler execution."""
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

from .syntax import mask_noncode

logger = logging.getLogger(__name__)

_INJECT_BEGIN = "// === INJECTED DET CHECK ===\n"
_INJECT_END = "// === END INJECTED ===\n"


def _find_verus_block_close(source: str) -> int:
    """Return index of the closing ``}`` of the outermost ``verus! { ... }``.

    Returns ``-1`` if no ``verus! { ... }`` block is found. The scanner is
    aware of line/block comments and string/char/raw-string literals so
    braces inside those constructs do not perturb the balance count.
    """
    masked = mask_noncode(source)
    m = re.search(r"\bverus\s*!\s*\{", masked)
    if not m:
        return -1
    i = m.end()
    depth = 1
    n = len(source)
    while i < n and depth > 0:
        c = masked[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _fix_points_to_addr_projection(code: str, source: str) -> str:
    """Rewrite the PointsTo address projection for ``simple_pptr`` sources.

    The POINTS_TO equal/narrow strategies emit ``.ptr().addr()``, which is the
    ``vstd::raw_ptr::PointsTo`` spelling. ``vstd::simple_pptr::PointsTo`` has no
    ``.ptr()`` method — its address projection is ``.pptr().addr()`` (or the
    direct ``.addr()``) — so the raw_ptr spelling fails to compile with
    ``E0599: no method named `ptr` ``. Only the tool-generated ``code`` is
    rewritten (never the user source), and only when the file uses simple_pptr,
    so raw_ptr projects keep the correct ``.ptr().addr()`` spelling. This mirrors
    the existing workaround in ``scripts/source_native_atmosphere_smoke.py``.
    """
    if re.search(r"\bsimple_pptr\b", source):
        return code.replace(".ptr().addr()", ".pptr().addr()")
    return code


def _inject_into_source(
    source: str,
    code: str,
    *,
    open_closed_specs: Optional[Iterable[str]] = None,
    allowed_transformations: Iterable[str] = (),
    diagnostics: Optional[list[str]] = None,
) -> str:
    """Inject into a balanced Verus block, with explicitly authorized rewrites.

    By default the source is preserved except for injection. Opt-in
    transformation names are listed in ``SOURCE_TRANSFORMATIONS``. Every
    effective transformation is logged and appended to ``diagnostics``.
    ``open_closed_specs`` explicitly authorizes adding ``verifier::opaque``
    to the named closed specifications; it does not make them public/open.

    ``deps_hack_stubs`` changes the model and must never be treated as a
    semantics-preserving compatibility rewrite.
    """
    allowed = set(allowed_transformations)
    unknown = allowed - SOURCE_TRANSFORMATIONS
    if unknown:
        raise ValueError(f"Unknown source transformations: {sorted(unknown)}")

    def record(message: str) -> None:
        logger.warning(message)
        if diagnostics is not None:
            diagnostics.append(message)

    for name, transform in (
        ("self_reference_equality", _rewrite_self_eq_old_self),
        ("reference_equality", _rewrite_ref_eq_ref),
        ("mutable_reference_ensures", _rewrite_mut_self_in_ensures),
        ("inherent_view_trait_impls", _synthesize_view_trait_impls),
        ("deps_hack_stubs", _rewrite_deps_hack),
        ("repr_transparent_lint", _allow_repr_transparent_lint),
    ):
        if name not in allowed:
            continue
        transformed = transform(source)
        if transformed != source:
            if name == "deps_hack_stubs":
                record(f"Applied MODEL-CHANGING source transformation: {name}")
            else:
                record(f"Applied source transformation: {name}")
            source = transformed
    if open_closed_specs:
        from specdet.adapters.verus.native.spec_helpers import rewrite_closed_to_opaque
        names = sorted(set(open_closed_specs))
        transformed = rewrite_closed_to_opaque(source, names)
        if transformed != source:
            record(f"Made closed specs explicitly revealable: {', '.join(names)}")
            source = transformed
    if "points_to_address_projection" in allowed:
        transformed = _fix_points_to_addr_projection(code, source)
        if transformed != code:
            record("Applied generated-code transformation: points_to_address_projection")
            code = transformed
    idx = _find_verus_block_close(source)
    if idx == -1:
        raise ValueError("No balanced verus! block found; cannot select an injection scope")
    shim = ""
    if ("lemma_seq_properties_shim" in allowed
            and re.search(r"\blemma_seq_properties\s*::\s*<", source)
            and not re.search(r"\bfn\s+lemma_seq_properties\b", source)):
        record("Applied compatibility shim: lemma_seq_properties_shim")
        shim = _LEMMA_SEQ_PROPERTIES_SHIM
    return (
        source[:idx]
        + "\n" + _INJECT_BEGIN + shim + code + "\n" + _INJECT_END + "\n"
        + source[idx:]
    )


SOURCE_TRANSFORMATIONS = frozenset({
    "self_reference_equality",
    "reference_equality",
    "mutable_reference_ensures",
    "inherent_view_trait_impls",
    "deps_hack_stubs",
    "repr_transparent_lint",
    "points_to_address_projection",
    "lemma_seq_properties_shim",
})

inject_into_source = _inject_into_source


# ISSUES.md#B-5: bare ``self == old(self)`` in invariants / ensures of
# ``&mut self`` methods triggers the Verus "Dereference this mutable reference"
# error under current strictness. Rewrite to the dereferenced form. We match
# both orderings symmetrically; the lookbehind/ahead guard ensures we do not
# rewrite ``foo.self == ...`` or already-prefixed forms.
_SELF_EQ_OLD_SELF_RE = re.compile(
    r"(?<![\w*.])self\s*==\s*old\s*\(\s*self\s*\)(?![\w*.])"
)
_OLD_SELF_EQ_SELF_RE = re.compile(
    r"(?<![\w*.])old\s*\(\s*self\s*\)\s*==\s*self(?![\w*.])"
)


def _rewrite_self_eq_old_self(source: str) -> str:
    source = _SELF_EQ_OLD_SELF_RE.sub("*self == *old(self)", source)
    source = _OLD_SELF_EQ_SELF_RE.sub("*old(self) == *self", source)
    return source


# ---------------------------------------------------------------------------
# deps_hack cross-crate shim (storage corpus)
#
# The storage project imports a sibling proc-macro crate ``deps_hack`` via
# ``use deps_hack::{PmSized, pmsized_primitive};``. In single-file mode that
# crate is unresolvable (E0432), and both ``#[derive(PmSized)]`` and the
# ``pmsized_primitive!`` macro likewise cannot be expanded.
#
# We rewrite the file to be self-contained:
#   1. Remove the ``use deps_hack::{...};`` line.
#   2. Strip ``PmSized`` from any ``#[derive(...)]`` annotation, capturing
#      the struct name that previously got the derive.
#   3. Drop ``pmsized_primitive!(T);`` macro invocations, capturing the
#      primitive type.
#   4. Append stub trait impls (PmSized / SpecPmSized / UnsafeSpecPmSized
#      where applicable) at module scope. The stubs return 0 for sizes /
#      aligns - safe for the det check because both runs use the same
#      trait impl, so determinism is preserved.
#
# Trait declarations are defined locally in storage source files, so once
# the import is gone the file is self-contained.
# ---------------------------------------------------------------------------

_DEPS_HACK_USE_RE = re.compile(
    r"^[ \t]*use\s+deps_hack\s*::\s*(?P<rhs>\{[^}]*\}|[^;\n]+?)\s*;\s*\n",
    re.MULTILINE,
)


def _parse_deps_hack_imports(rhs: str) -> list[str]:
    """Split the right-hand-side of ``use deps_hack::<rhs>;`` into items.

    Examples:
        ``pmsized_primitive`` -> ``["pmsized_primitive"]``
        ``{PmSized, pmsized_primitive}`` -> ``["PmSized", "pmsized_primitive"]``
        ``{crc64fast::Digest, pmsized_primitive}``
            -> ``["crc64fast::Digest", "pmsized_primitive"]``
    """
    rhs = rhs.strip()
    if rhs.startswith("{") and rhs.endswith("}"):
        rhs = rhs[1:-1]
    return [p.strip() for p in rhs.split(",") if p.strip()]


def _deps_hack_type_imports(items: list[str]) -> list[str]:
    """Filter to items that look like type imports (have a ``::`` path
    component, OR start with an uppercase letter excluding the known
    trait+derive names that we handle separately).

    ``PmSized`` is intentionally excluded; it is handled by the derive
    stripper which emits stub impls of the locally-defined trait.
    """
    out: list[str] = []
    for it in items:
        last = it.split("::")[-1]
        if not last or not last[0].isupper():
            continue
        if last == "PmSized":
            continue
        out.append(last)
    return out
_DERIVE_RE = re.compile(r"#\[derive\s*\(\s*(?P<inner>[^)]*)\)\s*\]")
_PMSIZED_PRIM_RE = re.compile(
    r"^[ \t]*pmsized_primitive\s*!\s*\(\s*(?P<ty>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;\s*\n",
    re.MULTILINE,
)
_STRUCT_AFTER_DERIVE_RE = re.compile(
    r"(?:#\[[^\]]*\]\s*)*"  # any leading attrs (repr(C) etc.)
    r"pub\s+struct\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
)


def _rewrite_deps_hack(source: str) -> str:
    """Strip ``deps_hack`` references and append stub trait impls.

    No-op if the source doesn't import ``deps_hack``.
    """
    if "use deps_hack" not in source:
        return source

    # 1. Capture all imported items from deps_hack and drop the lines.
    type_imports: list[str] = []
    for m in _DEPS_HACK_USE_RE.finditer(source):
        type_imports.extend(_deps_hack_type_imports(
            _parse_deps_hack_imports(m.group("rhs"))
        ))
    source = _DEPS_HACK_USE_RE.sub("", source)

    # 2. Strip `PmSized` from #[derive(...)] lists, capturing struct names.
    derived_structs: list[str] = []

    def _scrub_derive(m: re.Match) -> str:
        inner = m.group("inner")
        parts = [p.strip() for p in inner.split(",")]
        if "PmSized" not in parts:
            return m.group(0)
        parts = [p for p in parts if p != "PmSized"]
        # Look ahead in the source from this match to find the struct
        # name. We pass a flag back via the captured list using a sentinel.
        end = m.end()
        tail = source[end:end + 1000]
        sm = _STRUCT_AFTER_DERIVE_RE.match(tail.lstrip())
        if sm:
            derived_structs.append(sm.group("name"))
        if not parts:
            return ""
        return "#[derive(" + ", ".join(parts) + ")]"

    source = _DERIVE_RE.sub(_scrub_derive, source)

    # 3. Drop pmsized_primitive!(T); macro calls, capture types.
    primitive_types: list[str] = []
    for m in _PMSIZED_PRIM_RE.finditer(source):
        primitive_types.append(m.group("ty"))
    source = _PMSIZED_PRIM_RE.sub("", source)

    # 4. Append stub trait impls at module scope (end of file). We
    # synthesize: SpecPmSized (with spec_size_of/spec_align_of returning 0),
    # UnsafeSpecPmSized (marker), PmSized (with size_of/align_of returning
    # 0). The bodies are inside ``verus! { ... }`` so they are fully
    # verified. ConstPmSized is added for primitives only (outside
    # verus! since the trait is itself outside).
    # Type-name imports from deps_hack (e.g., ``crc64fast::Digest``) are
    # also stubbed via empty structs so any source references to them
    # (typically as fields of ``#[verifier::external_body]`` structs)
    # resolve.
    if not derived_structs and not primitive_types and not type_imports:
        return source

    stubs: list[str] = ["\n// === DEPS_HACK STUBS (single-file shim) ===\n"]
    for name in type_imports:
        stubs.append(f"pub struct {name} {{}}\n")
    stubs.append("verus! {\n")
    for name in derived_structs + primitive_types:
        stubs.append(
            f"impl SpecPmSized for {name} {{\n"
            f"    open spec fn spec_size_of() -> nat {{ 0 }}\n"
            f"    open spec fn spec_align_of() -> nat {{ 0 }}\n"
            f"}}\n"
            f"unsafe impl UnsafeSpecPmSized for {name} {{}}\n"
            f"unsafe impl PmSized for {name} {{\n"
            f"    fn size_of() -> usize {{ 0 }}\n"
            f"    fn align_of() -> usize {{ 0 }}\n"
            f"}}\n"
        )
    stubs.append("} // verus!\n")
    for name in primitive_types:
        stubs.append(
            f"unsafe impl ConstPmSized for {name} {{\n"
            f"    const SIZE: usize = 0;\n"
            f"    const ALIGN: usize = 0;\n"
            f"}}\n"
        )
    stubs.append("// === END DEPS_HACK STUBS ===\n")
    return source + "".join(stubs)


# ---------------------------------------------------------------------------
# Generalised reference-equality rewrite (ISSUES.md#B-5 extension)
#
# In addition to `self == old(self)`, current Verus also rejects ANY
# `<ref-param> == <ref-param>` comparison in spec context (e.g.
# ``ensures self == src`` where ``self: &mut Self`` and ``src: &T``).
# The fix is identical: rewrite to ``*lhs == *rhs``. We need parameter-type
# context to decide, so this pass walks fn declarations with brace/paren
# matching, parses each fn's signature, and rewrites only IDENT==IDENT
# patterns where BOTH names are reference-typed parameters of the
# enclosing fn.
# ---------------------------------------------------------------------------

_FN_DECL_RE = re.compile(
    r"\b(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?fn\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>(]*>)?\s*\("
)


_REPR_TRANSPARENT_RE = re.compile(r"#\[\s*repr\s*\(\s*transparent\s*\)\s*\]")


def _allow_repr_transparent_lint(source: str) -> str:
    """Suppress the ``repr_transparent_non_zst_fields`` lint at crate level.

    Newer rustc (2024+) hard-errors on ``#[repr(transparent)]`` structs that
    contain a ZST field of an external type with private fields — e.g.
    Verus's ``Ghost<T>`` / ``Tracked<T>``. The nrkernel corpus has structs
    like ``struct PDE { entry: usize, layer: Ghost<nat> }`` that hit this.
    Emitting a crate-level ``#![allow(repr_transparent_non_zst_fields)]``
    is the minimal-impact fix: layout semantics are preserved, only the
    lint is silenced. No-op if the source has no ``#[repr(transparent)]``
    or already declares the allow.
    """
    if not _REPR_TRANSPARENT_RE.search(source):
        return source
    if "repr_transparent_non_zst_fields" in source:
        return source
    attr = "#![allow(repr_transparent_non_zst_fields)]\n"
    # Crate-level inner attrs must precede any item but may follow other
    # inner attrs / shebangs / BOM. Skip leading shebang and inner attrs.
    i = 0
    n = len(source)
    if source.startswith("\ufeff"):
        i = 1
    if source[i:i+2] == "#!" and source[i:i+3] != "#![":
        # shebang line
        nl = source.find("\n", i)
        i = nl + 1 if nl != -1 else n
    # Skip existing inner attrs `#![...]`
    while i < n:
        # skip whitespace / comments
        while i < n and source[i] in " \t\r\n":
            i += 1
        if source[i:i+3] == "#![":
            end = _find_balanced(source, i + 2, "[", "]")
            if end == -1:
                break
            i = end
            continue
        if source[i:i+2] == "//":
            nl = source.find("\n", i)
            i = nl + 1 if nl != -1 else n
            continue
        if source[i:i+2] == "/*":
            end = source.find("*/", i + 2)
            i = end + 2 if end != -1 else n
            continue
        break
    return source[:i] + attr + source[i:]


def _find_balanced(source: str, start: int, open_ch: str, close_ch: str) -> int:
    """Return index AFTER matching close_ch for the open_ch at ``start``."""
    depth = 0
    i = start
    while i < len(source):
        c = source[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _split_top_level_commas(text: str) -> list[str]:
    """Split ``text`` by top-level commas (ignoring those inside (), [], <>)."""
    parts: list[str] = []
    depth = 0
    last = 0
    for i, c in enumerate(text):
        if c in "([<":
            depth += 1
        elif c in ")]>":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(text[last:i])
            last = i + 1
    parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]


def _parse_fn_params(params_text: str) -> dict[str, str]:
    """Return {param_name: ref_kind} for each parameter in ``params_text``.

    ``ref_kind`` is one of:

    - ``"mut_ref"``  — ``&mut T`` or ``&mut self``
    - ``"shared_ref"`` — ``&T`` or ``&self``
    - ``"value"``  — owned ``T`` or ``self`` / ``mut self``
    """
    out: dict[str, str] = {}
    for p in _split_top_level_commas(params_text):
        if re.match(r"^&\s*mut\s+self\b", p):
            out["self"] = "mut_ref"
            continue
        if re.match(r"^&\s*self\b", p):
            out["self"] = "shared_ref"
            continue
        if p == "self" or re.match(r"^mut\s+self\b", p):
            out["self"] = "value"
            continue
        m = re.match(
            r"^(?:mut\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<ty>.+)$",
            p,
        )
        if not m:
            # Verus destructure pattern: ``Tracked(name): Tracked<...>`` /
            # ``Ghost(name): Ghost<...>``. Pull the inner binding name and
            # treat the param as having the inner generic type. If the
            # inner type is ``&mut T`` the binding is a mut-ref and any
            # ``old(name)`` / bare ``name`` references in ensures must
            # be rewritten via ``final(name)`` like a normal mut-ref.
            md = re.match(
                r"^(?P<ctor>Ghost|Tracked)\s*\(\s*(?:mut\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*:\s*"
                r"(?P=ctor)\s*<\s*(?P<inner>.+?)\s*>\s*$",
                p,
            )
            if not md:
                continue
            name = md.group("name")
            inner = md.group("inner").strip()
            if re.match(r"^&\s*mut\b", inner):
                out[name] = "mut_ref"
            elif inner.startswith("&"):
                out[name] = "shared_ref"
            else:
                out[name] = "value"
            continue
        name = m.group("name")
        ty = m.group("ty").strip()
        if re.match(r"^&\s*mut\b", ty):
            out[name] = "mut_ref"
        elif ty.startswith("&"):
            out[name] = "shared_ref"
        else:
            out[name] = "value"
    return out


_IDENT_EQ_IDENT_RE = re.compile(
    r"(?<![\w*.])([A-Za-z_][A-Za-z0-9_]*)\s*==\s*([A-Za-z_][A-Za-z0-9_]*)(?![\w*.])"
)


# Clause keywords that may appear in a fn signature before the body block.
# Order in source: optional return type, then any of these in any order.
_CLAUSE_KW_RE = re.compile(
    r"\b(requires|ensures|invariant|invariant_except_break|decreases|recommends|opens_invariants)\b"
)


def _clause_at(source: str, sig_start: int, pos: int) -> str:
    """Return the name of the clause containing ``pos`` within a fn signature
    that starts at ``sig_start``. Returns the clause keyword (e.g. ``"ensures"``)
    or ``""`` if no clause keyword has been seen yet (i.e. still in return-type
    section).
    """
    last = ""
    for m in _CLAUSE_KW_RE.finditer(source, sig_start, pos):
        last = m.group(1)
    return last


def _rewrite_ref_eq_ref(source: str) -> str:
    """Rewrite ``IDENT == IDENT`` between two reference-typed params.

    Walks each ``fn`` declaration, parses its parameter list, and within the
    fn signature region (clauses + body block) rewrites bare ``a == b`` to
    a dereferenced form when **both** ``a`` and ``b`` are reference-typed
    params (or ``self`` bound by ``&self`` / ``&mut self``).

    Context-sensitive disambiguation:

    - In ``ensures`` clauses, ``&mut`` operands are wrapped as
      ``*final(name)`` (Verus requires explicit ``old``/``final`` for
      ``&mut`` references in postconditions).
    - In ``requires``/``invariant``/other clauses and in the fn body,
      ``&mut`` operands use the bare ``*name`` form.
    - Shared-reference operands always use bare ``*name``.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        m = _FN_DECL_RE.search(source, i)
        if not m:
            out.append(source[i:])
            break
        out.append(source[i:m.start()])
        paren_open = m.end() - 1
        if paren_open < 0 or source[paren_open] != "(":
            out.append(source[m.start():m.end()])
            i = m.end()
            continue
        paren_close = _find_balanced(source, paren_open, "(", ")")
        if paren_close < 0:
            out.append(source[m.start():])
            break
        params_text = source[paren_open + 1:paren_close - 1]
        params = _parse_fn_params(params_text)
        ref_names = {n_ for n_, k in params.items() if k in ("mut_ref", "shared_ref")}

        body_open = source.find("{", paren_close)
        semi = source.find(";", paren_close)
        if semi >= 0 and (body_open < 0 or semi < body_open):
            region_end = semi + 1
        else:
            if body_open < 0:
                out.append(source[m.start():])
                break
            body_close = _find_balanced(source, body_open, "{", "}")
            if body_close < 0:
                out.append(source[m.start():])
                break
            region_end = body_close

        segment_start = m.start()
        segment = source[segment_start:region_end]
        sig_local_start = paren_close - segment_start  # paren_close relative to segment

        if len(ref_names) >= 2:
            def _repl(mm: re.Match) -> str:
                lhs, rhs = mm.group(1), mm.group(2)
                if lhs not in ref_names or rhs not in ref_names:
                    return mm.group(0)
                # Locate the clause at the match position within the segment.
                pos_in_seg = mm.start()
                clause = _clause_at(segment, sig_local_start, pos_in_seg)

                def deref(name: str) -> str:
                    kind = params.get(name, "value")
                    # `&mut` in postcondition needs final/old wrapping.
                    if kind == "mut_ref" and clause == "ensures":
                        return f"*final({name})"
                    return f"*{name}"

                return f"{deref(lhs)} == {deref(rhs)}"
            segment = _IDENT_EQ_IDENT_RE.sub(_repl, segment)
        out.append(segment)
        i = region_end
    return "".join(out)


# ---------------------------------------------------------------------------
# Rewrite bare `&mut` parameter accesses in ``ensures`` clauses
# ---------------------------------------------------------------------------
#
# Current Verus requires any ``&mut`` parameter (``&mut self`` or
# ``foo: &mut T``) to be disambiguated as ``old(name)`` (pre-state) or
# ``final(name)`` (post-state) anywhere it appears in a postcondition.
# Legacy corpora (atmosphere) write bare ``self.wf()`` / ``thread_perm@``
# / ``perm.field`` inside ``ensures`` clauses; the convention was
# "bare ``name`` = post-state". We mechanically rewrite those to
# ``final(name).<...>`` (matching the legacy semantics) while leaving
# ``old(name)`` / ``final(name)`` already-wrapped occurrences untouched.
#
# The rewriter only fires inside ``ensures`` clauses of methods that have
# at least one ``&mut`` parameter. Other clauses (``requires``,
# ``invariant``, ``decreases``, function bodies) accept bare references
# and are left alone.


_BODY_BLOCK_PRECEDERS = (
    "=>",
    "==>",
    "<==>",
    "&&&",
    "|||",
    "&&",
    "||",
    "=",
)
_BODY_BLOCK_PRECEDER_KEYWORDS = (
    "else",
    "match",
    "if",
    "while",
    "for",
    "loop",
    "do",
    "move",
    "async",
    "unsafe",
)


def _ends_with_block_operator(source: str, end_exclusive: int) -> bool:
    """Return True iff ``source[:end_exclusive]`` ends with an operator
    that opens a ``{...}`` expression-block (NOT a comparison op like
    ``==`` / ``!=``).

    We test operators in length-descending order so that ``==>`` wins
    over ``=`` and ``<==>`` wins over ``=>``.
    """
    candidates = sorted(_BODY_BLOCK_PRECEDERS, key=len, reverse=True)
    for op in candidates:
        if source.endswith(op, 0, end_exclusive):
            # Disqualify cases where the apparent operator is actually
            # part of a longer comparison / non-block op. e.g.
            # ``==`` matched but the preceding char is ``!`` (``!==``)
            # or the OP is ``=`` but actually ``==``/``!=``/``<=``/``>=``.
            start = end_exclusive - len(op)
            if op == "=" and start > 0 and source[start - 1] in "=!<>+-*/%":
                continue
            if op in ("&&", "||") and start > 0 and source[start - 1] in "&|":
                continue
            return True
    return False


def _looks_like_expression_block(source: str, brace_pos: int) -> bool:
    """Return True when the ``{`` at ``brace_pos`` is part of a Verus
    expression (match arm body, ``if``/``else`` block, ``forall``/``exists``
    bound expression, RHS of ``==>``/``=>``/``<==>``, etc.) rather than the
    fn body.

    We walk backwards from ``brace_pos`` skipping whitespace, then look
    at the immediately-preceding token. The walker only treats
    ``{`` as an expression-block when:

    (a) The IMMEDIATE token before ``{`` (after whitespace) is a known
        block-opener operator (``=>``/``==>``/``<==>``/``=``/``&&``/...).
    (b) The token before ``{`` is a closing bracket / identifier / call,
        and walking back through that whole expression we eventually
        hit a block-opener keyword (``match``/``if``/``else``/...).

    We STOP walking back at clause-list separators (``,``, ``;``,
    Verus clause keywords) — those indicate the ``{`` follows a
    completed clause expression, i.e. it's the body.
    """
    j = brace_pos - 1
    while j >= 0 and source[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return False

    # Case (a): IMMEDIATE operator preceder.
    if _ends_with_block_operator(source, j + 1):
        return True

    # Case (b): walk back through the expression to find the head token.
    pos = j
    start_limit = max(0, brace_pos - 400)
    steps = 0
    while pos >= start_limit:
        steps += 1
        if steps > 200:
            return False
        ch = source[pos]
        if ch in " \t\r\n":
            pos -= 1
            continue
        # Closing bracket: jump to matching open
        if ch in ")]>":
            open_ch, close_ch = {
                ")": ("(", ")"),
                "]": ("[", "]"),
                ">": ("<", ">"),
            }[ch]
            depth = 0
            k = pos
            while k >= 0:
                cc = source[k]
                if cc == close_ch:
                    depth += 1
                elif cc == open_ch:
                    depth -= 1
                    if depth == 0:
                        break
                k -= 1
            if k < 0:
                return False
            pos = k - 1
            continue
        if ch.isalnum() or ch == "_":
            k = pos
            while k >= 0 and (source[k].isalnum() or source[k] == "_"):
                k -= 1
            word = source[k + 1:pos + 1]
            if word in _BODY_BLOCK_PRECEDER_KEYWORDS:
                return True
            if word in (
                "requires", "ensures", "invariant", "decreases",
                "recommends", "opens_invariants", "invariant_except_break",
            ):
                return False
            pos = k
            continue
        if ch == "." or (ch == ":" and pos > 0 and source[pos - 1] == ":"):
            pos = pos - (2 if ch == ":" else 1)
            continue
        if ch == "|":
            bar_open = source.rfind("|", 0, pos)
            if bar_open > 0:
                head = source[max(0, bar_open - 12):bar_open]
                if re.search(r"\b(forall|exists|choose)\b", head):
                    return True
            return False
        # If we hit an operator that opens a block in this walk-back,
        # treat as expression. (Important: this catches ``if X
        # { ... } else { ... }`` — second ``{`` is preceded by ``else``
        # keyword, already handled above; this branch catches operator
        # forms like ``a + b { ... }`` which shouldn't appear in Verus
        # spec position but be defensive.)
        if _ends_with_block_operator(source, pos + 1):
            return True
        # Clause separator: body block.
        if ch in ",;":
            return False
        # `(` open: we walked OUT of an expression's paren block — must
        # be the start of the params or a quantifier; treat as body.
        if ch == "(":
            return False
        # Unknown char (e.g. `#`, `@`, `!`) — assume body to be safe.
        return False
    return False


def _find_fn_body_open(source: str, paren_close: int) -> int:
    """Find the body ``{`` of a fn whose params close at ``paren_close``.

    Naively using ``source.find("{", paren_close)`` may pick a ``{`` that
    belongs to an expression-block inside an ``ensures`` clause (e.g.
    ``ret.is_Some() ==> { ... }`` / ``match sm { ... }`` / ``forall |x|
    self.X <==> ({ ... })``).

    Rules:

    1. Track outer ``(...)`` / ``[...]`` bracket depth. A real body ``{``
       can only appear at depth 0.
    2. At depth 0, a ``{...}`` block is rejected as the body when:
       (a) the closing ``}`` is followed by ``,`` / ``;``, OR
       (b) the immediate "head" preceding the ``{`` is an operator /
           Verus expression keyword (``=>``, ``==>``, ``match X``,
           ``else``, ``forall|...|``, etc.).
    """
    pos = paren_close
    n = len(source)
    paren_depth = 0
    bracket_depth = 0
    while pos < n:
        c = source[pos]
        if c == "(":
            paren_depth += 1
        elif c == ")":
            paren_depth -= 1
        elif c == "[":
            bracket_depth += 1
        elif c == "]":
            bracket_depth -= 1
        elif c == "{" and paren_depth == 0 and bracket_depth == 0:
            close = _find_balanced(source, pos, "{", "}")
            if close < 0:
                return -1
            j = close
            while j < n and source[j] in " \t\r\n":
                j += 1
            if j < n and source[j] in ",;":
                pos = close
                continue
            if _looks_like_expression_block(source, pos):
                pos = close
                continue
            return pos
        pos += 1
    return -1


def _find_top_level_semi(source: str, start: int, end: Optional[int] = None) -> int:
    """Find the first ``;`` at bracket depth 0 in ``source[start:end]``.

    Semicolons nested inside ``(...)``, ``[...]`` or ``{...}`` are skipped.
    This matters for signatures whose ``requires`` / ``ensures`` clauses
    contain expression blocks with ``let`` bindings, e.g. ::

        ensures
            forall |k| { let ki = ...; ... },

    Here the ``;`` after ``let ki = ...`` is *not* a signature terminator;
    only a depth-0 ``;`` marks a bodyless forward declaration. Using a naive
    ``source.find(";", ...)`` would incorrectly treat such a method as a
    forward declaration and skip rewriting its ``ensures`` clause.
    """
    pos = start
    n = len(source) if end is None else min(end, len(source))
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0
    while pos < n:
        c = source[pos]
        if c == "(":
            paren_depth += 1
        elif c == ")":
            paren_depth -= 1
        elif c == "[":
            bracket_depth += 1
        elif c == "]":
            bracket_depth -= 1
        elif c == "{":
            brace_depth += 1
        elif c == "}":
            brace_depth -= 1
        elif c == ";" and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0:
            return pos
        pos += 1
    return -1


def _rewrite_mut_self_in_ensures(source: str) -> str:
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        m = _FN_DECL_RE.search(source, i)
        if not m:
            out.append(source[i:])
            break
        out.append(source[i:m.start()])
        paren_open = m.end() - 1
        if paren_open < 0 or source[paren_open] != "(":
            out.append(source[m.start():m.end()])
            i = m.end()
            continue
        paren_close = _find_balanced(source, paren_open, "(", ")")
        if paren_close < 0:
            out.append(source[m.start():])
            break
        params_text = source[paren_open + 1:paren_close - 1]
        params = _parse_fn_params(params_text)
        mut_ref_names = [n_ for n_, kind in params.items() if kind == "mut_ref"]
        if not mut_ref_names:
            out.append(source[m.start():paren_close])
            i = paren_close
            continue

        # Forward-declared trait method: ``fn foo(...);`` (no body).
        # ``_find_fn_body_open`` handles balanced clause-blocks like
        # ``ensures ... ==> { ... }`` and ``let x = ...;`` correctly, so
        # we treat a valid body-open as authoritative; only fall back to
        # the ``;`` lookup when no body `{` was found. The ``;`` lookup must
        # ignore semicolons nested inside clause expression-blocks (e.g.
        # ``forall |k| { let ki = ...; ... }`` in an ``ensures``); only a
        # depth-0 ``;`` before the body terminates a bodyless declaration.
        candidate_body = _find_fn_body_open(source, paren_close)
        semi_end = candidate_body if candidate_body >= 0 else None
        semi = _find_top_level_semi(source, paren_close, semi_end)
        if semi >= 0 and (candidate_body < 0 or semi < candidate_body):
            out.append(source[m.start():semi + 1])
            i = semi + 1
            continue
        if candidate_body < 0:
            out.append(source[m.start():])
            break
        body_open = candidate_body
        sig_text = source[paren_close:body_open]
        new_sig = _rewrite_ensures_in_sig(sig_text, mut_ref_names)
        body_close = _find_balanced(source, body_open, "{", "}")
        if body_close < 0:
            out.append(source[m.start():])
            break
        out.append(source[m.start():paren_close])
        out.append(new_sig)
        out.append(source[body_open:body_close])
        i = body_close
    return "".join(out)


def _rewrite_ensures_in_sig(sig_text: str, mut_ref_names: list[str]) -> str:
    """Within a fn signature region (between params `)` and body `{`),
    rewrite bare ``&mut`` parameter occurrences inside each ``ensures``
    clause. ``mut_ref_names`` lists the parameter names whose receiver
    is ``&mut`` (including ``"self"``).
    """
    matches = list(_CLAUSE_KW_RE.finditer(sig_text))
    if not matches:
        return sig_text
    out_parts: list[str] = []
    prev_end = 0
    for idx, mm in enumerate(matches):
        out_parts.append(sig_text[prev_end:mm.start()])
        kw = mm.group(1)
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(sig_text)
        clause_text = sig_text[mm.start():next_start]
        if kw == "ensures":
            clause_text = _rewrite_bare_refs_in_text(clause_text, mut_ref_names)
        out_parts.append(clause_text)
        prev_end = next_start
    out_parts.append(sig_text[prev_end:])
    return "".join(out_parts)


def _rewrite_bare_refs_in_text(text: str, names: list[str]) -> str:
    """Replace bare ``<name>`` references with the post-state form, for
    each name in ``names`` that is a ``&mut`` parameter.

    Verus rules for the result of ``final``:

    - ``final(x).field`` / ``final(x).method()`` / ``final(x)@`` — OK
      via implicit deref (``@`` desugars to ``View::view(self)`` which
      takes ``&self`` and Rust autoderefs).
    - ``final(x) == y`` / ``final(x), foo(final(x))`` — NOT OK; needs
      explicit ``*final(x)``.

    ``old(name)`` / ``final(name)`` already-wrapped forms are preserved.
    """
    if not names:
        return text
    # Sort by length desc to prevent shorter names eating into longer ones
    # (e.g. ``self`` and ``selfless`` if both happened to be params).
    names_sorted = sorted(names, key=len, reverse=True)
    name_alt = "|".join(re.escape(n) for n in names_sorted)

    placeholders: dict[str, str] = {}

    def _protect(m: re.Match) -> str:
        ph = f"__REF_PH_{len(placeholders)}__"
        placeholders[ph] = m.group(0)
        return ph

    # Protect already-wrapped forms.
    protected = re.sub(
        rf"\b(?:old|final)\(\s*(?:{name_alt})\s*,?\s*\)",
        _protect,
        text,
    )

    def _repl(m: re.Match) -> str:
        nm = m.group(0)
        start = m.start()
        # Preceded by `*` (explicit deref already present in source) =>
        # substitute without an extra `*`.
        i_prev = start - 1
        while i_prev >= 0 and protected[i_prev] in " \t\r\n":
            i_prev -= 1
        preceded_by_star = i_prev >= 0 and protected[i_prev] == "*"

        # `self` followed by `.IDENT` / `.method(...)` / `@` — auto-deref
        # via field/method/view. The chain dictates the operand type, so
        # we should NOT add an explicit `*` even if the surrounding context
        # is an equality comparison (the chain may evaluate to a value
        # already).
        end = m.end()
        j = end
        while j < len(protected) and protected[j] in " \t\r\n":
            j += 1
        followed_by_field = j < len(protected) and protected[j] in ".@"

        # Equality context: `X == name` or `name == X`. Verus rejects
        # ``final(name) == X`` because ``final`` returns a reference;
        # comparison requires the dereferenced value.
        eq_re = re.compile(r"^\s*(?:==|!=)")
        followed_by_eq = bool(eq_re.match(protected, end))
        preceded_by_eq = (
            i_prev >= 1
            and protected[i_prev] == "="
            and protected[i_prev - 1] in "=!"
        )

        if preceded_by_star:
            return f"final({nm})"
        if followed_by_field:
            # The chain continues — let the chain decide the type;
            # ``final(name).field`` / ``final(name)@`` are valid.
            return f"final({nm})"
        if followed_by_eq or preceded_by_eq:
            return f"*final({nm})"
        # Default: function-arg or general position. Most Verus spec
        # functions accept ``&Self`` / ``&T`` references, and Rust does
        # NOT auto-ref values across function-call boundaries. Returning
        # ``final(name)`` (which is itself a reference) is the safer
        # choice.
        return f"final({nm})"

    rewritten = re.sub(rf"(?<![\w.])(?:{name_alt})\b", _repl, protected)
    for ph, original in placeholders.items():
        rewritten = rewritten.replace(ph, original)
    return rewritten


# Back-compat alias kept for any external caller of the older name.
def _rewrite_bare_self_in_text(text: str) -> str:
    return _rewrite_bare_refs_in_text(text, ["self"])


# ---------------------------------------------------------------------------
# View trait synthesis (ISSUES.md#B-6)
#
# Newer Verus desugars ``self@`` strictly via ``View::view``. Legacy corpora
# (esp. atmosphere) define ``pub open spec fn view(&self) -> RetType`` as an
# inherent method on a struct but never write the corresponding
# ``impl View for T`` trait impl — they relied on the older sugar that
# fell back to the inherent method. We synthesize the missing trait impls
# by mirroring every inherent ``view(&self)`` we find inside a non-trait
# ``impl`` block.
# ---------------------------------------------------------------------------

_VIEW_FN_RE = re.compile(
    r"(?P<attrs>(?:\s*#\s*\[[^\]]+\]\s*)*)\bpub\s+(?P<spec_kind>open|closed)\s+spec\s+fn\s+view\s*\(\s*&\s*self\s*\)\s*->\s*"
)


def _scan_inherent_impl_blocks(source: str):
    """Yield (impl_head_text, type_clause, body_start, body_end) for each
    inherent ``impl`` block (not ``impl Trait for ...``).

    ``type_clause`` is the portion between ``impl`` (and optional ``<...>``
    generics) and the opening ``{`` — i.e. the type-being-implemented.
    """
    pos = 0
    while pos < len(source):
        head_re = re.compile(r"\bimpl\b", re.MULTILINE)
        m = head_re.search(source, pos)
        if not m:
            return
        head_start = m.start()
        # Walk forward, balancing < and > inside generics, to find `{`.
        j = m.end()
        # Skip optional whitespace + impl generics `<...>` (balanced).
        while j < len(source) and source[j].isspace():
            j += 1
        impl_generics = ""
        if j < len(source) and source[j] == "<":
            close = _find_balanced(source, j, "<", ">")
            if close < 0:
                return
            impl_generics = source[j:close]
            j = close
        # Now read the type clause up to `{`. If we hit `for ` keyword,
        # this is a trait impl — skip.
        rest_start = j
        # Find next `{` not inside `<...>` or `(...)`.
        depth_angle = 0
        depth_paren = 0
        k = j
        while k < len(source):
            c = source[k]
            if c == "<":
                depth_angle += 1
            elif c == ">":
                if depth_angle > 0:
                    depth_angle -= 1
            elif c == "(":
                depth_paren += 1
            elif c == ")":
                if depth_paren > 0:
                    depth_paren -= 1
            elif c == "{" and depth_angle == 0 and depth_paren == 0:
                break
            k += 1
        if k >= len(source):
            return
        type_clause = source[rest_start:k].strip()
        body_open = k
        body_close = _find_balanced(source, body_open, "{", "}")
        if body_close < 0:
            return
        # Trait impl detection: `for ` keyword in type_clause at top level.
        is_trait_impl = bool(
            re.search(r"\bfor\b", type_clause)
        )
        if not is_trait_impl:
            yield {
                "impl_generics": impl_generics,  # includes "<...>" or ""
                "type_clause": type_clause,  # e.g. "Foo<T, N>"
                "body_start": body_open + 1,
                "body_end": body_close - 1,
                "block_end": body_close,
            }
        pos = body_close


def _extract_inherent_view(body: str) -> Optional[dict]:
    """If ``body`` contains a ``pub open|closed spec fn view(&self) -> RetType { ... }``,
    return {ret_type, body, spec_kind}; else None."""
    m = _VIEW_FN_RE.search(body)
    if not m:
        return None
    # Return type extends until the next unbalanced `{` at depth 0 (ignoring
    # `where` clauses isn't necessary for inherent view in this corpus).
    ret_start = m.end()
    depth = 0
    j = ret_start
    while j < len(body):
        c = body[j]
        if c in "<(":
            depth += 1
        elif c in ">)":
            if depth > 0:
                depth -= 1
        elif c == "{" and depth == 0:
            break
        j += 1
    if j >= len(body):
        return None
    ret_type = body[ret_start:j].strip()
    # Skip `recommends ...` if present; capture only the body block.
    body_open = j
    body_close = _find_balanced(body, body_open, "{", "}")
    if body_close < 0:
        return None
    inner_body = body[body_open + 1:body_close - 1]
    # Trim `recommends ... ,` lines from the ret_type tail.
    # ret_type may include trailing `recommends ...` clauses; cut them out.
    rt = ret_type
    rec_idx = re.search(r"\brecommends\b", rt)
    if rec_idx:
        rt = rt[:rec_idx.start()].rstrip()
    rt = rt.rstrip(",").strip()
    return {
        "spec_kind": m.group("spec_kind"),
        "attrs": (m.group("attrs") or "").strip(),
        "ret_type": rt,
        "body": inner_body.strip(),
    }


def _synthesize_view_trait_impls(source: str) -> str:
    """Append an ``impl View for T`` trait impl for every inherent
    ``view(&self) -> RetType`` we find inside a non-trait ``impl`` block.

    Skips synthesis if an ``impl<...> View for <type_clause>`` already exists
    for the same type clause.
    """
    # Collect (impl_generics, type_clause, view_info) for each inherent view.
    candidates = []
    for blk in _scan_inherent_impl_blocks(source):
        body = source[blk["body_start"]:blk["body_end"]]
        info = _extract_inherent_view(body)
        if info is None:
            continue
        candidates.append({
            "impl_generics": blk["impl_generics"],
            "type_clause": blk["type_clause"],
            "view": info,
            "block_end": blk["block_end"],
        })
    if not candidates:
        return source

    # Filter out types that already have a View trait impl.
    existing = set()
    for m in re.finditer(
        r"impl\s*(?:<[^{]*?>)?\s*View\s+for\s+([^\s<{][^{]*?)\{",
        source,
    ):
        existing.add(m.group(1).strip())

    # Build synthesized blocks; insert after each candidate's inherent impl.
    # We append in REVERSE order so insertion offsets remain valid.
    candidates.sort(key=lambda c: c["block_end"], reverse=True)
    out_chars = list(source)
    for c in candidates:
        tc = c["type_clause"]
        if tc in existing:
            continue
        existing.add(tc)
        gen = c["impl_generics"]
        v = c["view"]
        # Preserve `#[verifier::external_body]` (or similar) when the
        # inherent view fn is marked as such — its body is a stub (often
        # ``unimplemented!()``) that Verus only accepts under external_body.
        attrs = v.get("attrs", "")
        attr_block = (attrs + "\n    ") if attrs else ""
        synth = (
            f"\n\n// === SYNTHESIZED View trait impl ===\n"
            f"impl{gen + ' ' if gen else ' '}View for {tc} {{\n"
            f"    type V = {v['ret_type']};\n"
            f"    {attr_block}{v['spec_kind']} spec fn view(&self) -> Self::V {{ "
            f"{v['body']} }}\n"
            f"}}\n"
        )
        out_chars.insert(c["block_end"], synth)
    return "".join(out_chars)


_LEMMA_SEQ_PROPERTIES_SHIM = (
    "// Compat shim for corpus source that calls the deprecated\n"
    "// `lemma_seq_properties` (renamed to broadcast group\n"
    "// `group_seq_properties` in current vstd).\n"
    "pub proof fn lemma_seq_properties<V>()\n"
    "    ensures true,\n"
    "{\n"
    "    broadcast use vstd::seq_lib::group_seq_properties;\n"
    "}\n\n"
)
