"""Pure syntax helpers for reachable and closed Verus specifications."""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .syntax import mask_noncode


_CALLEE_RE = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)\s*\(")



_SPEC_FN_HEADER_RE = re.compile(
    r"(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:open\s+|closed\s+)?"
    r"(?:uninterp\s+)?"
    r"spec\s+fn\s+"
    r"([A-Za-z_][A-Za-z_0-9]*)"
)



def _spec_fn_body(source: str, name: str) -> Optional[str]:
    """Return the body text (between the first ``{`` and matching ``}``)
    of a ``spec fn <name>`` defined in *source*, or ``None`` if the
    function isn't defined / has no body. Malformed source is rejected."""
    masked = mask_noncode(source)
    for m in _SPEC_FN_HEADER_RE.finditer(masked):
        if m.group(1) != name:
            continue
        # Find the opening brace for the body (skip the parameter list and
        # any ``-> RetType`` clause).
        i = m.end()
        depth_paren = 0
        body_open = -1
        while i < len(source):
            c = masked[i]
            if c == "(":
                depth_paren += 1
            elif c == ")":
                depth_paren -= 1
            elif c == "{" and depth_paren == 0:
                body_open = i
                break
            elif c == ";" and depth_paren == 0:
                # uninterp spec fn / forward decl — no body.
                break
            i += 1
        if body_open < 0:
            continue
        # Brace-match the body.
        depth = 0
        for j in range(body_open, len(source)):
            if masked[j] == "{":
                depth += 1
            elif masked[j] == "}":
                depth -= 1
                if depth == 0:
                    return source[body_open + 1 : j]
        raise ValueError(f"Unbalanced body for spec fn {name}")
    return None



_CLOSED_SPEC_FN_RE = re.compile(
    r"(?P<lead>(?:^|\n)[ \t]*)"
    r"(?P<vis>(?:pub(?:\([^)]*\))?\s+)?)"
    r"closed\s+spec\s+fn\s+"
    r"(?P<name>[A-Za-z_][A-Za-z_0-9]*)"
)



def reachable_spec_fns(
    ensures_texts: Iterable[str],
    source: str,
    *,
    max_depth: int = 4,
) -> set[str]:
    """Return the set of ``spec fn`` names transitively reachable from
    the ensures texts, restricted to those *defined in* ``source``
    (i.e., ``_spec_fn_body`` returns a body).

    Used by the det-check pipeline to know which closed spec fns to
    open + reveal."""
    joined = "\n".join(ensures_texts)
    seen: set[str] = set()
    reachable: set[str] = set()
    queue: list[tuple[str, int]] = [
        (name, 0) for name in _CALLEE_RE.findall(mask_noncode(joined))
    ]
    while queue:
        name, depth = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        if depth >= max_depth:
            continue
        body = _spec_fn_body(source, name)
        if body is None:
            continue
        reachable.add(name)
        for callee in _CALLEE_RE.findall(mask_noncode(body)):
            if callee not in seen:
                queue.append((callee, depth + 1))
    return reachable



def closed_spec_fns_in(source: str, names: Iterable[str]) -> set[str]:
    """Return the subset of ``names`` that are declared as ``closed
    spec fn`` in ``source`` (so are candidates for the ``closed → opaque
    open`` rewrite)."""
    names = set(names)
    found: set[str] = set()
    for m in _CLOSED_SPEC_FN_RE.finditer(mask_noncode(source)):
        if m.group("name") in names:
            found.add(m.group("name"))
    return found



_IMPL_HEADER_RE = re.compile(
    r"\bimpl\b"                                # impl keyword
    r"(?:\s*<(?P<generics>[^>]*)>)?"           # optional generics
    r"\s+"
    r"(?P<rest>[^{]+?)"                        # everything up to the brace
    r"\s*\{"
)



def _impl_generic_param_names(generics_text: str) -> set[str]:
    """Extract the BARE generic-parameter names from an ``impl<...>`` clause.

    ``<T, const N: usize, U: Foo>`` -> ``{"T", "N", "U"}``. Lifetimes
    are skipped. Only the identifier preceding ``:`` / ``=`` / ``,`` is
    captured.
    """
    if not generics_text:
        return set()
    out: set[str] = set()
    for part in generics_text.split(","):
        s = part.strip()
        if not s or s.startswith("'"):
            continue
        if s.startswith("const "):
            s = s[len("const "):]
        m = re.match(r"([A-Za-z_][A-Za-z_0-9]*)", s)
        if m:
            out.add(m.group(1))
    return out



def _impl_self_type(rest: str) -> Optional[str]:
    """Given the text between ``impl`` (incl. its generics) and the
    opening ``{``, return the bare Self-type identifier of the impl.

    Inherent impl ``impl Foo<K> { ... }``      -> ``"Foo"``
    Trait impl    ``impl Trait for Foo<K> {}`` -> ``"Foo"``
    """
    s = rest.strip()
    # ``for`` keyword at a word boundary marks a trait impl. Use rsplit
    # so we don't get confused by an earlier ``for`` inside a generic.
    m = re.search(r"\bfor\b", s)
    if m:
        s = s[m.end():].strip()
    # First identifier token in s is the Self type (possibly followed by
    # generic args / trait bounds / lifetime, all of which we strip).
    m2 = re.match(r"([A-Za-z_][A-Za-z_0-9]*)", s)
    return m2.group(1) if m2 else None



def _has_external_body_attr_before(source: str, pos: int) -> bool:
    """Return True if the closest preceding non-whitespace tokens before
    ``pos`` contain ``#[verifier::external_body]``. We scan up to ~160
    chars back (enough to skip another attribute or two)."""
    start = max(0, pos - 160)
    window = source[start:pos]
    # Walk backwards through attribute/whitespace lines. If we hit a
    # non-attribute, non-whitespace token, stop — the attribute (if any)
    # belongs to something else.
    lines = window.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#["):
            if "external_body" in stripped:
                return True
            continue
        # Hit a non-attr token before finding external_body.
        return False
    return False



def _has_opaque_attr_before(source: str, decl_start: int) -> bool:
    """Return True iff a ``#[verifier::opaque]`` attribute is present
    in the attribute-block immediately preceding ``decl_start``.

    Walks back over consecutive attribute lines (and blank lines)
    until a non-attribute line is reached, then returns True if any
    of those attribute lines contain ``opaque``.
    """
    window = source[max(0, decl_start - 240) : decl_start]
    lines = window.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#["):
            if "opaque" in stripped:
                return True
            continue
        return False
    return False



def closed_spec_fn_qualified_names(
    source: str,
    names: Iterable[str],
) -> dict[str, str]:
    """For each name in ``names`` that is declared as a ``closed spec fn``
    in ``source``, return the qualified path Verus needs to ``reveal`` it.

    Free fns (declared at module scope) map to their bare name.
    Impl-method spec fns (declared inside ``impl <Type> {...}``) map
    to ``"<Type>::<name>"``. ``impl Trait for Type`` correctly maps to
    ``<Type>::<name>`` (not ``<Trait>::<name>``).

    Skips declarations annotated with ``#[verifier::external_body]`` —
    their bodies are deliberately opaque (e.g., ``unimplemented!()``)
    and Verus rejects ``#[verifier::opaque]`` on them.

    Also skips declarations with no ``{ body }`` (forward decls / trait
    method signatures inside a ``trait`` block).

    Raises ValueError for ambiguous same-named closed declarations rather
    than choosing an unrelated method to reveal.

    A spec fn is considered to live inside an impl block iff the
    nearest enclosing ``{...}`` opened by an ``impl ... { ... }`` header
    surrounds it (by brace depth).
    """
    names = set(names)
    if not names:
        return {}
    masked = mask_noncode(source)

    # Pre-compute impl-block ranges: (open_brace_idx, close_brace_idx, self_type)
    impl_blocks: list[tuple[int, int, str]] = []
    # Spans of "skipped" impl blocks (blanket impls where the Self type
    # IS a generic parameter): we drop any closed spec fn declared in
    # one of these spans entirely, since ``T::name`` is not legal at
    # module scope and a bare ``name`` would also resolve incorrectly.
    skipped_impl_spans: list[tuple[int, int]] = []
    for m in _IMPL_HEADER_RE.finditer(masked):
        self_ty = _impl_self_type(m.group("rest"))
        if self_ty is None:
            continue
        # Brace-match to find the matching close.
        open_idx = m.end() - 1
        depth = 0
        close_idx = -1
        for j in range(open_idx, len(source)):
            if masked[j] == "{":
                depth += 1
            elif masked[j] == "}":
                depth -= 1
                if depth == 0:
                    close_idx = j
                    break
        if close_idx <= 0:
            continue
        # Blanket impl ``impl<T: Foo> Bar for T``: the Self type IS a
        # generic parameter. We cannot emit ``T::name`` in a reveal
        # because ``T`` is not in scope at the det-check call site,
        # and the bare ``name`` would resolve incorrectly (or not at
        # all). Record the block's span so we can drop decls inside.
        impl_generics = _impl_generic_param_names(m.group("generics") or "")
        if self_ty in impl_generics:
            skipped_impl_spans.append((open_idx, close_idx))
            continue
        impl_blocks.append((open_idx, close_idx, self_ty))

    def _is_in_skipped_impl(pos: int) -> bool:
        return any(o < pos < c for (o, c) in skipped_impl_spans)

    def _enclosing_impl_type(pos: int) -> Optional[str]:
        candidates = [
            (open_, close_, ty)
            for (open_, close_, ty) in impl_blocks
            if open_ < pos < close_
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1] - x[0])
        return candidates[0][2]

    out: dict[str, str] = {}
    for m in _CLOSED_SPEC_FN_RE.finditer(masked):
        name = m.group("name")
        if name not in names:
            continue
        # Skip declarations inside blanket impls (``impl<T: Foo> Bar for T``):
        # neither ``T::name`` nor bare ``name`` resolve correctly at the
        # det-check call site.
        if _is_in_skipped_impl(m.start("name")):
            continue
        # Skip if marked external_body — has no real Verus-visible body.
        if _has_external_body_attr_before(source, m.start()):
            continue
        # Skip if there's no body block (forward decl in a trait).
        if _spec_fn_body(source, name) is None:
            continue
        if name in out:
            raise ValueError(
                f"Ambiguous closed spec fn {name!r}; provide an unambiguous source context "
                "instead of choosing a declaration to reveal"
            )
        ty = _enclosing_impl_type(m.start("name"))
        out[name] = f"{ty}::{name}" if ty else name
    return out



