"""Single source of truth for Verus / Rust attribute parsing.

This module owns *all* per-item attribute inspection in the type-extraction
pipeline. Both function-level extraction (``extract/extractor.py``) and
project-level catalog building (``extract/type_registry.py``) consult it,
so that adding a new attribute means **one** edit instead of two.

Background — why this module exists
-----------------------------------

For most of the project's history, attribute checks were duplicated:

* ``extractor.py`` had ``_has_external_body_attr`` walking sibling
  ``attribute_item`` nodes for ``#[verifier(external_body)]`` /
  ``#[verifier::external_body]``;
* ``type_registry.py`` had ``_is_external_body`` doing the same thing on
  attribute *text* (already pre-extracted by ``_attr_text``);
* ``llm_type/apply.py`` had **no** attribute parsing at all — patches
  arrived from the LLM type-completion cache stripped of provenance.

When a second attribute (``#[verifier::ext_equal]``) was added, the
duplication bit us: the extractor side honoured it, the registry side
didn't, and the Tier 1.5 apply path silently dropped it during generic
instantiation (``apply._substitute``'s shallow copy). The symptom was
generated equal_fns that ignored the ext_equal short-circuit on
ironkv types like ``AckState<Message>``, undoing most of the codegen
savings.

This module collapses the two parallel parsers into one shared
``ItemAttrs`` payload + one shared parser. The contract is:

1. **Every flag that affects codegen lives in ``ItemAttrs``.**
   If the extractor cares, ``TypeInfo`` must mirror it. If the
   registry cares, ``TypeDef`` must mirror it. The mirror is
   one-line; the **parsing** is owned here.
2. **Every parsing entry point goes through ``parse_item_attrs``.**
   Both the AST-node and the attribute-text shapes are supported
   (legacy ``type_registry.py`` only has the text).
3. **The full project source can be scanned in one shot** via
   ``scan_source_for_item_attrs`` to build a name-keyed table.
   That table is what powers the Tier 1.5 post-pass: cached patches
   don't carry attribute provenance, so we re-derive it from source.

Adding a new attribute
----------------------

To add support for a new attribute (say, ``#[verifier::reject_recursive_types]``):

1. Add the field to ``ItemAttrs`` (default value reflecting "no
   annotation").
2. Add detection in ``parse_item_attrs`` / ``_classify_attribute_text``.
3. Add the field to ``TypeInfo`` (``extract/types.py``) with default,
   plus to_dict / from_dict.
4. (If the registry uses it) Add the field to ``TypeDef``
   (``extract/type_registry.py``).
5. ``apply._substitute`` and other reachable-walker users will pick it
   up automatically — they use ``dataclasses.replace`` so new fields
   propagate without code edits.

That's it. No more "edit 4 places and pray".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import tree_sitter as ts


# ---------------------------------------------------------------------------
# Attribute payload — every flag that affects codegen lives here.
# ---------------------------------------------------------------------------


@dataclass
class ItemAttrs:
    """Structured outcome of parsing a single struct / enum item's attribute
    sidecar (the ``attribute_item`` siblings inside a
    ``declaration_with_attrs`` node, or — for the registry path — the
    pre-rendered attribute text strings).

    Defaults reflect "no annotation"; merging two ``ItemAttrs`` (e.g. from
    multiple attribute_item nodes) uses ``logical-or`` for the booleans.
    """

    # ``#[verifier(external_body)]`` / ``#[verifier::external_body]``.
    # Verus treats the type's body as abstract — field expressions are
    # forbidden in spec contexts. Codegen short-circuits to ``lhs == rhs``.
    is_external_body: bool = False

    # ``#[verifier::external_type_specification]`` — the type is a spec
    # surrogate for an external Rust type. Mostly informational; the
    # registry uses it to skip declared-but-not-defined types.
    is_external_type_specification: bool = False

    # ``#[verifier::ext_equal]`` / ``#[verifier(ext_equal)]`` — opts the
    # type into extensional equality. ``=~=`` drills through
    # Seq/Set/Map/spec_fn fields and nested ext_equal types. Codegen
    # short-circuits STRUCT/ENUM to ``lhs =~= rhs``.
    #
    # Note: ``#[verifier::auto_ext_equal(N)]`` is a **different** attribute
    # (depth control for assert-position auto-promotion). We deliberately
    # do **not** set ``is_ext_equal`` for it, since spec fn bodies don't
    # get auto-promotion at all.
    is_ext_equal: bool = False

    # The cfg feature predicate (``#[cfg(feature = "X")]``). ``None`` means
    # the item is always enabled. Anything more complex than a single
    # ``feature = "..."`` predicate currently parses to ``None`` and the
    # caller treats it conservatively (always include).
    cfg_feature_req: Optional[str] = None

    # Names listed in ``#[derive(...)]`` clauses. Used by the registry to
    # tell e.g. "this struct derives PartialEq".
    derives: list[str] = field(default_factory=list)


def _merge(into: ItemAttrs, other: ItemAttrs) -> None:
    """Logical-or merge ``other`` into ``into`` (mutates ``into``)."""
    if other.is_external_body:
        into.is_external_body = True
    if other.is_external_type_specification:
        into.is_external_type_specification = True
    if other.is_ext_equal:
        into.is_ext_equal = True
    if other.cfg_feature_req is not None:
        # First non-None wins; multiple cfg(feature = ...) on the same
        # item is rare and we conservatively keep the first.
        if into.cfg_feature_req is None:
            into.cfg_feature_req = other.cfg_feature_req
    if other.derives:
        into.derives.extend(other.derives)


# ---------------------------------------------------------------------------
# Text-level classification (no tree-sitter needed)
# ---------------------------------------------------------------------------


_DERIVE_RE = re.compile(r"derive\s*\(\s*([^)]+)\)")
_CFG_FEATURE_RE = re.compile(
    r"""cfg\s*\(\s*feature\s*=\s*"([^"]+)"\s*\)""", re.VERBOSE
)


