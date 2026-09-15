"""Provider-independent lexical checks for supplied Verus proof blocks.

These checks reject known axiom-adding and out-of-scope constructs. They do
not replace compiler verification or validation of trusted dependencies.
The forbidden constructs include:

  ``assume(P)``       — adds P as an axiom without checking it. Verus
                        silently accepts ``assume(false)`` → trivially
                        proves any postcondition. **The reason this
                        scanner exists.**
  ``admit()``         — explicitly discharges any pending obligation.
  ``unimplemented!()``— Rust macro; Verus treats the call site as having
                        any postcondition (since it can't be reached).
  ``unreachable!()``  — same reasoning.
  ``assume_specification`` — declares an extern fn's spec without checking.
  ``#[verifier::external_body]`` / ``#[verifier(external_body)]`` —
                        Verus skips body verification; supplied proof
                        blocks must not introduce this attribute.
  New ``fn`` / ``spec fn`` / ``proof fn`` / ``exec fn`` definitions —
                        the prompt asks for *statements* to put inside an
                        existing proof body; new fn defs are out-of-scope
                        and create a vector for axiom-style declarations.
  ``impl`` / ``trait`` / ``struct`` / ``enum`` / ``type`` items —
                        same reason; items belong at module scope, not
                        inside a proof body.

The scan strips comments and string literals first so commented-out
mentions of ``assume`` don't false-positive. Returns a list of
:class:`SandboxViolation` entries with line offsets pointing into the
ORIGINAL block so callers can report precise diagnostics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List

from .syntax import LexicalError, mask_noncode


@dataclass(frozen=True)
class SandboxViolation:
    pattern: str
    reason: str
    line: int            # 1-indexed line in the original (un-stripped) block
    col: int             # 1-indexed column
    snippet: str         # the offending substring, with ~30 chars context


# Pattern table. Each entry: (regex, human-readable reason).
#
# `\b` anchors avoid matching inside identifiers like ``assume_something``
# *except* where the rule is to forbid the whole prefix (``assume_specification``
# uses an explicit \b boundary which still matches the start of the token).
_FORBIDDEN: List[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bassume\s*\("),
        "`assume(P)` is an axiom — Verus accepts P without proof. "
        "Use `assert(P)` (which Verus must verify) or `assert P by { ... }`.",
    ),
    (
        re.compile(r"\badmit\s*\(\s*\)"),
        "`admit()` discharges any pending obligation without proof.",
    ),
    (
        re.compile(r"\bunimplemented\s*!"),
        "`unimplemented!()` lets Verus skip the post; not a real proof.",
    ),
    (
        re.compile(r"\bunreachable\s*!"),
        "`unreachable!()` lets Verus assume the site is dead code; "
        "not a real proof unless you've discharged reachability first.",
    ),
    (
        re.compile(r"\bassume_specification\b"),
        "`assume_specification` declares an unverified external spec.",
    ),
    (
        re.compile(r"external_body"),
        "`#[verifier::external_body]` skips body verification; "
        "supplied proof blocks must not introduce this attribute.",
    ),
    (
        re.compile(r"#\s*\[\s*verifier\b[^]]*\bexternal\b"),
        "`#[verifier(external)]` / `#[verifier(external_body)]` skips checks.",
    ),
    # ----- structural / item-level forbids -----
    (
        re.compile(r"\bfn\s+[A-Za-z_]"),
        "new `fn` definitions are not allowed inside the proof block — "
        "emit statements only.",
    ),
    (
        re.compile(r"\b(?:spec|proof|exec|open\s+spec|closed\s+spec)\s+fn\b"),
        "new `spec fn` / `proof fn` definitions are out of scope — "
        "emit statements only.",
    ),
    (
        re.compile(r"\bimpl\b\s*[<\w]"),
        "`impl` items are not allowed inside a proof block.",
    ),
    (
        re.compile(r"\btrait\s+[A-Za-z_]"),
        "`trait` declarations are not allowed inside a proof block.",
    ),
    (
        re.compile(r"\bstruct\s+[A-Za-z_]"),
        "`struct` definitions are not allowed inside a proof block.",
    ),
    (
        re.compile(r"\benum\s+[A-Za-z_]"),
        "`enum` definitions are not allowed inside a proof block.",
    ),
    (
        re.compile(r"\btype\s+[A-Za-z_]"),
        "`type` definitions are not allowed inside a proof block.",
    ),
]


# ---------------------------------------------------------------------------
# Comment / string stripping.
# ---------------------------------------------------------------------------

def _mask(text: str) -> str:
    """Replace comments / strings with same-length spaces (preserving \\n).

    This way line/column offsets in violations point back into the
    original text exactly.
    """
    return mask_noncode(text)


def _line_col(text: str, pos: int) -> tuple[int, int]:
    """Convert an absolute char position into (1-indexed line, 1-indexed col)."""
    line = text.count("\n", 0, pos) + 1
    last_nl = text.rfind("\n", 0, pos)
    col = pos - last_nl if last_nl >= 0 else pos + 1
    return line, col


def _snippet(text: str, pos: int, width: int = 30) -> str:
    a = max(0, pos - width // 2)
    b = min(len(text), pos + width)
    s = text[a:b].replace("\n", " ⏎ ")
    if a > 0:
        s = "…" + s
    if b < len(text):
        s = s + "…"
    return s


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def scan_proof_block(block: str) -> List[SandboxViolation]:
    """Return all allowlist violations found in ``block``.

    Empty list means only that these lexical checks passed.
    """
    try:
        masked = _mask(block)
    except LexicalError as exc:
        line, col = _line_col(block, exc.offset)
        return [SandboxViolation("lexical_error", str(exc), line, col, _snippet(block, exc.offset))]
    violations: list[SandboxViolation] = []
    for pat, reason in _FORBIDDEN:
        for m in pat.finditer(masked):
            line, col = _line_col(block, m.start())
            violations.append(
                SandboxViolation(
                    pattern=pat.pattern,
                    reason=reason,
                    line=line,
                    col=col,
                    snippet=_snippet(block, m.start()),
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Pattern A: helper-lemma block scanner.
# ---------------------------------------------------------------------------
# The helper block is allowed to declare ``proof fn lemma_<name>(...)``
# at module scope, which the inline proof-block scanner above forbids
# by construction. Everything else stays forbidden — including:
#   * exec / spec fn declarations (Pattern A is about proofs only)
#   * struct / enum / trait / impl / type items
#   * assume / admit / external_body / assume_specification / unimplemented /
#     unreachable
#   * proof fn declarations whose name does NOT start with ``lemma_`` (an
#     guard against accidentally admitting arbitrary function declarations)
#
# The result: a permissive island for explicitly-named lemmas, with the
# same axiom-side guards as the main block.

# Helper-only allowlist: strip the "no proof fn" line from the main table.
_FORBIDDEN_HELPER_BASE: List[tuple[re.Pattern[str], str]] = [
    (pat, reason) for (pat, reason) in _FORBIDDEN
    if pat.pattern not in (
        # Allow `fn` only if it's a `proof fn lemma_*` (checked separately
        # below). Same for the spec/proof/exec catchall.
        r"\bfn\s+[A-Za-z_]",
        r"\b(?:spec|proof|exec|open\s+spec|closed\s+spec)\s+fn\b",
    )
]

# Anti-smuggling: any `fn` form that is NOT `proof fn lemma_*` is forbidden.
_HELPER_FN_OK_RE = re.compile(r"\bproof\s+fn\s+lemma_[A-Za-z0-9_]+\b")
# Catches every other introduction of a new fn/spec/exec.
_HELPER_FN_BAN_RE = re.compile(
    r"\b(?:open\s+|closed\s+)?(?:spec|exec)\s+fn\b"
    r"|\bfn\s+[A-Za-z_]"
)


def scan_helper_lemmas(block: str) -> List[SandboxViolation]:
    """Return allowlist violations for the helper-lemma block (Pattern A).

    Allowed:
      * any number of ``proof fn lemma_<name>(...) ...`` declarations
      * standard proof-mode bodies inside those lemmas (assert / forall /
        reveal / broadcast use / lemma calls)
    Forbidden (same as :func:`scan_proof_block`):
      * assume / admit / external_body / assume_specification / unimplemented /
        unreachable
      * struct / enum / trait / impl / type items
      * exec or spec fn declarations
      * proof fn declarations whose name doesn't start with ``lemma_``
    """
    try:
        masked = _mask(block)
    except LexicalError as exc:
        line, col = _line_col(block, exc.offset)
        return [SandboxViolation("lexical_error", str(exc), line, col, _snippet(block, exc.offset))]
    violations: list[SandboxViolation] = []
    for pat, reason in _FORBIDDEN_HELPER_BASE:
        for m in pat.finditer(masked):
            line, col = _line_col(block, m.start())
            violations.append(
                SandboxViolation(
                    pattern=pat.pattern,
                    reason=reason,
                    line=line,
                    col=col,
                    snippet=_snippet(block, m.start()),
                )
            )
    # Now scan for any disallowed fn introduction — but allow proof fn lemma_*.
    # We do this by looking for "ban" hits and excluding overlaps with "ok" hits.
    ok_spans: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in _HELPER_FN_OK_RE.finditer(masked)
    ]
    for m in _HELPER_FN_BAN_RE.finditer(masked):
        # If this hit is right after `proof ` (i.e. it's actually a `proof fn`
        # form that the "ok" rule already accepted), skip.
        if any(a <= m.start() < b for (a, b) in ok_spans):
            continue
        line, col = _line_col(block, m.start())
        violations.append(SandboxViolation(
            pattern=_HELPER_FN_BAN_RE.pattern,
            reason=(
                "only `proof fn lemma_<name>(...)` declarations are allowed in "
                "the helper-lemmas block; exec / spec fn (and proof fn whose "
                "name does not start with `lemma_`) are out of scope."
            ),
            line=line,
            col=col,
            snippet=_snippet(block, m.start()),
        ))
    return violations


def format_violations(vs: Iterable[SandboxViolation]) -> str:
    """Render violations as a multi-line diagnostic string."""
    out: list[str] = []
    for v in vs:
        out.append(
            f"  - line {v.line} col {v.col}: {v.reason}\n"
            f"    snippet: {v.snippet}"
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Self-test.
# ---------------------------------------------------------------------------

def _self_test() -> None:
    ok_block = """
    // safe proof: just asserts
    assert(x == y);
    assert forall|i: int| 0 <= i < n implies a[i] == b[i] by {
        // local reasoning
        assert(a[i] == b[i]);
    }
    reveal(spec_foo);
    broadcast use group_foo;
    lemma_seq_extensionality::<int>(s1, s2);
    """
    vs = scan_proof_block(ok_block)
    assert vs == [], f"safe block flagged: {vs}"

    bad_assume = "assert(x == y);\nassume(false);\n"
    vs = scan_proof_block(bad_assume)
    assert any("assume" in v.reason for v in vs), vs

    bad_attr = '#[verifier::external_body]\nproof { assert(true); }\n'
    vs = scan_proof_block(bad_attr)
    assert any("external" in v.reason for v in vs), vs

    bad_fn = "fn helper() -> int { 0 }\n"
    vs = scan_proof_block(bad_fn)
    assert any("fn" in v.pattern for v in vs), vs

    bad_admit = "admit();\n"
    vs = scan_proof_block(bad_admit)
    assert any("admit" in v.reason for v in vs), vs

    # Comments and strings must not trigger.
    commented = '// assume(false) — explanation\nassert(true);\n'
    vs = scan_proof_block(commented)
    assert vs == [], f"comment false-positive: {vs}"
    in_string = 'assert(msg == "assume(false)");\n'
    vs = scan_proof_block(in_string)
    assert vs == [], f"string literal false-positive: {vs}"

    # spec fn / proof fn forms.
    bad_spec_fn = "spec fn aux(x: int) -> int { x + 1 }\n"
    vs = scan_proof_block(bad_spec_fn)
    assert any("fn" in v.pattern for v in vs), vs

    # ----- helper-lemma scanner (Pattern A) -----
    ok_helpers = """
    proof fn lemma_sorted_unique(a: Seq<int>, i: int, j: int)
        requires sorted(a), 0 <= i < a.len(), 0 <= j < a.len(), a[i] == a[j]
        ensures i == j
    {
        // proof body
        assert(a[i] == a[j]);
    }

    proof fn lemma_seq_index_eq<T>(s: Seq<T>, t: Seq<T>)
        requires s =~= t
        ensures forall|k: int| 0 <= k < s.len() ==> s[k] == t[k]
    {}
    """
    vs = scan_helper_lemmas(ok_helpers)
    assert vs == [], f"safe helpers flagged: {vs}"

    # assume / admit still forbidden in helpers
    bad_h_assume = "proof fn lemma_x() { assume(false); }\n"
    vs = scan_helper_lemmas(bad_h_assume)
    assert any("assume" in v.reason for v in vs), vs

    # spec fn / exec fn forbidden in helpers
    bad_h_spec = "spec fn aux() -> int { 0 }\n"
    vs = scan_helper_lemmas(bad_h_spec)
    assert vs, "spec fn slipped past helper sandbox"

    bad_h_exec = "exec fn evil() { }\n"
    vs = scan_helper_lemmas(bad_h_exec)
    assert vs, "exec fn slipped past helper sandbox"

    # bare `fn` (non-proof) forbidden
    bad_h_bare = "fn helper() -> int { 0 }\n"
    vs = scan_helper_lemmas(bad_h_bare)
    assert vs, "bare fn slipped past helper sandbox"

    # proof fn must be named lemma_*
    bad_h_name = "proof fn helper_aux() {}\n"
    vs = scan_helper_lemmas(bad_h_name)
    assert vs, "non-lemma_* proof fn slipped past helper sandbox"

    # Multiple lemma_* helpers all good.
    multi = """
    proof fn lemma_a() {}
    proof fn lemma_b() {}
    proof fn lemma_c_with_args<T>(x: T) requires true ensures true {}
    """
    vs = scan_helper_lemmas(multi)
    assert vs == [], f"multi-lemma block flagged: {vs}"

    # struct / impl still forbidden in helpers
    bad_h_struct = "struct S {}\n"
    vs = scan_helper_lemmas(bad_h_struct)
    assert vs, "struct slipped past helper sandbox"

    print("sandbox self-test: PASS")


if __name__ == "__main__":
    _self_test()