def rewrite_closed_to_opaque(
    source: str,
    names: Iterable[str],
) -> str:
    """Inject ``#[verifier::opaque]`` on top of each ``[pub] closed
    spec fn <name>`` declaration in ``names``. Returns the modified
    source text.

    Critically, we **do not** rewrite ``closed`` to ``open``. Verus
    requires ``open spec fn`` to be ``pub`` (otherwise: "function is
    marked `open` but not marked `pub`"), but ``pub open spec fn``
    requires the body to be well-formed at every external call site
    — which fails when the body references module-private fields
    (e.g. ``self.delegation_map`` on a struct whose fields are
    pkg-private). Verus does, however, accept
    ``#[verifier::opaque] pub closed spec fn``: the body is closed
    by default (preserving the visibility invariant) but can be
    revealed inside our injected det-check proof with
    ``reveal(<qualified_name>);``.

    Idempotent: skips declarations already preceded by an
    ``#[verifier::opaque]`` attribute (the regex match still fires,
    but ``_already_has_opaque_attr_before`` returns True). Idempotent
    by inspection of the immediate preceding attribute line(s).

    Skips declarations annotated with ``#[verifier::external_body]``
    — Verus rejects ``#[verifier::opaque]`` on those. Also skips
    declarations with no ``{ body }`` (forward decls in trait blocks
    — also rejected by ``#[verifier::opaque]``).

    The injected ``#[verifier::opaque]`` attribute is placed on its
    own line preceding the original modifier, matching the ironkv
    convention (see e.g.
    ``host_impl_v__impl2__real_init_impl.rs:1110``).
    """
    names = set(names)
    if not names:
        return source

    def _sub(m: "re.Match[str]") -> str:
        if m.group("name") not in names:
            return source[m.start():m.end()]
        # Skip external_body decls (Verus rejects opaque on them).
        if _has_external_body_attr_before(source, m.start()):
            return source[m.start():m.end()]
        # Skip if already annotated opaque (idempotency).
        if _has_opaque_attr_before(source, m.start()):
            return source[m.start():m.end()]
        # Skip bodyless forward decls.
        if _spec_fn_body(source, m.group("name")) is None:
            return source[m.start():m.end()]
        lead = m.group("lead")
        vis = m.group("vis")
        name = m.group("name")
        prefix_newline = "\n" if lead.startswith("\n") else ""
        prefix_indent = lead.lstrip("\n")
        return (
            f"{prefix_newline}{prefix_indent}#[verifier::opaque]"
            f"\n{prefix_indent}{vis}closed spec fn {name}"
        )

    for match in reversed(list(_CLOSED_SPEC_FN_RE.finditer(mask_noncode(source)))):
        source = source[:match.start()] + _sub(match) + source[match.end():]
    return source