def _classify_attribute_text(text: str) -> ItemAttrs:
    """Classify a single attribute's source text into an ``ItemAttrs``.

    ``text`` is the verbatim attribute text including the ``#[...]`` outer
    brackets (the form ``_attr_text`` in ``type_registry.py`` produces, and
    the form ``_text(attr_node)`` produces in the extractor).
    """
    out = ItemAttrs()

    # external_body — accept both #[verifier(external_body)] and
    # #[verifier::external_body], including the verifier-paren variant.
    if "verifier" in text and "external_body" in text:
        out.is_external_body = True

    # external_type_specification — same dual-form acceptance.
    if "verifier" in text and "external_type_specification" in text:
        out.is_external_type_specification = True

    # ext_equal — must reject the unrelated auto_ext_equal(N) variant,
    # which is a different attribute that only controls assert-position
    # auto-promotion depth.
    if ("verifier" in text and "ext_equal" in text
            and "auto_ext_equal" not in text):
        out.is_ext_equal = True

    # cfg(feature = "X")
    m = _CFG_FEATURE_RE.search(text)
    if m is not None:
        out.cfg_feature_req = m.group(1)

    # derive(...)
    m = _DERIVE_RE.search(text)
    if m is not None:
        names = [n.strip() for n in m.group(1).split(",") if n.strip()]
        out.derives.extend(names)

    return out


# ---------------------------------------------------------------------------
# AST-level entry point
# ---------------------------------------------------------------------------


def _node_text(node: ts.Node) -> str:
    """Recover the source text covered by ``node`` (utf-8 decode of the
    byte slice tree-sitter holds onto).
    """
    return bytes(node.text).decode("utf-8", errors="replace")


def parse_item_attrs(attr_nodes: list[ts.Node]) -> ItemAttrs:
    """Parse a list of ``attribute_item`` tree-sitter nodes into a merged
    :class:`ItemAttrs`.

    The order doesn't matter (the merge is logical-or-on-booleans). Empty
    list returns the all-defaults ``ItemAttrs``.
    """
    out = ItemAttrs()
    for n in attr_nodes:
        text = _node_text(n)
        _merge(out, _classify_attribute_text(text))
    return out


def parse_item_attrs_from_texts(texts: list[str]) -> ItemAttrs:
    """Same as :func:`parse_item_attrs` but takes already-rendered attribute
    texts — used by the registry path which extracts text via
    ``_attr_text`` before classification.
    """
    out = ItemAttrs()
    for t in texts:
        _merge(out, _classify_attribute_text(t))
    return out


# ---------------------------------------------------------------------------
# Project-wide scan: build a name → ItemAttrs map from source
# ---------------------------------------------------------------------------


# Regex to spot ``pub struct Name`` / ``pub enum Name`` / ``struct Name`` / etc.
# We're deliberately permissive: any whitespace-delimited ``struct`` /
# ``enum`` keyword followed by an identifier on (or near) the same logical
# line counts. We don't need to handle generic params here because the
# caller looks up by *bare* name; the regex stops at the first non-
# identifier character.
_TYPE_DECL_RE = re.compile(
    r"""
    (?:pub(?:\s*\(\s*[^)]*\s*\))?\s+)?   # optional pub or pub(...)
    (?P<kw>struct|enum|union|type)\s+    # item keyword
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)     # bare name
    """,
    re.VERBOSE,
)

# Regex to find an attribute line (``#[ ... ]``). We do greedy matching on
# the bracket body since attributes can contain nested parens but not
# nested brackets.
_ATTR_RE = re.compile(r"""\#\[(?P<body>[^\[\]]*)\]""")


def scan_source_for_item_attrs(source: str) -> dict[str, ItemAttrs]:
    """Scan a concatenated source string for every ``struct`` / ``enum`` /
    ``union`` / ``type`` declaration, look back at its preceding
    consecutive attribute lines, and return ``{name: ItemAttrs}``.

    This is the "single-pass attribute table" used by the Tier 1.5
    post-pass: cached LLM type-completion patches don't carry attribute
    provenance, so we re-derive it directly from source. The result is
    folded onto every reachable ``TypeInfo`` whose bare name matches.

    Notes on the approach:

    * The function is **text-based**, not AST-based. tree-sitter would be
      more accurate but slower over the whole project (ironkv ~ 2.6 MB
      of source) and we only need to recognize the few well-known Verus
      attributes. The regex misses corner cases like macro-expanded
      structs or attributes attached via cfg_attr; those are rare
      enough that we accept the false negatives.
    * If a bare name appears more than once in the source (e.g. behind
      different ``#[cfg(...)]`` predicates) the *merged* attrs win.
      That matches how Verus itself treats the conditional definitions
      — only the active branch is compiled, and they share annotation
      shape in practice.
    """
    out: dict[str, ItemAttrs] = {}

    lines = source.split("\n")
    pending_attrs: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            # Blank line — attributes are still allowed to apply across
            # blank lines in Rust (they bind to the next item), so we
            # don't reset pending here.
            continue
        # Comment lines never reset pending_attrs either.
        if stripped.startswith("//"):
            continue
        # Attribute line: collect its text.
        if stripped.startswith("#[") or stripped.startswith("#!["):
            # Single-line attribute. (Multi-line attributes are rare in
            # this codebase; if encountered they'd be best handled via
            # tree-sitter — but we accept the miss.)
            for m in _ATTR_RE.finditer(stripped):
                pending_attrs.append("#[" + m.group("body") + "]")
            continue
        # Item-keyword line: associate pending attrs with the name.
        m = _TYPE_DECL_RE.match(stripped)
        if m is not None:
            name = m.group("name")
            attrs = parse_item_attrs_from_texts(pending_attrs)
            if name in out:
                _merge(out[name], attrs)
            else:
                out[name] = attrs
            pending_attrs = []
            continue
        # Anything else (impl block, fn, mod, use, ...) detaches attrs.
        # The attrs were meant for that other item, not for a future
        # struct/enum. Drop them.
        pending_attrs = []

    return out


# ---------------------------------------------------------------------------
# Whole-tree / whole-source attribute table builders
# ---------------------------------------------------------------------------


def collect_item_attrs_from_tree(tree: ts.Tree) -> dict[str, ItemAttrs]:
    """Walk a tree-sitter parse tree and return ``{bare_name: ItemAttrs}``
    for every ``struct_item`` / ``enum_item`` wrapped in a
    ``declaration_with_attrs`` node.

    Used by the extractor where we already have parsed trees and want
    maximum precision (tree-sitter sees attributes that the regex
    scanner might miss, e.g. multi-line ``cfg_attr`` constructions).

    The runner / registry paths that don't have parsed trees use
    :func:`scan_source_for_item_attrs` instead.
    """
    out: dict[str, ItemAttrs] = {}

    def walk(node: ts.Node, parent: Optional[ts.Node] = None) -> None:
        if node.type in ("struct_item", "enum_item"):
            # find the name
            name_node = None
            for c in node.children:
                if c.type == "type_identifier":
                    name_node = c
                    break
            if name_node is not None and parent is not None \
                    and parent.type == "declaration_with_attrs":
                attr_nodes: list[ts.Node] = []
                for c in parent.children:
                    if c is node:
                        break
                    if c.type == "attribute_item":
                        attr_nodes.append(c)
                attrs = parse_item_attrs(attr_nodes)
                name = _node_text(name_node)
                if name in out:
                    _merge(out[name], attrs)
                else:
                    out[name] = attrs
        for child in node.children:
            walk(child, node)

    walk(tree.root_node)
    return out


# ---------------------------------------------------------------------------
# Generic TypeInfo / type_defs propagation
# ---------------------------------------------------------------------------


def propagate_attrs_to_type_defs(
    attrs_table: dict[str, ItemAttrs],
    *,
    type_defs,
    params=None,
    return_type=None,
) -> None:
    """Tag every reachable :class:`specdet.adapters.verus.native.extract.types.TypeInfo`
    whose bare name has a non-default :class:`ItemAttrs` entry in
    ``attrs_table``.

    Walks ``type_defs.values()`` plus ``params[*].type`` plus ``return_type``
    recursively through ``type_args`` / ``fields`` / ``variants`` /
    ``spec_view``. Uses OR-merge semantics: flags on the TypeInfo are only
    set, never cleared. This is what handles generic instantiation
    copies (``AckState<Message>`` shallow-copied from
    ``type_defs["AckState"]``) that ``apply._substitute`` produces.

    Centralising the walker here is the whole point of attrs.py — both
    the extractor's resolve-types post-pass and the Tier 1.5 runner's
    post-pass call this single function so the propagation rule is
    impossible to drift.
    """

    def visit(ti) -> None:
        if ti is None:
            return
        # Bare-name lookup: drop the generic-args suffix.
        bare = ti.name.split("<", 1)[0] if "<" in ti.name else ti.name
        attrs = attrs_table.get(bare)
        if attrs is not None:
            if attrs.is_external_body and not ti.is_opaque:
                ti.is_opaque = True
            if attrs.is_ext_equal and not ti.is_ext_equal:
                ti.is_ext_equal = True
        for ta in ti.type_args:
            visit(ta)
        for f in ti.fields:
            visit(f.type)
        for v in ti.variants:
            if v.inner is not None:
                visit(v.inner)
        if ti.spec_view is not None:
            visit(ti.spec_view)

    for td in type_defs.values():
        visit(td)
    if params is not None:
        for p in params:
            visit(p.type)
    if return_type is not None:
        visit(return_type)


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------


def _self_test() -> bool:
    ok = True

    # Text classification
    cases = [
        ("#[verifier::external_body]",
         dict(is_external_body=True)),
        ("#[verifier(external_body)]",
         dict(is_external_body=True)),
        ("#[verifier::ext_equal]",
         dict(is_ext_equal=True)),
        ("#[verifier(ext_equal)]",
         dict(is_ext_equal=True)),
        ("#[verifier::auto_ext_equal(10)]",  # NOT ext_equal
         dict()),
        ("#[verifier::external_type_specification]",
         dict(is_external_type_specification=True)),
        ('#[cfg(feature = "myfeat")]',
         dict(cfg_feature_req="myfeat")),
        ("#[derive(Clone, Debug, PartialEq)]",
         dict(derives=["Clone", "Debug", "PartialEq"])),
        ("#[doc = \"hello\"]",
         dict()),  # unrelated
    ]
    for text, expected in cases:
        got = _classify_attribute_text(text)
        for k, v in expected.items():
            actual = getattr(got, k)
            if actual != v:
                print(f"FAIL: {text!r} -> {k}={actual!r}, want {v!r}")
                ok = False

    # Source-scan
    src = """
verus!{

#[verifier::ext_equal]
pub struct A {
    x: u64,
}

#[verifier(external_body)]
pub struct B {
    p: *const u8,
}

#[verifier::auto_ext_equal(5)]
pub struct C { y: u64 }

#[derive(Clone)]
#[cfg(feature = "fancy")]
pub enum D {
    X,
    Y(u8),
}

}
"""
    table = scan_source_for_item_attrs(src)
    expectations = {
        "A": dict(is_ext_equal=True, is_external_body=False),
        "B": dict(is_external_body=True, is_ext_equal=False),
        "C": dict(is_ext_equal=False, is_external_body=False),  # auto != regular
        "D": dict(derives=["Clone"], cfg_feature_req="fancy"),
    }
    for name, exp in expectations.items():
        if name not in table:
            print(f"FAIL: {name!r} missing from scan_source_for_item_attrs output")
            ok = False
            continue
        attrs = table[name]
        for k, v in exp.items():
            actual = getattr(attrs, k)
            if actual != v:
                print(f"FAIL: scan[{name!r}].{k}={actual!r}, want {v!r}")
                ok = False

    # Whole-tree builder + propagator integration
    try:
        import tree_sitter_verus as tsv
        lang = ts.Language(tsv.language())
        parser = ts.Parser(lang)
        tree = parser.parse(src.encode())
        tree_table = collect_item_attrs_from_tree(tree)
        for name in ("A", "B"):
            if name not in tree_table:
                print(f"FAIL: tree builder missed {name!r}")
                ok = False
        if "A" in tree_table and not tree_table["A"].is_ext_equal:
            print("FAIL: tree builder lost is_ext_equal on A")
            ok = False
        if "B" in tree_table and not tree_table["B"].is_external_body:
            print("FAIL: tree builder lost is_external_body on B")
            ok = False
    except ImportError:
        # tree-sitter-verus not installed in this env — skip the tree-walker
        # part of the self-test (the regex scanner is the safety net).
        pass

    # Propagator: build a fake TypeInfo tree and verify flags get OR-merged
    from .types import TypeInfo, TypeKind, FieldInfo, VariantInfo
    inner = TypeInfo(kind=TypeKind.STRUCT, name="A", fields=[])
    outer = TypeInfo(
        kind=TypeKind.STRUCT, name="Wrap",
        fields=[FieldInfo(name="a", type=inner)],
    )
    type_defs = {"Wrap": outer, "A": inner}
    fake_table = {"A": ItemAttrs(is_ext_equal=True)}
    propagate_attrs_to_type_defs(fake_table, type_defs=type_defs)
    if not inner.is_ext_equal:
        print("FAIL: propagator didn't tag A.is_ext_equal")
        ok = False
    # Verify generic-instantiation case: a shallow copy of A under a
    # different name still picks up the flag when the bare name matches.
    instantiation = TypeInfo(kind=TypeKind.STRUCT, name="A<Message>", fields=[])
    container = TypeInfo(
        kind=TypeKind.STRUCT, name="Outer",
        fields=[FieldInfo(name="x", type=instantiation)],
    )
    propagate_attrs_to_type_defs(
        {"A": ItemAttrs(is_ext_equal=True)}, type_defs={"Outer": container},
    )
    if not instantiation.is_ext_equal:
        print("FAIL: propagator missed generic instantiation A<Message>")
        ok = False

    print("attrs self-test:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)
