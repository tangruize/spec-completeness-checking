"""
Module 2: gen_det — Determinism Check Generator

Merged with extract into Step 1 of the pipeline.
Produces a DetCheckSpec (template + symbol table) that Step 2 consumes.
"""

import logging
import re
from copy import deepcopy
from typing import TYPE_CHECKING, Optional

import tree_sitter as ts
import tree_sitter_verus as tsv

from specdet.adapters.verus.native.extract.types import (
    TypeKind, TypeInfo, Param, FunctionSpec, Assume,
    Symbol, DetCheckSpec,
)
from .equal_policy import EqualPolicy, default_policy
from .expressions import StateNames, Unsupported, free_identifiers, rename_free_identifiers
from .pairing import InputPair, validate_input_pairs

if TYPE_CHECKING:
    from specdet.adapters.verus.native.view.registry import ViewRegistry

logger = logging.getLogger(__name__)

_lang = ts.Language(tsv.language())
_parser = ts.Parser(_lang)


# ---------------------------------------------------------------------------
# TypeInfo → TypeExpr bridge for the L1+L2+L3 view resolver
# ---------------------------------------------------------------------------

# TypeKind → TypeExpr.kind map for primitive / unit so the resolver picks
# the identity-view path. For composite kinds we hand-craft TypeExpr below.
_PRIMITIVE_KINDS = {
    TypeKind.INT, TypeKind.USIZE, TypeKind.ISIZE,
    TypeKind.U8, TypeKind.U16, TypeKind.U32, TypeKind.U64,
    TypeKind.I8, TypeKind.I16, TypeKind.I32, TypeKind.I64,
    TypeKind.BOOL, TypeKind.STR,
}


def _typeinfo_to_typeexpr(ty: "TypeInfo"):
    """Convert a ``TypeInfo`` (gen_det's runtime type model) into a
    ``TypeExpr`` (type_registry's symbolic tree) so the
    :class:`ViewRegistry` can resolve it.

    The conversion is lossy on the head-name side (Verus's
    ``Vec<u32>`` and ``Vec<Foo>`` collapse to head ``"Vec"`` in the
    registry's short-name index), but that's exactly what the
    resolver wants — prelude rules fire on head name regardless of
    instantiation.
    """
    from specdet.adapters.verus.native.extract.type_registry import TypeExpr

    if ty.kind in _PRIMITIVE_KINDS:
        return TypeExpr(kind="primitive", head=ty.name or "u32",
                        raw=ty.name or "u32")
    if ty.kind == TypeKind.UNIT:
        return TypeExpr(kind="unit", raw="()")
    if ty.kind in (TypeKind.SEQ, TypeKind.SET):
        head = "Seq" if ty.kind == TypeKind.SEQ else "Set"
        args = [_typeinfo_to_typeexpr(a) for a in (ty.type_args or [])]
        return TypeExpr(kind="generic" if args else "leaf",
                        head=head, args=args, raw=ty.name or head)
    if ty.kind == TypeKind.OPTION:
        args = [_typeinfo_to_typeexpr(a) for a in (ty.type_args or [])]
        return TypeExpr(kind="generic" if args else "leaf",
                        head="Option", args=args, raw=ty.name or "Option")
    if ty.kind == TypeKind.RESULT:
        args = [_typeinfo_to_typeexpr(a) for a in (ty.type_args or [])]
        return TypeExpr(kind="generic" if args else "leaf",
                        head="Result", args=args, raw=ty.name or "Result")
    # Struct / Enum / Unknown — use whatever name we have
    head = ty.name or "?"
    args = [_typeinfo_to_typeexpr(a) for a in (ty.type_args or [])]
    # Extractor sometimes stores the *raw* spelling in ``ty.name``
    # (e.g. ``"StrictlyOrderedMap<K>"``) — sometimes with a populated
    # ``type_args=[K]`` (LLM-completed types), sometimes with empty
    # ``type_args`` (path-resolver fallback in extractor.py). The
    # ``ViewRegistry`` indexes types by their *short* name, so the raw
    # spelling causes a miss and the resolver silently falls through
    # to field-by-field structural equality, defeating the view layer.
    # Strip the generic suffix unconditionally so the short-name lookup
    # matches in both cases.
    if "<" in head:
        head = head.split("<", 1)[0].strip()
    if "::" in head:
        head = head.rsplit("::", 1)[-1]
    return TypeExpr(kind="generic" if args else "leaf",
                    head=head, args=args, raw=ty.name or "")


def _var_name(param: Param, prefix: str = "") -> str:
    name = "self_" if param.is_self else param.name
    return f"{prefix}{name}" if prefix else name


# Raw-pointer detection: recognised by TypeInfo.name prefix because tree-sitter
# parses `*mut T` / `*const T` as pointer_type nodes and the extractor records
# the full source text as `name`.
_RAW_POINTER_PREFIXES = ("*mut ", "*const ", "*mut\t", "*const\t")


def _is_raw_pointer_type(ty: TypeInfo) -> bool:
    """Return True iff `ty` is a raw-pointer type (`*mut T` / `*const T`).

    Raw pointers are matched only on syntactic form because the extractor
    classifies them as TypeKind.UNKNOWN (they have no interesting spec
    structure) — all we have to key off is the original source text.
    """
    name = (ty.name or "").lstrip()
    return any(name.startswith(p) for p in _RAW_POINTER_PREFIXES)


def _is_points_to_raw_type(ty: TypeInfo) -> bool:
    """Return True only when the outer type itself is `PointsToRaw`.

    A substring check is unsound here: tuple spellings such as
    `(*mut u8, Tracked<PointsToRaw>, Tracked<Dealloc>)` also contain the
    text `PointsToRaw`, but the tuple's other observable components must
    still participate in the generated equality.
    """
    name = (ty.name or "").strip()
    head = name.split("<", 1)[0].strip().rsplit("::", 1)[-1]
    return head == "PointsToRaw"


def _is_pcell_points_to_type(ty: TypeInfo) -> bool:
    name = (ty.name or "").replace(" ", "")
    return "pcell::PointsTo<" in name or "cell::pcell::PointsTo<" in name


def _type_name(param: Param) -> str:
    return param.type.name


def _is_unsized_ty(ty: str) -> bool:
    """Heuristic: does this type need to stay behind `&` to be Sized?

    Slice `[T]`, str, and `dyn Trait` are the common unsized forms in
    Verus corpora. Fixed-size arrays `[T; N]` are Sized. Anything else
    (structs, enums, raw pointers, primitives) is Sized.
    """
    t = ty.strip()
    if t == "str":
        return True
    if t.startswith("dyn "):
        return True
    if t.startswith("[") and t.endswith("]") and ";" not in t:
        return True
    return False


# ---------------------------------------------------------------------------
# Phantom-generic pruning
# ---------------------------------------------------------------------------

def _ts_find_function_item(node: ts.Node) -> Optional[ts.Node]:
    """Locate the synthesized `function_item` inside a probe-fn parse tree."""
    if node.type == "function_item":
        return node
    for c in node.children:
        out = _ts_find_function_item(c)
        if out is not None:
            return out
    return None


def _ts_child_by_type(node: ts.Node, *types: str) -> Optional[ts.Node]:
    for c in node.children:
        if c.type in types:
            return c
    return None


def _ts_collect_referenced(node: ts.Node, known: set[str]) -> set[str]:
    """Walk ``node`` collecting every leaf identifier / lifetime / type-id
    whose text intersects ``known``. Skips ``type_parameters`` subtrees so
    HRTB / nested-fn binders don't count as outer-scope references.
    """
    out: set[str] = set()

    def visit(n: ts.Node) -> None:
        # type_parameters introduces a fresh binder list (HRTB ``for<'a>`` or
        # nested-fn `fn<'a>`); names inside are local, skip the whole subtree.
        if n.type == "type_parameters":
            return
        if n.type in ("type_identifier", "lifetime", "identifier", "primitive_type"):
            t = n.text.decode()
            if t in known:
                out.add(t)
        for c in n.children:
            visit(c)

    visit(node)
    return out


def _collect_referenced_text(text: str, known: set[str]) -> set[str]:
    """Text fallback for tiny snippets tree-sitter cannot parse well.

    This is intentionally conservative and is used only as an auxiliary source
    for generic-bound dependencies in `_prune_generics`.
    """
    out: set[str] = set()
    for name in known:
        if name.startswith("'"):
            pattern = re.escape(name) + r"\b"
        else:
            pattern = r"\b" + re.escape(name) + r"\b"
        if re.search(pattern, text):
            out.add(name)
    return out


def _generic_call_turbofish(generics_decl: str) -> str:
    """Render `::<...>` call arguments from a function generic declaration."""
    if not generics_decl.strip():
        return ""
    probe_src = f"fn __probe{generics_decl}() {{}}"
    tree = _parser.parse(probe_src.encode())
    fn_node = _ts_find_function_item(tree.root_node)
    if fn_node is None:
        return ""
    tp_node = _ts_child_by_type(fn_node, "type_parameters")
    if tp_node is None:
        return ""
    args: list[str] = []
    for child in tp_node.children:
        if child.type == "lifetime_parameter":
            # Function lifetimes are inferred at the call site; explicitly
            # passing them can fail for late-bound lifetimes (E0794).
            continue
        elif child.type == "type_parameter":
            ti = _ts_child_by_type(child, "type_identifier")
            if ti is not None:
                args.append(ti.text.decode())
        elif child.type == "const_parameter":
            raw = child.text.decode()
            m = re.match(r"\s*const\s+([A-Za-z_][A-Za-z0-9_]*)", raw)
            if m is not None:
                args.append(m.group(1))
    return f"::<{', '.join(args)}>" if args else ""


def _augment_generics_from_self_type(generics_decl: str, self_type: str | None) -> str:
    """Add missing bare type parameters that appear in `SelfType<...>`.

    Some source-native fallback extraction paths recover only the impl target
    text (e.g. `HashMap<V>`) and lose the enclosing `impl<V>` generic list.
    Without `V` in the synthesized det/equal function signatures, Verus emits
    `cannot find type V`.  Add simple identifier generic args back; bounds are
    intentionally not guessed.
    """
    if not self_type or "<" not in self_type:
        return generics_decl
    m = re.match(r"^[A-Za-z_][A-Za-z0-9_:]*\s*<(.+)>\s*$", self_type.strip())
    if m is None:
        return generics_decl
    existing = set(_parse_generic_bounds(generics_decl).keys())
    additions: list[str] = []
    for arg in _split_top_level_commas_text(m.group(1)):
        arg = arg.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", arg):
            continue
        if arg not in existing and arg not in additions:
            additions.append(arg)
    if not additions:
        return generics_decl
    text = generics_decl.strip()
    if text.startswith("<") and text.endswith(">"):
        inner = text[1:-1].strip()
        return f"<{inner}, {', '.join(additions)}>" if inner else f"<{', '.join(additions)}>"
    return f"<{', '.join(additions)}>"


def _prune_generics(
    generics_decl: str,
    where_decl: str,
    sig_params_text: str,
    return_type_text: str = "",
) -> tuple[str, str]:
    """Drop generic parameters not referenced by the synthesized fn signature,
    and drop any where-predicate that refers only to dropped generics.

    Both inputs may be empty strings. Renders back to ``<...>`` / ``where ...``
    text. Conservative on parse failures: returns inputs unchanged.

    Closure rule: if a where-predicate references one kept generic and one
    pruned generic, the predicate is kept *and* the pruned generic is pulled
    back in (otherwise we'd reference an undeclared name).
    """
    if not generics_decl.strip():
        return generics_decl, where_decl

    # Build a probe fn so tree-sitter parses generics + where in proper context.
    params = sig_params_text.strip()
    if params.startswith("(") and params.endswith(")"):
        params = params[1:-1]
    rt = return_type_text.strip() or "()"
    where_part = f" {where_decl.strip()}" if where_decl.strip() else ""
    probe_src = f"fn __probe{generics_decl}({params}) -> {rt}{where_part} {{}}"

    tree = _parser.parse(probe_src.encode())
    fn_node = _ts_find_function_item(tree.root_node)
    if fn_node is None:
        return generics_decl, where_decl

    tp_node = _ts_child_by_type(fn_node, "type_parameters")
    if tp_node is None:
        return "", where_decl  # nothing declared, drop where if any

    # 1. Enumerate generic params: (kind, name, raw)
    entries: list[tuple[str, str, str]] = []
    known: set[str] = set()
    for child in tp_node.children:
        if child.type == "lifetime_parameter":
            # Node text is e.g. "'a" or "'a: 'static"
            name = child.text.decode().split(":", 1)[0].strip()
            entries.append(("lifetime", name, child.text.decode()))
            known.add(name)
        elif child.type == "type_parameter":
            ti = _ts_child_by_type(child, "type_identifier")
            if ti is None:
                # Unparseable; keep verbatim with a sentinel name.
                entries.append(("unknown", "", child.text.decode()))
                continue
            name = ti.text.decode()
            entries.append(("type", name, child.text.decode()))
            known.add(name)
        elif child.type == "const_parameter":
            ident = None
            for c in child.children:
                if c.type == "identifier":
                    ident = c
                    break
            if ident is None:
                entries.append(("unknown", "", child.text.decode()))
                continue
            name = ident.text.decode()
            entries.append(("const", name, child.text.decode()))
            known.add(name)
        # Ignore punctuation children (`<`, `>`, `,`).

    # 2. Names referenced in the synthesized signature.
    used: set[str] = set()
    # Callers concatenate the rendered run1/run2 ensures text into
    # ``sig_params_text`` so generics used only inside spec expressions stay.
    # That text is not a valid parameter list when the ensures carries inner
    # attributes (``#![trigger ...]``) or multi-line quantifiers, and the probe
    # then parses to an ERROR tree with an empty ``parameters`` node — which
    # used to silently drop *every* generic and emit an unbound ``T`` / ``'a``.
    # Fall back to the textual scan in that case (strictly conservative: it can
    # only keep more generics).
    probe_parse_ok = not tree.root_node.has_error
    params_node = _ts_child_by_type(fn_node, "parameters")
    if params_node is not None:
        used |= _ts_collect_referenced(params_node, known)
    # Return type — tree-sitter-verus uses `named_return_type` (verus form)
    # or a plain type node after `->`. Check both.
    rt_node = _ts_child_by_type(fn_node, "named_return_type")
    if rt_node is None:
        # Fall back: any sibling between `->` and the block that is a type-ish.
        seen_arrow = False
        for c in fn_node.children:
            if c.type == "->":
                seen_arrow = True
                continue
            if seen_arrow and c.type not in ("block", "where_clause"):
                used |= _ts_collect_referenced(c, known)
                break
    else:
        used |= _ts_collect_referenced(rt_node, known)

    if not probe_parse_ok:
        used |= _collect_referenced_text(
            sig_params_text + " " + return_type_text, known
        )

    # 3. Where predicates: parse each, pre-compute referenced name sets.
    wc_node = _ts_child_by_type(fn_node, "where_clause")
    wp_list: list[tuple[str, set[str]]] = []
    if wc_node is not None:
        for c in wc_node.children:
            if c.type == "where_predicate":
                refs = _ts_collect_referenced(c, known)
                wp_list.append((c.text.decode(), refs))

    # Generic-parameter bounds can also reference other generics, e.g.
    # `<I, __DetSelf: VestPublicOutput<I>>`. If `__DetSelf` is kept because it
    # appears in the signature, `I` must be kept too even if it appears only in
    # that bound. Tree-sitter does not model these as where-predicates, so add a
    # second closure source from each kept generic parameter's own bound text.
    generic_bound_refs: list[tuple[str, set[str]]] = [
        (
            name,
            _ts_collect_referenced(_parser.parse(raw.encode()).root_node, known)
            | _collect_referenced_text(raw, known),
        )
        for (_kind, name, raw) in entries
        if name
    ]

    # 4. Closure: predicate/bound that overlaps kept ⇒ keep predicate AND pull
    #    in its other referenced names so we don't end up referencing an
    #    undeclared generic.
    kept = set(used)
    changed = True
    while changed:
        changed = False
        for name, refs in generic_bound_refs:
            if name in kept and not refs.issubset(kept):
                kept |= refs
                changed = True
        for raw, refs in wp_list:
            if refs & kept and not refs.issubset(kept):
                kept |= refs
                changed = True

    # 5. Filter and render.
    kept_entries = [(k, n, r) for (k, n, r) in entries
                    if k == "unknown" or n in kept]
    new_generics = (
        "<" + ", ".join(r for (_, _, r) in kept_entries) + ">"
        if kept_entries else ""
    )

    kept_preds_raw: list[str] = []
    for raw, refs in wp_list:
        # Predicates that mention no known generic are anomalous (e.g., bound
        # on an associated type binding); keep verbatim.
        if not refs:
            kept_preds_raw.append(raw)
        elif refs & kept:
            kept_preds_raw.append(raw)
    new_where = ("where " + ", ".join(kept_preds_raw)) if kept_preds_raw else ""

    return new_generics, new_where


# ---------------------------------------------------------------------------
# Symbol table construction
# ---------------------------------------------------------------------------

def _classify_phase(ty: TypeInfo) -> str:
    """Classify a type as simple or compound for output ordering."""
    if ty.kind in (TypeKind.RESULT, TypeKind.OPTION, TypeKind.ENUM,
                   TypeKind.BOOL, TypeKind.UNIT,
                   TypeKind.INT, TypeKind.USIZE, TypeKind.ISIZE,
                   TypeKind.U8, TypeKind.U16, TypeKind.U32, TypeKind.U64,
                   TypeKind.I8, TypeKind.I16, TypeKind.I32, TypeKind.I64):
        return "output_simple"
    return "output_compound"


def _build_symbols(
    spec: FunctionSpec, *, input_pairs: tuple[InputPair, ...] = (),
) -> list[Symbol]:
    """Build the symbol table: variables to narrow, in order."""
    symbols = []

    # Phase 1: Input variables
    pairs = {pair.parameter: pair for pair in input_pairs}
    for p in spec.params:
        pair = pairs.get(p.name)
        names = (
            (pair.left, pair.right) if pair is not None
            else (f"pre_{_var_name(p)}" if p.is_mut_ref else _var_name(p),)
        )
        for name in names:
            symbols.append(Symbol(name=name, type=p.type, phase="input"))

    # Phase 2: Output variables — simple first, then compound
    simple_outputs = []
    compound_outputs = []

    # Return value (r1, r2)
    ret_phase = _classify_phase(spec.return_type)
    if ret_phase == "output_simple":
        simple_outputs.append(("r1", spec.return_type))
        simple_outputs.append(("r2", spec.return_type))
    else:
        compound_outputs.append(("r1", spec.return_type))
        compound_outputs.append(("r2", spec.return_type))

    # Post-state of &mut params (post1_*, post2_*)
    for p in spec.params:
        if p.is_mut_ref:
            base = _var_name(p)
            phase = _classify_phase(p.type)
            pair = [
                (f"post1_{base}", p.type),
                (f"post2_{base}", p.type),
            ]
            if phase == "output_simple":
                simple_outputs.extend(pair)
            else:
                compound_outputs.extend(pair)

    for name, ty in simple_outputs:
        symbols.append(Symbol(name=name, type=ty, phase="output_simple"))
    for name, ty in compound_outputs:
        symbols.append(Symbol(name=name, type=ty, phase="output_compound"))

    return symbols


# ---------------------------------------------------------------------------
# Template generation
# ---------------------------------------------------------------------------

def _default_check_name(function: str) -> str:
    return "det_" + re.sub(r"[^A-Za-z0-9_]", "_", function)


def _build_template(
    spec: FunctionSpec,
    check_name: str | None = None,
    policy: EqualPolicy | None = None,
    view_registry: Optional["ViewRegistry"] = None,
    reveal_specs: Optional[list[str]] = None,
    *,
    input_pairs: tuple[InputPair, ...] = (),
) -> tuple[str, str, str, list[tuple[str, str]]]:
    """
    Generate the det check proof fn with {ASSUMES} placeholder.
    
    The template has a single {ASSUMES} marker where binary search
    will insert accumulated assume expressions.
    """
    fn_name = check_name or _default_check_name(spec.name)
    validate_input_pairs(spec, input_pairs, fn_name)
    for clause in (*spec.requires, *spec.ensures):
        free_identifiers(clause)
        if "{ASSUMES}" in clause:
            raise Unsupported("A source clause contains the native template marker")
    pairs = {pair.parameter: pair for pair in input_pairs}
    left_bindings = {pair.parameter: pair.left for pair in input_pairs}
    right_bindings = {pair.parameter: pair.right for pair in input_pairs}

    # Build parameter list
    input_params = []
    output1_params = []
    output2_params = []

    for p in spec.params:
        ty = _type_name(p)
        pair = pairs.get(p.name)
        if p.is_mut_ref:
            pre_name = f"pre_{_var_name(p)}"
            post1_name = f"post1_{_var_name(p)}"
            post2_name = f"post2_{_var_name(p)}"
            # Splitting a `&mut` param into pre/post value copies drops the
            # reference; re-borrow when the pointee is unsized (`&mut [T]`)
            # because Rust rejects unsized fn params. In spec mode `&[T]` and
            # `[T]` denote the same value.
            ty_ann = f"&{ty}" if _is_unsized_ty(ty) else ty
            input_params.extend(
                (name, ty_ann) for name in
                ((pair.left, pair.right) if pair is not None else (pre_name,))
            )
            output1_params.append((post1_name, ty_ann))
            output2_params.append((post2_name, ty_ann))
        elif p.is_ref:
            # Shared reference param: keep the `&` in the synthesized fn
            # signature. Verus spec mode auto-derefs field access (`p.f`),
            # `is` checks (`p is Some`), and equality (`p == x`), so this
            # is no worse than dropping the `&`. Crucially, ensures often
            # call spec methods that take `&T` and pass the param verbatim
            # (e.g. `b == self.range_consistent(lo, hi, dst)` where
            # `range_consistent` takes `&KeyIterator<K>`); preserving the
            # `&` lets those calls type-check unchanged.
            input_params.extend(
                (name, f"&{ty}") for name in
                ((pair.left, pair.right) if pair is not None else (_var_name(p),))
            )
        else:
            input_params.extend(
                (name, ty) for name in
                ((pair.left, pair.right) if pair is not None else (_var_name(p),))
            )

    ret_ty = spec.return_type.name
    output1_params.append(("r1", ret_ty))
    output2_params.append(("r2", ret_ty))

    all_params = input_params + output1_params + output2_params
    params_str = ", ".join(f"{name}: {ty}" for name, ty in all_params)

    # Requires
    requires_str = ""
    if spec.requires or input_pairs:
        # Wrap each clause in `(...)` so constructs like `a ==> b` don't merge
        # with the next clause when joined, and join with commas.
        bindings = (left_bindings, right_bindings) if input_pairs else ({},)
        req_clauses = [
            f"({_substitute_input(c.strip(), spec, input_bindings=run_bindings)})"
            for run_bindings in bindings for c in spec.requires if c.strip()
        ]
        req_clauses.extend(f"({pair.relation})" for pair in input_pairs)
        if req_clauses:
            requires_str = "\n    requires " + ", ".join(req_clauses) + ","

    # Ensures: join individual clauses with &&& (Verus short-circuit conjunction),
    # wrapping each clause in parens so constructs like `matches ==>` are never
    # exposed on the RHS of a binary operator.
    def _join_clauses(clauses: list[str]) -> str:
        parts = [f"({c.strip()})" for c in clauses if c.strip()]
        return "\n            &&& ".join(parts)

    ensures_joined = _join_clauses(spec.ensures)
    if input_pairs and not spec.ensures:
        ensures_joined = "true"
    run1 = _substitute_run(ensures_joined, spec, run_id=1, input_bindings=left_bindings)
    run2 = _substitute_run(ensures_joined, spec, run_id=2, input_bindings=right_bindings)

    # Equality conclusion: call a generated spec fn `{fn_name}_equal(...)`.
    # This fn is a structural-equality relation generated from TypeInfo, which
    # avoids quirks of Verus's default `==` on types whose inner types lack
    # PartialEq (e.g. Result<(), Error> where Error has no Eq impl).
    equal_fn_name = f"{fn_name}_equal"
    equal_body_args = []  # list of (lhs, rhs, ty) used inside equal fn body
    equal_call_args = []  # callsite expressions (wraps `@` for view types)
    equal_params = []     # list of param decls in the spec fn signature
    # r1/r2 always go first
    equal_body_args.append(("r1", "r2", spec.return_type))
    equal_call_args.append(("r1", "r2"))
    equal_params.append(("r1", spec.return_type))
    equal_params.append(("r2", spec.return_type))
    # then each &mut param's post1/post2, using spec_view when available
    for p in spec.params:
        if not p.is_mut_ref:
            continue
        vn = _var_name(p)
        ty = p.type
        if ty.spec_view is not None:
            # callsite passes post1_X@ / post2_X@ (convert to view);
            # equal fn parameter is typed as the View; body accesses fields
            # directly on the bare param name (no `@`).
            view_ty = ty.spec_view
            equal_body_args.append((f"post1_{vn}", f"post2_{vn}", view_ty))
            equal_call_args.append((f"post1_{vn}@", f"post2_{vn}@"))
            equal_params.append((f"post1_{vn}", view_ty))
            equal_params.append((f"post2_{vn}", view_ty))
        else:
            equal_body_args.append((f"post1_{vn}", f"post2_{vn}", ty))
            equal_call_args.append((f"post1_{vn}", f"post2_{vn}"))
            equal_params.append((f"post1_{vn}", ty))
            equal_params.append((f"post2_{vn}", ty))

    call_args_flat = []
    for (a1, a2) in equal_call_args:
        call_args_flat.append(a1)
        call_args_flat.append(a2)
    conclusion = f"{equal_fn_name}({', '.join(call_args_flat)})"

    # Ensures: `({&&& run1 &&& run2}) ==> conclusion`. No assumes here — they
    # go into the body as `assume(...)` statements, which is cleaner and keeps
    # the postcondition stable across search rounds.
    # Lift impl/fn generics onto the proof fn signature, but only the subset
    # actually referenced by params/return/ensures/requires — phantom generics
    # trigger E0284/E0283 type-annotations-needed at the call site of the
    # equal-fn. We include ensures (run1/run2) and requires so that generics
    # used ONLY in spec bodies (e.g. `S::spec_align_of()` for a wrapper fn
    # `fn align_of<S: PmSized>()`) are also kept.
    sig_for_prune = params_str + " " + run1 + " " + run2 + " " + requires_str
    if spec.self_type:
        sig_for_prune = re.sub(r'\bSelf\b', spec.self_type, sig_for_prune)
    if spec.trait_name:
        sig_for_prune += " " + spec.trait_name
    pruned_generics, pruned_where = _prune_generics(
        spec.generics_decl, spec.where_decl, sig_for_prune)

    # Trait-fn fallback: when the source fn lives inside a `trait { ... }`
    # block (no `impl` ancestor), `spec.self_type` is None but `Self`
    # still appears in params / return / ensures. Renderering it verbatim
    # at module scope triggers E0411 ("Self not allowed in a function").
    # Substitute it with a synthetic generic and inject the generic into
    # the proof-fn's type-parameter list so the synthesized fn typechecks
    # in isolation.
    needs_self_subst = (not spec.self_type) and (
        re.search(r'\bSelf\b', params_str) is not None
        or re.search(r'\bSelf\b', run1) is not None
        or re.search(r'\bSelf\b', run2) is not None
        or re.search(r'\bSelf\b', requires_str) is not None
        or re.search(r'\bSelf\b', conclusion) is not None
    )
    if needs_self_subst:
        placeholder = "__DetSelf"
        params_str = _substitute_self_type(params_str, placeholder)
        run1 = _substitute_self_type(run1, placeholder)
        run2 = _substitute_self_type(run2, placeholder)
        requires_str = _substitute_self_type(requires_str, placeholder)
        conclusion = _substitute_self_type(conclusion, placeholder)
        pruned_where = _substitute_self_type(pruned_where, placeholder)
        # If the source fn lives inside a `trait Foo { ... }` declaration,
        # bound `__DetSelf` by the trait so `Self::method` calls (e.g.
        # `Self::zero_spec()`) resolve. Otherwise leave it unbounded.
        placeholder_decl = (
            f"{placeholder}: {spec.trait_name}" if spec.trait_name else placeholder
        )
        # Splice `__DetSelf` into the generics list (creating one if absent).
        if pruned_generics.strip():
            inner = pruned_generics.strip().lstrip('<').rstrip('>').strip()
            pruned_generics = f"<{inner}, {placeholder_decl}>"
        else:
            pruned_generics = f"<{placeholder_decl}>"

    # Build the default equal spec fn body uses bare names (no `@`).
    equal_fn_self_type = spec.self_type
    equal_fn_generics = spec.generics_decl
    if needs_self_subst:
        equal_fn_self_type = "__DetSelf"
        placeholder_decl = (
            f"__DetSelf: {spec.trait_name}" if spec.trait_name else "__DetSelf"
        )
        # Add the placeholder to the equal-fn's generics decl as well.
        if equal_fn_generics.strip():
            inner = equal_fn_generics.strip().lstrip('<').rstrip('>').strip()
            equal_fn_generics = f"<{inner}, {placeholder_decl}>"
        else:
            equal_fn_generics = f"<{placeholder_decl}>"
    equal_param_decls_for_prune = ", ".join(
        f"{n}: {_type_annotation(t)}" for (n, t) in equal_params
    )
    if equal_fn_self_type:
        equal_param_decls_for_prune = re.sub(
            r'\bSelf\b', equal_fn_self_type, equal_param_decls_for_prune
        )
    equal_call_generics, _ = _prune_generics(
        equal_fn_generics, spec.where_decl, equal_param_decls_for_prune
    )
    conclusion = (
        f"{equal_fn_name}{_generic_call_turbofish(equal_call_generics)}"
        f"({', '.join(call_args_flat)})"
    )
    equal_fn_def = _build_equal_fn(
        equal_fn_name, equal_params, equal_body_args, policy,
        generics_decl=equal_fn_generics,
        where_decl=spec.where_decl,
        self_type=equal_fn_self_type,
        view_registry=view_registry,
    )

    where_block = f"\n    {pruned_where}" if pruned_where else ""
    # Reveal block: opt-in opening of ``#[verifier::opaque] open spec fn``
    # bodies in the body of the det check proof. Caller (single_file)
    # supplies the names of spec fns it has just rewritten from
    # ``closed`` to ``opaque open``; we emit ``reveal(name);`` for each
    # so z3 sees the body when checking the det conclusion.
    reveal_block = ""
    if reveal_specs:
        # ``reveal(...)`` is a proof statement; the body of the det fn
        # is proof mode, so this is in-scope. Reveals are scoped to the
        # enclosing proof fn — they do not leak into other call sites.
        reveal_lines = "\n    ".join(f"reveal({n});" for n in reveal_specs)
        reveal_block = f"    {reveal_lines}\n"
    code = f"""proof fn {fn_name}{pruned_generics}({params_str}){where_block}{requires_str}
    ensures
        ({{
            &&& {run1}
            &&& {run2}
        }}) ==> {conclusion},
{{
{reveal_block}{{ASSUMES}}}}"""

    # Substitute `Self` (word-boundary) with the impl target text so the
    # generated proof fn — which lives at module scope — typechecks even
    # when ensures/requires referenced `Self` directly.
    if spec.self_type:
        code = _substitute_self_type(code, spec.self_type)

    return code, equal_fn_def, equal_fn_name, equal_call_args


# ---------------------------------------------------------------------------
# Public API: produce DetCheckSpec
# ---------------------------------------------------------------------------

def build_det_check_spec(
    spec: FunctionSpec,
    check_name: str | None = None,
    verus_config: dict | None = None,
    equal_policy: EqualPolicy | None = None,
    view_registry: Optional["ViewRegistry"] = None,
    source: str = "",
    *,
    input_pairs: tuple[InputPair, ...] = (),
) -> DetCheckSpec:
    """
    Build a DetCheckSpec from a FunctionSpec.

    This is the output of Step 1 (extract + gen_det).

    ``equal_policy`` controls how the generated ``det_<fn>_equal`` spec fn
    coarsens structural equality. Defaults to ``default_policy()`` — all
    ``Err`` values equivalent; everything else strict.

    ``view_registry`` (Phase 2) is the L1+L2+L3 view-aware-equal resolver.
    When supplied, struct types lacking an inline ``TypeInfo.spec_view``
    will be looked up by short name in the project's prelude / alias /
    impl-View tables, and a ``.view()`` / ``@`` projection will be
    emitted instead of recursive structural comparison. Pass ``None`` for
    the legacy (pre-Phase-2) behaviour.

    ``input_pairs`` selects independently quantified inputs and supplies their
    already-approved, already-rendered input relations. The whole precondition
    and each run's postcondition use that run's bindings; mutable inputs retain
    distinct pre/post states. Empty pairs preserve concrete shared inputs.

    ``source`` (optional, full file text) enables closed-spec-fn opening:
    spec fns transitively reachable from the target's ensures that are
    declared ``closed spec fn`` in ``source`` will be (a) recorded on
    the returned DetCheckSpec so the inject phase can rewrite their
    declarations to ``#[verifier::opaque] open spec fn``, and (b)
    ``reveal``'d in the det-check proof body. This eliminates the
    "closed spec fn opacity" class of unknowns where z3 cannot derive
    key facts (e.g. ``self.constants == pre.constants``) from a body
    it can't see.
    """
    if equal_policy is None:
        equal_policy = default_policy()
    validate_input_pairs(spec, input_pairs, check_name)
    spec = deepcopy(spec)

    # For trait-decl synth fns (Self appears in ensures, no concrete impl
    # target) the extractor may have resolved ``Self`` to a same-file impl's
    # concrete struct (e.g. ``SHTKey``). Both schema enumeration AND the
    # equal-fn body would then emit field-projections like ``r1.ukey``,
    # which Verus rejects with E0609 because the synth fn's ``Self`` is the
    # bound type parameter ``__DetSelf``. Quench by rewriting any TypeInfo
    # whose name is ``Self`` to an opaque generic placeholder before either
    # the template or the symbols are built.
    if spec.self_type is None and spec.trait_name is not None:
        opaque_self = TypeInfo(kind=TypeKind.STRUCT, name="__DetSelf",
                               fields=[], variants=[])

        def _quench_self(ti):
            if ti is None:
                return ti
            if ti.kind == TypeKind.STRUCT and ti.name == "Self":
                return opaque_self
            return ti

        spec.return_type = _quench_self(spec.return_type)
        for p in spec.params:
            p.type = _quench_self(p.type)

    # Closed-spec-fn opening: compute the set of spec fns reachable from
    # the ensures that are currently declared ``closed`` in source. Pass
    # them to _build_template (emits ``reveal(...)``) and surface them
    # via DetCheckSpec so the inject phase can rewrite their
    # declarations to ``#[verifier::opaque] open``.
    reveal_specs: list[str] = []        # qualified names for reveal() calls
    opened_names: list[str] = []        # bare names for the source rewrite
    if source:
        # Import locally to avoid a module-load cycle through classify.
        from specdet.adapters.verus.native.spec_helpers import (
            reachable_spec_fns as _reachable_spec_fns,
            closed_spec_fn_qualified_names as _qualified_names,
        )
        reach = _reachable_spec_fns(spec.ensures, source, max_depth=4)
        qual_map = _qualified_names(source, reach)
        # Deterministic order (alphabetical by bare name).
        opened_names = sorted(qual_map.keys())
        reveal_specs = [qual_map[n] for n in opened_names]

    template, equal_fn_def, equal_fn_name, equal_call_args = _build_template(
        spec, check_name, equal_policy, view_registry=view_registry,
        reveal_specs=reveal_specs, input_pairs=input_pairs,
    )
    symbols = _build_symbols(spec, input_pairs=input_pairs)
    check_fn_name = check_name or _default_check_name(spec.name)

    return DetCheckSpec(
        function=spec.name,
        det_check_template=template,
        symbols=symbols,
        verus_config=verus_config or {},
        equal_fn_def=equal_fn_def,
        equal_fn_name=equal_fn_name,
        check_fn_name=check_fn_name,
        equal_policy=equal_policy.to_dict(),
        # callsite form: includes `@` for view-wrapped compound outputs.
        # Used by distinctness phase to call `!{equal_fn_name}(lhs, rhs, ...)`.
        equal_arg_pairs=[
            {"lhs": a1, "rhs": a2} for (a1, a2) in equal_call_args
        ],
        generics_decl=spec.generics_decl,
        where_decl=spec.where_decl,
        self_type=spec.self_type,
        opened_closed_specs=opened_names,
    )


def render_template(
    template_or_spec,
    assumes: list[Assume],
) -> str:
    """Render a det check template with concrete assumes.

    Accepts either a raw template string (legacy) or a DetCheckSpec
    (preferred). When a DetCheckSpec is passed, the generated
    `spec fn {equal_fn_name}(...) -> bool` is prepended to the rendered
    code so the conclusion call `{equal_fn_name}(...)` resolves.
    Replaces `{ASSUMES}` in the template with `assume(...)` statements.
    """
    if isinstance(template_or_spec, DetCheckSpec):
        template = template_or_spec.det_check_template
        equal_fn_def = template_or_spec.equal_fn_def or ""
    else:
        template = template_or_spec
        equal_fn_def = ""

    if assumes:
        assume_parts = [f"    assume({a.expression.strip()});" for a in assumes]
        assume_str = "\n".join(assume_parts) + "\n"
    else:
        assume_str = ""

    body = template.replace("{ASSUMES}", assume_str)
    if equal_fn_def:
        return equal_fn_def + "\n\n" + body
    return body


# ---------------------------------------------------------------------------
# Scope-aware input, state, and result substitution
# ---------------------------------------------------------------------------

def _strip_unary_deref(text: str, name: str, replacement: str) -> str:
    """Strip only dereferences of the free input, never multiplication."""
    return rename_free_identifiers(text, {}, deref_names={name: replacement}, statements=True)


def _substitute_self_type(text: str, self_type: str) -> str:
    """Replace the ``Self`` keyword, not literals or ordinary identifiers.

    This helper also accepts parameter/contract fragments and generated items.
    AST contexts distinguish expression turbofish from type arguments.
    """
    if "Self" not in text:
        return text
    source_text = text.replace("{ASSUMES}", "/*     */")
    contexts = (
        ("", ""),
        ("spec fn __self_probe() -> bool {(\n", "\n)}"),
        ("spec fn __self_probe(", ") {}"),
        ("proof fn __self_probe() ", " {}"),
    )
    for prefix, suffix in contexts:
        tree = _parser.parse((prefix + source_text + suffix).encode())
        if not tree.root_node.has_error:
            break
    else:
        raise Unsupported("Cannot parse Self type substitution without errors")
    offset = len(prefix.encode())
    end = offset + len(text.encode())
    generic = re.match(r'^([\w:]+)\s*<(.*)>\s*$', self_type, re.DOTALL)
    base = generic.group(1) if generic else self_type
    expression_type = f"{base}::<{generic.group(2)}>" if generic else self_type
    edits = []

    def visit(node: ts.Node, macro: bool = False) -> None:
        if node.type in {"line_comment", "block_comment", "string_literal", "raw_string_literal",
                         "char_literal"}:
            return
        macro = macro or node.type in {"macro_invocation", "token_tree"}
        if node.type in {"identifier", "type_identifier"} and node.text == b"Self":
            if macro:
                raise Unsupported("Cannot substitute Self inside an opaque macro")
            if offset <= node.start_byte < node.end_byte <= end:
                parent = node.parent
                if parent.type == "call_expression" and parent.child_by_field_name("function") == node:
                    replacement = base
                elif node.type == "identifier" or (
                    parent.type == "struct_expression" and parent.child_by_field_name("name") == node
                ):
                    replacement = expression_type
                else:
                    replacement = self_type
                edits.append((node.start_byte - offset, node.end_byte - offset, replacement))
            return
        for child in node.named_children:
            visit(child, macro)

    visit(tree.root_node)
    result = text.encode()
    for start, stop, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement.encode() + result[stop:]
    return result.decode()


def _substitution_bindings(
    spec: FunctionSpec, input_bindings: dict[str, str], run_id: int | None,
) -> tuple[dict[str, str], set[str], dict[str, StateNames]]:
    name_map: dict[str, str] = {}
    ref_renames: set[str] = set()
    states: dict[str, StateNames] = {}
    for p in spec.params:
        source_name = "self" if p.is_self else p.name
        vn = _var_name(p)
        pre_name = input_bindings.get(p.name, f"pre_{vn}" if p.is_mut_ref else vn)
        if p.is_mut_ref:
            post_name = f"post{run_id}_{vn}" if run_id is not None else None
            name_map[source_name] = post_name or pre_name
            states[source_name] = StateNames(pre_name, post_name)
            # Unsized pointees remain references in the harness signature.
            # The string form is retained for older direct helper callers.
            ty = p.type.name if isinstance(p.type, TypeInfo) else str(p.type)
            if not _is_unsized_ty(ty):
                ref_renames.add(source_name)
        else:
            name_map[source_name] = pre_name
            if p.is_self:
                states[source_name] = StateNames(pre_name)
    return name_map, ref_renames, states


def _substitute_input(
    requires_raw: str, spec: FunctionSpec, *,
    input_bindings: dict[str, str] | None = None,
) -> str:
    name_map, ref_renames, states = _substitution_bindings(spec, input_bindings or {}, None)
    return rename_free_identifiers(
        requires_raw, name_map, ref_renames, state_names=states, statements=True,
    )


def _rename_idents_in_expr(text: str, name_map: dict, ref_renames: Optional[set] = None) -> str:
    """Rename free value identifiers, respecting scope and UTF-8 byte spans.

    Unknown grammar and opaque macros raise ``Unsupported``; there is no
    textual fallback. Field names, paths, literals, and shadowed locals are
    preserved. Colliding local binders are alpha-renamed to avoid capture.
    """
    return rename_free_identifiers(text, name_map, ref_renames)


def _substitute_run(
    ensures_raw: str, spec: FunctionSpec, run_id: int, *,
    input_bindings: dict[str, str] | None = None,
) -> str:
    name_map, ref_renames, states = _substitution_bindings(spec, input_bindings or {}, run_id)
    for p in spec.params:
        if p.is_mut_ref and p.is_self:
            name_map.setdefault("__PRE__", states["self"].old)
            name_map.setdefault("__POST__", f"post{run_id}_{_var_name(p)}")
    if spec.result_binding:
        name_map[spec.result_binding] = f"r{run_id}"
    name_map.setdefault("__RESULT__", f"r{run_id}")
    return rename_free_identifiers(
        ensures_raw, name_map, ref_renames, state_names=states, match_suffix=run_id,
    )


# ---------------------------------------------------------------------------
# Compatibility entry point for match-binding alpha-renaming
# ---------------------------------------------------------------------------

def _rename_match_bindings(text: str, run_id: int) -> str:
    return rename_free_identifiers(text, {}, match_suffix=run_id)


# ---------------------------------------------------------------------------
# Equal fn generation (structural-equality spec fn built from TypeInfo)
# ---------------------------------------------------------------------------

def _build_equal_fn(
    fn_name: str,
    params: list[tuple[str, TypeInfo]],
    arg_pairs: list[tuple[str, str, TypeInfo]],
    policy: EqualPolicy | None = None,
    generics_decl: str = "",
    where_decl: str = "",
    self_type: str | None = None,
    view_registry: Optional["ViewRegistry"] = None,
) -> str:
    """Emit a Verus spec fn that structurally compares each (lhs, rhs) pair.

    The function is `&&`-joined over all pairs. Each individual equality is
    built recursively by `build_equal_expr` based on TypeInfo + policy, which
    means enums/Results are `match`-split so we never rely on a derived `==`
    that might be missing for nested types (e.g. `Error` without `PartialEq`).

    If ``policy.custom_body`` is set, it is used verbatim as the function
    body (the caller — typically a human reviewer or an LLM hook — takes
    full responsibility for correctness).

    ``generics_decl`` / ``where_decl`` / ``self_type`` propagate the
    enclosing impl's generic context onto the synthesized spec fn.
    """
    if policy is None:
        policy = default_policy()

    param_decls = ", ".join(f"{n}: {_type_annotation(t)}" for (n, t) in params)
    if self_type:
        param_decls = _substitute_self_type(param_decls, self_type)

    if policy.custom_body is not None and policy.custom_body.strip():
        body = policy.custom_body.strip()
        prelude_decls: list[str] = []
    else:
        prelude_decls = []
        clauses = []
        caller_context = f"{generics_decl} {where_decl}".strip()
        for (lhs, rhs, ty) in arg_pairs:
            clauses.append(build_equal_expr(ty, lhs, rhs, policy,
                                            view_registry=view_registry,
                                            prelude_collector=prelude_decls,
                                            caller_generics=caller_context))

        if not clauses:
            body = "true"
        else:
            body = "\n    && ".join(f"({c})" for c in clauses)

    # Header comment makes it explicit to reviewers that this body is
    # generated from a declarative policy and summarises the active rules.
    header = (
        f"// Generated equal-fn for determinism check.\n"
        f"// Policy: errs_equivalent={policy.errs_equivalent}, "
        f"opaque_ok={policy.opaque_ok}"
    )
    if policy.ignore_fields:
        header += f", ignore_fields={sorted(policy.ignore_fields)}"
    if policy.opaque_types:
        header += f", opaque_types={sorted(policy.opaque_types)}"
    if policy.custom_body:
        header += " [custom_body in use]"

    # Drop unused generics (phantom-generic ⇒ E0284 at the call site). Use
    # the post-Self-substitution `param_decls` as the reference signature;
    # the equal-fn return type is `bool`, so generics never appear there.
    pruned_generics, pruned_where = _prune_generics(
        generics_decl, where_decl, param_decls)

    where_block = f"\n    {pruned_where}" if pruned_where else ""
    fn_def = (
        f"{header}\n"
        f"spec fn {fn_name}{pruned_generics}({param_decls}) -> bool{where_block} {{\n"
        f"    {body}\n"
        f"}}"
    )

    # PR-D2: prepend L4 LLM-synthesised `impl View for T { … }` decls so the
    # `.view()` calls embedded in the equal-fn resolve at compile time.
    # Dedupe while preserving first-seen order — the same prelude may be
    # collected multiple times when the type appears in several arg pairs.
    if prelude_decls:
        seen: set[str] = set()
        deduped: list[str] = []
        for d in prelude_decls:
            key = d.strip()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(d.strip())
        prelude_text = (
            "// Explicitly accepted view declarations\n"
            + "\n\n".join(deduped)
        )
        fn_def = prelude_text + "\n\n" + fn_def
    return fn_def


def _type_annotation(ty: TypeInfo) -> str:
    """Render a TypeInfo as a Verus type annotation for a parameter.

    ``&mut`` parameters are split into pre/post value copies, which strips the
    reference and can leave an unsized head type (``&mut [T]`` -> ``[T]``).
    Rust rejects unsized fn params, so re-borrow those as shared references —
    in spec code ``&[T]`` and ``[T]`` denote the same value, and ``@`` / ``==``
    behave identically.
    """
    name = ty.name or ""
    if _is_unsized_ty(name):
        return f"&{name.strip()}"
    return name


# ---------------------------------------------------------------------------
# PR-G — A-3 nested-Err policy: detect Result hiding inside a container
# so we can lift `errs_equivalent` element-wise instead of letting raw `==`
# on Seq/Map structurally compare Err payloads.
# ---------------------------------------------------------------------------

def _contains_result(ty: TypeInfo, _seen: Optional[set[int]] = None) -> bool:
    """True iff `ty` transitively contains a `Result<_, _>`.

    Used by `build_equal_expr` to decide whether raw structural `==` on
    a Seq/Map element would over-compare under `errs_equivalent=True`.
    A `visited` guard handles self-referential TypeInfo graphs (e.g.
    a struct field whose type points back to the struct).
    """
    if _seen is None:
        _seen = set()
    tid = id(ty)
    if tid in _seen:
        return False
    _seen.add(tid)
    if ty.kind == TypeKind.RESULT:
        return True
    for arg in ty.type_args or []:
        if _contains_result(arg, _seen):
            return True
    for fld in ty.fields or []:
        if _contains_result(fld.type, _seen):
            return True
    for var in ty.variants or []:
        if var.inner is not None and _contains_result(var.inner, _seen):
            return True
    return False


def _container_needs_elementwise(ty: TypeInfo, policy: EqualPolicy) -> bool:
    """True iff a container of `ty` cannot be safely compared with raw
    structural equality under `policy`. Result elements need elementwise
    comparison so `errs_equivalent` reaches nested payloads. String elements
    need elementwise comparison so equality goes through `@` rather than raw
    exec String identity."""
    if ty.kind == TypeKind.STR:
        return True
    if not policy.errs_equivalent:
        return False
    return _contains_result(ty)


def _parse_generic_bounds(generics_decl: str) -> dict[str, set[str]]:
    """Parse generic/where bounds into ``{"K": {"Trait"}, ...}``.

    Accepts either a generic declaration like
    ``<K: KeyTrait + VerusClone, V>`` or a combined context like
    ``<K> where K: KeyTrait``.

    Robust against missing leading/trailing angle brackets and trims
    whitespace. Bounds with ``::`` (path traits) are kept as-is.
    """
    import re
    if not generics_decl:
        return {}
    text = generics_decl or ""
    where_tail = ""
    where_match = re.search(r'\bwhere\b', text)
    if where_match:
        where_tail = text[where_match.end():]
        text = text[:where_match.start()]
    m = re.search(r'<([^<>]*(?:<[^<>]*>[^<>]*)*)>', text)
    inside = m.group(1) if m else text
    out: dict[str, set[str]] = {}
    chunks = _split_top_level_commas_text(inside)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if ':' in chunk:
            tp, bounds = chunk.split(':', 1)
            tp = tp.strip()
            traits = {_short_bound_name(b.strip()) for b in bounds.split('+') if b.strip()}
        else:
            tp = chunk.strip()
            traits = set()
        out[tp] = traits

    for pred in _split_top_level_commas_text(where_tail):
        pred = pred.strip()
        if not pred or ':' not in pred:
            continue
        lhs, bounds = pred.split(':', 1)
        lhs = lhs.strip()
        # Associated-type predicates like `T::V: Foo` do not mean `T` itself
        # has view equality, so keep only direct type-parameter bounds here.
        if '::' in lhs:
            continue
        bucket = out.setdefault(lhs, set())
        bucket |= {_short_bound_name(b.strip()) for b in bounds.split('+') if b.strip()}
    return out


def _split_top_level_commas_text(text: str) -> list[str]:
    depth = 0
    chunks: list[str] = []
    cur = []
    for ch in text:
        if ch == '<':
            depth += 1
            cur.append(ch)
        elif ch == '>':
            depth -= 1
            cur.append(ch)
        elif ch == ',' and depth == 0:
            chunks.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        chunks.append("".join(cur))
    return chunks


def _short_bound_name(bound: str) -> str:
    bound = bound.strip()
    if not bound:
        return bound
    bound = re.sub(r'^\?+', '', bound)
    bound = bound.split('<', 1)[0].strip()
    return bound.rsplit('::', 1)[-1].strip()


_VIEW_LIKE_BOUNDS = {"View"}


def _generic_view_equal(
    ty: TypeInfo,
    lhs: str,
    rhs: str,
    caller_generics: str,
) -> Optional[str]:
    """Use view equality for generic types whose bounds guarantee `View`.

    Only an explicit View bound is mechanical evidence. Project-specific
    supertraits require caller-supplied view information, not a name allowlist.
    """
    if not caller_generics or not ty.name:
        return None
    name = _strip_ref_type_name(ty.name.strip())
    caller_bounds = _parse_generic_bounds(caller_generics)
    assoc = re.match(r"^([_A-Za-z][_A-Za-z0-9]*)::[_A-Za-z][_A-Za-z0-9]*$", name)
    if assoc is not None:
        if re.search(
            rf"\b{re.escape(name)}\s*:\s*(?:[^,>]+\+\s*)?View\b",
            caller_generics,
        ):
            return f"({lhs})@ == ({rhs})@"
        return None
    if not re.match(r"^'?[_A-Za-z][_A-Za-z0-9]*$", name):
        return None
    lookup_name = "__DetSelf" if name == "Self" and "__DetSelf" in caller_bounds else name
    bounds = caller_bounds.get(lookup_name, set())
    if bounds & _VIEW_LIKE_BOUNDS:
        return f"({lhs})@ == ({rhs})@"
    return None


def _strip_ref_type_name(name: str) -> str:
    """Return the inner type for simple reference spellings.

    The extractor preserves reference return types as names like ``&PMRegion``.
    If the inner generic has a view-like bound (``PMRegion:
    PersistentMemoryRegion``), equality should still go through ``@`` rather
    than raw reference identity.
    """
    text = name.strip()
    while text.startswith("&"):
        text = text[1:].lstrip()
        if text.startswith("'"):
            parts = text.split(None, 1)
            text = parts[1].lstrip() if len(parts) == 2 else ""
        if text.startswith("mut "):
            text = text[4:].lstrip()
    return text


def _shim_bounds_satisfied(prelude_decl: str, caller_generics: str) -> bool:
    """Check whether the trait bounds required by a registered View shim
    (e.g. ``impl<K: KeyTrait + VerusClone + View> View for KeyIterator<K>``)
    are all satisfied by the equal-fn's own generic decl.

    Returns True when the shim's bounds are a subset of caller bounds
    (so emitting ``(x).view()`` will compile). Returns False when any
    extra bound is required that the caller doesn't provide — the
    caller should then fall back to a non-view comparison rather than
    emit a ``.view()`` projection that won't typecheck.

    When ``caller_generics`` is empty (no generic context to check
    against) we assume the call site is monomorphic and skip the
    check (return True) — this matches the legacy behaviour for
    non-generic structs whose shim doesn't introduce new bounds.
    """
    if not prelude_decl:
        return True
    import re
    m = re.search(r'impl\s*<([^<>]*(?:<[^<>]*>[^<>]*)*)>', prelude_decl)
    if not m:
        return True
    shim_decl = "<" + m.group(1) + ">"
    shim_bounds = _parse_generic_bounds(shim_decl)
    caller_bounds = _parse_generic_bounds(caller_generics)
    builtin = {"Sized", "?Sized"}
    for tp, required in shim_bounds.items():
        # If the shim binds a type-param that the caller doesn't expose
        # (it's monomorphic at the call site), the shim's extra bounds
        # don't constrain us — verus will instantiate ``K`` to a
        # concrete type and check separately.
        if tp not in caller_bounds:
            continue
        available = caller_bounds[tp]
        missing = required - available - builtin
        if missing:
            return False
    return True


def build_equal_expr(
    ty: TypeInfo,
    lhs: str,
    rhs: str,
    policy: EqualPolicy | None = None,
    view_registry: Optional["ViewRegistry"] = None,
    prelude_collector: Optional[list[str]] = None,
    caller_generics: str = "",
) -> str:
    """Recursively emit a Verus boolean expression that structurally compares
    two values of the given type. The output is always inside `spec` mode.

    For primitive / Set / Seq, uses `==` directly (Verus has structural
    equality on these). For Result / Option / generic Enum, emits a
    conjunction of `is`-discriminator equality + per-variant implication
    comparing inner fields. For Struct, emits a conjunction over field
    comparisons; uses `@` if the struct has a spec view.

    ``policy`` (default: ``default_policy()``) controls coarsening rules —
    e.g. ``errs_equivalent`` collapses all ``Err`` to one equivalence class,
    ``opaque_ok`` does the same for ``Ok``, ``opaque_types`` treats whole
    named types as equivalent, and ``ignore_fields`` omits struct fields.

    ``view_registry`` (Phase 2 L1+L2+L3+L4 resolver) — when provided, the
    STRUCT / UNKNOWN fallback first consults the registry for a
    view-aware-equal projection (prelude container, alias deref, discovered
    ``impl View``, or explicitly accepted view declarations). When
    ``None``, behaviour is unchanged.

    ``prelude_collector`` — list passed in by ``_build_equal_fn`` to gather
    L4 ``impl View for T { … }`` declarations that gen_det must emit before
    the equal-fn so the synthesized ``.view()`` calls resolve at compile
    time. Caller dedupes.
    """
    if policy is None:
        policy = default_policy()

    k = ty.kind

    # Whole-type opacity (policy override)
    if ty.name and ty.name in policy.opaque_types:
        return "true"

    # Permission/provenance carrier with no deterministic observable value.
    if _is_points_to_raw_type(ty):
        return "true /* PointsToRaw permission: opaque */"

    # Raw-pointer opacity (mechanical default).
    # `*mut T` / `*const T` addresses are allocator-nondeterministic at the
    # Verus/Z3 level — structural `==` compares abstract heap addresses and
    # always admits spurious "different pointer" witnesses (see observations).
    # Gated by `compare_raw_pointers` for the rare case a spec genuinely
    # pins pointer identity through ghost state.
    if _is_raw_pointer_type(ty) and not policy.compare_raw_pointers:
        return "true /* raw pointer: opaque by default */"

    if k == TypeKind.STR:
        return f"({lhs})@ == ({rhs})@"

    # Primitive / value types where structural `==` is safe
    if k in (
        TypeKind.INT, TypeKind.USIZE, TypeKind.ISIZE,
        TypeKind.U8, TypeKind.U16, TypeKind.U32, TypeKind.U64,
        TypeKind.I8, TypeKind.I16, TypeKind.I32, TypeKind.I64,
        TypeKind.BOOL, TypeKind.UNIT,
    ):
        return f"{lhs} == {rhs}"

    # `Set<E>` — extensional equality. Z3's structural `==` on Set is the
    # constructor-term identity (history of `.insert` / `.remove`); `=~=`
    # is "for all e, lhs.contains(e) == rhs.contains(e)" which is the
    # equality the spec actually wants and what every downstream lemma /
    # ensures clause already relies on. Inside spec-fn bodies `==` is NOT
    # auto-promoted to `=~=` (only in `assert` / `ensures` / `invariant`
    # — see https://verus-lang.github.io/verus/guide/extensional_equality.html),
    # so we must emit `=~=` explicitly here.
    if k == TypeKind.SET:
        return f"{lhs} =~= {rhs}"


    # PR-G: Seq<E> where E transitively contains Result needs elementwise
    # comparison so `errs_equivalent` reaches the nested Err. Raw `==` on
    # a spec Seq is element-wise structural and would compare Err payloads.
    if k == TypeKind.SEQ:
        # PR-N: when the extractor recorded a `spec_view` on this SEQ, the
        # type is really a wrapper (e.g. exec `Vec<T>`) whose struct-eq is
        # not derivable from view equality (`Vec` is external_body in
        # Verus). Compare through the view so the obligation matches what
        # the wrapper's `ensures` actually delivers.
        if ty.spec_view is not None:
            return build_equal_expr(
                ty.spec_view, f"({lhs})@", f"({rhs})@", policy,
                view_registry=view_registry,
                prelude_collector=prelude_collector,
                caller_generics=caller_generics,
            )
        elem_ty = ty.type_args[0] if ty.type_args else None
        if elem_ty is not None and elem_ty.kind == TypeKind.STR:
            return (
                f"(({lhs}).map_values(|x: String| x@)"
                f" == ({rhs}).map_values(|x: String| x@))"
            )
        if elem_ty is not None and _container_needs_elementwise(elem_ty, policy):
            elem_eq = build_equal_expr(elem_ty, f"{lhs}[i]", f"{rhs}[i]", policy,
                                       view_registry=view_registry,
                                       prelude_collector=prelude_collector, caller_generics=caller_generics)
            return (
                f"({lhs}.len() == {rhs}.len()"
                f" && forall|i: int| 0 <= i < {lhs}.len() ==> ({elem_eq}))"
            )
        # Extensional equality (see Set branch comment above).
        return f"{lhs} =~= {rhs}"

    # PR-G: Map<K, V> where V transitively contains Result also needs
    # value-wise comparison. Domain must match, then each value is
    # compared via the policy-aware equal-expr.
    if k == TypeKind.MAP:
        # PR-N: same wrapper rule as SEQ — when the extractor saw `spec_view`
        # on this MAP, the type is really an exec `HashMap<K,V>` (or
        # similar) and struct-eq is not derivable from view equality.
        if ty.spec_view is not None:
            return build_equal_expr(
                ty.spec_view, f"({lhs})@", f"({rhs})@", policy,
                view_registry=view_registry,
                prelude_collector=prelude_collector,
                caller_generics=caller_generics,
            )
        v_ty = ty.type_args[1] if len(ty.type_args) > 1 else None
        if v_ty is not None and _container_needs_elementwise(v_ty, policy):
            val_eq = build_equal_expr(v_ty, f"{lhs}[k]", f"{rhs}[k]", policy,
                                      view_registry=view_registry,
                                      prelude_collector=prelude_collector, caller_generics=caller_generics)
            k_ty = ty.type_args[0] if ty.type_args else TypeInfo(TypeKind.INT, "int")
            k_name = k_ty.name or "int"
            return (
                f"({lhs}.dom() =~= {rhs}.dom()"
                f" && forall|k: {k_name}| {lhs}.dom().contains(k)"
                f" ==> ({val_eq}))"
            )
        # Extensional equality (see Set branch comment above).
        return f"{lhs} =~= {rhs}"

    if k == TypeKind.RESULT:
        ok_ty = ty.type_args[0] if len(ty.type_args) > 0 else TypeInfo(TypeKind.UNIT, "()")
        err_ty = ty.type_args[1] if len(ty.type_args) > 1 else TypeInfo(TypeKind.UNKNOWN, "unknown")
        # Ok side — opaque or recurse
        if policy.opaque_ok:
            ok_clause = f"(({lhs} is Ok) ==> true)"
        else:
            ok_eq = build_equal_expr(ok_ty, f"{lhs}->Ok_0", f"{rhs}->Ok_0", policy,
                                     view_registry=view_registry,
                                     prelude_collector=prelude_collector, caller_generics=caller_generics)
            ok_clause = f"(({lhs} is Ok) ==> ({ok_eq}))"
        # Err side — collapse all Errs or recurse
        if policy.errs_equivalent:
            # All Errs are equivalent: only discriminator match matters.
            return (
                f"(({lhs} is Ok) == ({rhs} is Ok))"
                f" && {ok_clause}"
            )
        err_eq = build_equal_expr(err_ty, f"{lhs}->Err_0", f"{rhs}->Err_0", policy,
                                  view_registry=view_registry,
                                  prelude_collector=prelude_collector, caller_generics=caller_generics)
        return (
            f"(({lhs} is Ok) == ({rhs} is Ok))"
            f" && {ok_clause}"
            f" && (({lhs} is Err) ==> ({err_eq}))"
        )

    if k == TypeKind.OPTION:
        inner_ty = ty.type_args[0] if ty.type_args else TypeInfo(TypeKind.UNKNOWN, "unknown")
        some_eq = build_equal_expr(inner_ty, f"{lhs}->Some_0", f"{rhs}->Some_0", policy,
                                   view_registry=view_registry,
                                   prelude_collector=prelude_collector, caller_generics=caller_generics)
        return (
            f"(({lhs} is Some) == ({rhs} is Some))"
            f" && (({lhs} is Some) ==> ({some_eq}))"
        )

    # PR-F: Tracked<T> / Ghost<T> — wrapper types whose spec value is the
    # inner T accessed via `@`. Compare through the projection so policy
    # rules (errs_equivalent / opaque_ok / ignore_fields) apply to the
    # inner value, not the wrapper identity.
    if k in (TypeKind.TRACKED, TypeKind.GHOST):
        if ty.type_args:
            inner_ty = ty.type_args[0]
            if inner_ty.spec_view is not None:
                return build_equal_expr(
                    inner_ty.spec_view,
                    f"({lhs})@@",
                    f"({rhs})@@",
                    policy,
                    view_registry=view_registry,
                    prelude_collector=prelude_collector,
                    caller_generics=caller_generics,
                )
            inner_eq = build_equal_expr(
                inner_ty, f"({lhs})@", f"({rhs})@", policy,
                view_registry=view_registry,
                prelude_collector=prelude_collector,
                caller_generics=caller_generics,
            )
            return inner_eq
        # No inner info — compare wrappers via `@` raw.
        return f"({lhs})@ == ({rhs})@"

    # PR-F: PointsTo<V> — the meaningful spec equality is "same init
    # state, and (if init) same inner value, at the same addr". We
    # compare each projection separately so policy can drive the inner.
    if k == TypeKind.POINTS_TO:
        if _is_pcell_points_to_type(ty):
            return "true /* pcell PointsTo permission: opaque */"
        parts = [
            f"(({lhs}).is_init() == ({rhs}).is_init())",
            f"(({lhs}).ptr().addr() == ({rhs}).ptr().addr())",
        ]
        if ty.type_args:
            v_ty = ty.type_args[0]
            v_eq = build_equal_expr(
                v_ty, f"({lhs}).value()", f"({rhs}).value()", policy,
                view_registry=view_registry,
                prelude_collector=prelude_collector,
                caller_generics=caller_generics,
            )
            parts.append(
                f"(({lhs}).is_init() ==> ({v_eq}))"
            )
        return " && ".join(parts)

    if k == TypeKind.ENUM:
        if not ty.variants:
            # No variant info — try the view registry before raw `==` so
            # macro-generated enums (e.g. `state_machine!`) can still get
            # a view-aware equal.
            vreg_eq = _try_view_registry_equal(view_registry, ty, lhs, rhs,
                                               prelude_collector,
                                               caller_generics=caller_generics)
            if vreg_eq is not None:
                return vreg_eq
            return f"{lhs} == {rhs}"
        # C-like enums (unit variants with integer discriminants) collapse
        # to a single integer comparison. This matches how the spec
        # typically talks about them (`x as usize == N`), avoids
        # enumerating every variant, and keeps the equal-fn valid even
        # when some variants are cfg-gated out of the active build.
        if ty.is_c_like_enum():
            return f"({lhs} as int) == ({rhs} as int)"
        # View-prefer: if the enum has an inline `spec fn view` (so
        # ``TypeInfo.spec_view`` is populated, e.g. ``CSingleMessage ->
        # SingleMessage<Message>``), compare via the view rather than
        # enumerating every variant. The spec ensures typically pin the
        # post-state via ``ret@ == ...`` / ``self@ == ...``, so the
        # view-form lines up with the proof obligation; the per-variant
        # form forces z3 to disprove a much bigger structural conjunction
        # and is the dominant root cause of ``clone_up_to_view`` unknowns
        # in IronKV. Mirrors the struct branch (line ~1691).
        view = ty.spec_view
        lhs_is_viewed = lhs.endswith("@")
        rhs_is_viewed = rhs.endswith("@")
        if view is not None and not (lhs_is_viewed and rhs_is_viewed):
            return f"({lhs})@ == ({rhs})@"
        # Also consult the L1+L2+L3+L4 resolver before falling back to
        # the per-variant structural comparison.
        if view is None and not (lhs_is_viewed and rhs_is_viewed):
            vreg_eq = _try_view_registry_equal(view_registry, ty, lhs, rhs,
                                               prelude_collector,
                                               caller_generics=caller_generics)
            if vreg_eq is not None:
                return vreg_eq
        # ``#[verifier::ext_equal]`` enum: per the Verus extensional_equality
        # guide, ``=~=`` on this type drills recursively through any
        # Seq / Set / Map / spec_fn fields (and through nested
        # ext_equal-annotated types) while still collapsing to ``==`` on
        # plain struct/primitive fields. Short-circuit to ``lhs =~= rhs``
        # so the generated equal_fn body stays a single token instead of
        # an exponential per-variant structural drill. The substitution
        # is semantically monotonic: for non-ext_equal types ``=~=`` is
        # equivalent to ``==``, and for ext_equal types it is at least as
        # strong as the field-wise comparison would have been. Skip when
        # the policy needs field-level granularity (``ignore_fields``).
        if ty.is_ext_equal and not policy.ignore_fields:
            return f"({lhs}) =~= ({rhs})"
        # Pre-compute the set of struct-form field names that appear in
        # more than one variant. Verus only auto-generates an ``arrow_f``
        # accessor when ``f`` is unique across all variants of the enum;
        # for ambiguous names ``lhs->f`` errors with "method `arrow_f`
        # not found". We fall back to whole-variant structural equality
        # in that case (sound: ``lhs == rhs`` is strictly stronger than
        # per-field equality given matching discriminators).
        ambiguous_struct_fields = ty.ambiguous_struct_variant_fields()
        # For each variant, require both sides to be that variant and inner
        # fields to match. The discriminators must agree first.
        parts = []
        for v in ty.variants:
            disc = f"(({lhs} is {v.name}) == ({rhs} is {v.name}))"
            parts.append(disc)
            if v.inner is not None:
                if v.struct_form and v.inner.fields:
                    # Struct-form variant ``V { f1, f2, ... }`` — Verus
                    # accesses fields directly as ``lhs->fname`` only when
                    # the name is unambiguous across all variants.
                    has_ambiguous = any(
                        fld.name in ambiguous_struct_fields
                        for fld in v.inner.fields
                    )
                    if has_ambiguous:
                        # Fall back to whole-variant structural equality.
                        # Sound: under the discriminator guard ``lhs is V``,
                        # ``lhs == rhs`` implies all per-field equalities.
                        inner_eq = f"{lhs} == {rhs}"
                    else:
                        field_clauses: list[str] = []
                        for fld in v.inner.fields:
                            field_clauses.append(build_equal_expr(
                                fld.type,
                                f"{lhs}->{fld.name}",
                                f"{rhs}->{fld.name}",
                                policy,
                                view_registry=view_registry,
                                prelude_collector=prelude_collector,
                caller_generics=caller_generics,
                            ))
                        inner_eq = " && ".join(f"({c})" for c in field_clauses) \
                            if field_clauses else "true"
                else:
                    # Tuple-form variant ``V(T)`` — accessed via ``lhs->V_0``.
                    inner_eq = build_equal_expr(
                        v.inner, f"{lhs}->{v.name}_0", f"{rhs}->{v.name}_0",
                        policy,
                        view_registry=view_registry,
                        prelude_collector=prelude_collector,
                caller_generics=caller_generics,
                    )
                parts.append(f"(({lhs} is {v.name}) ==> ({inner_eq}))")
        return " && ".join(parts)

    if k == TypeKind.STRUCT:
        view = ty.spec_view
        lhs_is_viewed = lhs.endswith("@")
        rhs_is_viewed = rhs.endswith("@")
        # Opaque (``#[verifier(external_body)]``) struct: Verus disallows
        # field expressions, but ``==`` on the whole value is still permitted
        # and chains through any ensures clause that pins both runs' values
        # to a deterministic spec call (e.g. ``r1 == spec_from_vec(v) &&
        # r2 == spec_from_vec(v) ==> r1 == r2``). Fall straight through to
        # ``lhs == rhs`` — descending into ``ty.fields`` would emit
        # ``r1.m == r2.m``, which Verus rejects.
        #
        # Exception: when the opaque type ALSO has a ``spec_view`` (e.g.
        # ``HashMap<V>`` which is external_body but exposes
        # ``spec fn view(self) -> Map<K,V>``), defer the opaque-vs-view
        # decision to the view branch below. The view-projected
        # comparison ``(r1)@ == (r2)@`` is what the surrounding ensures
        # clauses actually pin (since exec/spec interop relies on the
        # view, not the opaque value), so the bare ``r1 == r2`` would be
        # provably weaker than the specs can drive — leading to unknown
        # on otherwise-deterministic constructors like ``HashMap::new``.
        if ty.is_opaque and view is None:
            vreg_eq = _try_view_registry_equal(view_registry, ty, lhs, rhs,
                                               prelude_collector,
                                               caller_generics=caller_generics)
            if vreg_eq is not None:
                return vreg_eq
            generic_view_eq = _generic_view_equal(ty, lhs, rhs, caller_generics)
            if generic_view_eq is not None:
                return generic_view_eq
            return f"{lhs} == {rhs}"
        # Transparent alias: an LLM type-completion patch with empty
        # `fields` + a `spec_view` is the canonical encoding of a
        # ``pub type Alias<...> = SomeView<...>;`` alias. At the Rust
        # type level the alias resolves to the view directly, so any
        # value-of-type-Alias is already a value-of-type-View and we
        # must NOT wrap with ``()@`` (the view side has no ``.view()``
        # method). Recurse into the spec_view type directly using the
        # caller's lhs/rhs unchanged.
        if view is not None and not ty.fields and not ty.variants:
            return build_equal_expr(view, lhs, rhs, policy,
                                    view_registry=view_registry,
                                    prelude_collector=prelude_collector, caller_generics=caller_generics)
        if view is not None and view.fields and lhs_is_viewed and rhs_is_viewed:
            clauses = []
            for fld in view.fields:
                if fld.name in policy.ignore_fields:
                    continue
                clauses.append(build_equal_expr(
                    fld.type, f"{lhs}.{fld.name}", f"{rhs}.{fld.name}", policy,
                    view_registry=view_registry,
                    prelude_collector=prelude_collector,
                caller_generics=caller_generics,
                ))
            if not clauses:
                return "true"
            return " && ".join(f"({c})" for c in clauses)
        if view is not None and not (lhs_is_viewed and rhs_is_viewed):
            # Nested struct-with-view (e.g. Result<Kheap, _> where caller
            # passed `r1->Ok_0`). Compare through the view at spec level.
            # Note: ignore_fields/errs_equivalent cannot be threaded through
            # a raw `@ == @` comparison — if the caller needs that, they
            # should supply a custom_body for this function.
            return f"({lhs})@ == ({rhs})@"
        # Phase-2 view resolution takes precedence over source-struct
        # extensional equality. An external/opaque wrapper may be marked
        # `#[verifier::ext_equal]` while also exposing the actual semantic
        # state through `impl View`; comparing the wrapper with `=~=` can
        # still leave representation fields uninterpreted. The registered
        # view is the contract-facing equality and should win.
        vreg_eq = _try_view_registry_equal(
            view_registry,
            ty,
            lhs,
            rhs,
            prelude_collector,
            caller_generics=caller_generics,
        )
        if vreg_eq is not None:
            return vreg_eq
        # ``#[verifier::ext_equal]`` struct: per the Verus extensional_equality
        # guide, ``=~=`` on this type drills recursively through any
        # Seq / Set / Map / spec_fn fields (and through nested
        # ext_equal-annotated types) while still collapsing to ``==`` on
        # plain primitive / struct fields. Short-circuit to
        # ``lhs =~= rhs`` so the generated equal_fn body stays a single
        # token instead of an exponential per-field structural drill.
        # The substitution is semantically monotonic: for non-ext_equal
        # types ``=~=`` is equivalent to ``==``, and for ext_equal types
        # it is at least as strong as the field-wise comparison would
        # have been. Skip when the policy needs field-level granularity
        # (``ignore_fields``); also skip when the value is already
        # view-projected (``lhs@``) because the view target may have its
        # own equality semantics that don't match the source struct's
        # ext_equal annotation.
        if (ty.is_ext_equal and not policy.ignore_fields
                and not lhs_is_viewed and not rhs_is_viewed):
            return f"({lhs}) =~= ({rhs})"
        generic_view_eq = _generic_view_equal(ty, lhs, rhs, caller_generics)
        if generic_view_eq is not None:
            return generic_view_eq
        if ty.fields:
            clauses = []
            for fld in ty.fields:
                if fld.name in policy.ignore_fields:
                    continue
                clauses.append(build_equal_expr(
                    fld.type, f"{lhs}.{fld.name}", f"{rhs}.{fld.name}", policy,
                    view_registry=view_registry,
                    prelude_collector=prelude_collector,
                caller_generics=caller_generics,
                ))
            if not clauses:
                return "true"
            return " && ".join(f"({c})" for c in clauses)
        # No field info at all — fall back to `==`
        return f"{lhs} == {rhs}"

    # UNKNOWN: try the view registry before falling back to raw `==`.
    vreg_eq = _try_view_registry_equal(view_registry, ty, lhs, rhs,
                                       prelude_collector,
                                       caller_generics=caller_generics)
    if vreg_eq is not None:
        return vreg_eq
    generic_view_eq = _generic_view_equal(ty, lhs, rhs, caller_generics)
    if generic_view_eq is not None:
        return generic_view_eq
    return f"{lhs} == {rhs}"


def _try_view_registry_equal(
    view_registry: Optional["ViewRegistry"],
    ty: TypeInfo,
    lhs: str,
    rhs: str,
    prelude_collector: Optional[list[str]] = None,
    caller_generics: str = "",
) -> Optional[str]:
    """Phase 2 hook: ask the resolver for a view-aware equality
    expression. Returns ``None`` when the registry isn't supplied or
    the type is uncovered — caller falls through to its existing
    structural fallback. Failures inside the resolver are swallowed
    and logged (we never want a registry bug to break codegen).

    When the resolver returns an L4 hit (LLM-synthesised view), the
    accepted ``impl View for T { … }`` declaration is appended to
    ``prelude_collector`` (deduplicated by the caller). The caller
    must prepend the collector to the synthesized equal-fn so the
    ``.view()`` projection actually resolves at compile time.

    ``caller_generics`` is the synthesized equal-fn's own generic
    declaration. If supplied, the resolver's prelude shim is rejected
    when it introduces trait bounds beyond what the caller already
    has — emitting ``(x).view()`` against an inapplicable shim would
    fail to typecheck (E0277 ``the trait bound ... is not satisfied``).
    The caller falls back to field-by-field structural comparison in
    that case.
    """
    if view_registry is None or not ty.name:
        return None
    try:
        type_expr = _typeinfo_to_typeexpr(ty)
        res = view_registry.resolve(type_expr)
        if not res.is_resolved:
            return None
        if res.prelude_decl and not _shim_bounds_satisfied(
                res.prelude_decl, caller_generics):
            return None
        if res.prelude_decl and prelude_collector is not None:
            prelude_collector.append(res.prelude_decl)
        lhs_view = res.view_expr(lhs)
        rhs_view = res.view_expr(rhs)
        if (res.layer == "L3"
                and "inherent view for" not in (res.rationale or "")
                and not _generic_l3_view_bounds_satisfied(
                    ty.name,
                    caller_generics,
                    res.view_type_text,
                )):
            return None
        return f"({lhs_view} =~= {rhs_view})"
    except Exception as e:  # pragma: no cover — safety net
        logger.warning("ViewRegistry.equal_expr failed for %s: %s",
                       ty.name, e)
        return None


def _generic_l3_view_bounds_satisfied(
    type_name: str,
    caller_generics: str,
    view_type_text: str = "",
) -> bool:
    m = re.match(r"^[A-Za-z_][A-Za-z0-9_:]*\s*<(.+)>\s*$", type_name.strip())
    if m is None:
        return True
    caller_bounds = _parse_generic_bounds(caller_generics)
    projected_args = set(
        re.findall(
            r"<\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s+View\s*>::V",
            view_type_text,
        )
    )
    if view_type_text and not projected_args:
        return True
    args = [
        arg.strip()
        for arg in _split_top_level_commas_text(m.group(1))
    ]
    if projected_args:
        args = [arg for arg in args if arg in projected_args]
    for arg in args:
        arg = arg.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", arg):
            continue
        if arg in caller_bounds and not (caller_bounds[arg] & _VIEW_LIKE_BOUNDS):
            return False
    return True


# ---------------------------------------------------------------------------
# rebuild_equal_fn — regenerate equal fn after llm_refine (types may have
# become more informative, e.g. UNKNOWN -> struct).
# ---------------------------------------------------------------------------

def rebuild_equal_fn(det_spec: DetCheckSpec,
                     view_registry: Optional["ViewRegistry"] = None,
                     ) -> DetCheckSpec:
    """Regenerate ``equal_fn_def`` / ``equal_fn_name`` / ``equal_arg_pairs`` from
    the (possibly refined) ``det_spec.symbols`` and return the updated spec.

    Strategy: find the output symbols by phase (output_simple / output_compound),
    group into pairs (r1/r2, post1_X/post2_X), then replay ``_build_equal_fn``.

    ``view_registry`` — optional Phase-2 L1+L2+L3 view resolver, propagated
    to ``build_equal_expr``. ``None`` preserves legacy behaviour.
    """
    base = det_spec.check_fn_name or _default_check_name(det_spec.function)
    equal_fn_name = f"{base}_equal"

    # Collect output symbols. Symbols are created by _build_symbols with
    # names r1/r2 (output_simple) and post1_X/post2_X (output_compound).
    sym_by_name: dict[str, Symbol] = {s.name: s for s in det_spec.symbols}

    params: list[tuple[str, TypeInfo]] = []
    body_pairs: list[tuple[str, str, TypeInfo]] = []   # used inside equal fn
    callsite_pairs: list[tuple[str, str]] = []         # used at call site
    declared: set[str] = set()

    # r1 / r2 first
    if "r1" in sym_by_name and "r2" in sym_by_name:
        r1 = sym_by_name["r1"]
        r2 = sym_by_name["r2"]
        params.append(("r1", r1.type))
        params.append(("r2", r2.type))
        body_pairs.append(("r1", "r2", r1.type))
        callsite_pairs.append(("r1", "r2"))
        declared.add("r1")
        declared.add("r2")

    # then post1_X / post2_X — for compound outputs with a spec view, the
    # equal fn parameter is typed as the view (e.g. BitmapView), and the
    # callsite passes `post1_X@`. Inside the fn body the local name is
    # already the view, so we access fields directly (no `@`).
    for name in sorted(sym_by_name.keys()):
        if not name.startswith("post1_"):
            continue
        partner = "post2_" + name[len("post1_"):]
        if partner not in sym_by_name:
            continue
        if name in declared or partner in declared:
            continue
        sym = sym_by_name[name]
        ty = sym.type
        if sym.phase == "output_compound" and ty.spec_view is not None:
            view_ty = ty.spec_view
            params.append((name, view_ty))
            params.append((partner, view_ty))
            body_pairs.append((name, partner, view_ty))
            callsite_pairs.append((f"{name}@", f"{partner}@"))
        else:
            params.append((name, ty))
            params.append((partner, ty))
            body_pairs.append((name, partner, ty))
            callsite_pairs.append((name, partner))
        declared.add(name)
        declared.add(partner)

    equal_fn_def = _build_equal_fn(
        equal_fn_name, params, body_pairs,
        EqualPolicy.from_dict(det_spec.equal_policy),
        generics_decl=det_spec.generics_decl,
        where_decl=det_spec.where_decl,
        self_type=det_spec.self_type,
        view_registry=view_registry,
    )
    det_spec.equal_fn_def = equal_fn_def
    det_spec.equal_fn_name = equal_fn_name
    det_spec.equal_arg_pairs = [{"lhs": a1, "rhs": a2} for (a1, a2) in callsite_pairs]

    # Also rewrite the template's conclusion call so it uses the updated
    # callsite forms (may differ from pre-refine if spec_view became known).
    call_args_flat = []
    for (a1, a2) in callsite_pairs:
        call_args_flat.append(a1)
        call_args_flat.append(a2)
    new_call = f"{equal_fn_name}({', '.join(call_args_flat)})"
    import re as _re
    det_spec.det_check_template = _re.sub(
        rf"{_re.escape(equal_fn_name)}\([^)]*\)",
        new_call,
        det_spec.det_check_template,
        count=1,
    )
    return det_spec


# ---------------------------------------------------------------------------
# Self-tests — invoked via `python -m specdet.adapters.verus.native.codegen.gen_det test`
# ---------------------------------------------------------------------------

def _run_self_tests() -> int:
    """Lightweight in-process tests for build_equal_expr + PR-G nested-Err."""
    from specdet.adapters.verus.native.extract.types import VariantInfo, FieldInfo

    failures: list[str] = []

    def check(label: str, got: str, expected_substrs: list[str],
              forbidden_substrs: Optional[list[str]] = None):
        for s in expected_substrs:
            if s not in got:
                failures.append(f"{label}: expected substring {s!r} in:\n  {got}")
        for s in (forbidden_substrs or []):
            if s in got:
                failures.append(f"{label}: forbidden substring {s!r} present in:\n  {got}")

    # --- PR-G fixtures: nested Result inside containers ---
    int_ty = TypeInfo(TypeKind.INT, "int")
    u32_ty = TypeInfo(TypeKind.U32, "u32")
    u8_ty = TypeInfo(TypeKind.U8, "u8")
    err_ty = TypeInfo(TypeKind.STRUCT, "MyErr")  # opaque error struct
    result_u32_err = TypeInfo(
        TypeKind.RESULT, "Result<u32, MyErr>",
        type_args=[u32_ty, err_ty],
        variants=[VariantInfo("Ok", u32_ty), VariantInfo("Err", err_ty)],
    )

    # Top-level Result — both policies covered.
    expr_top = build_equal_expr(result_u32_err, "r1", "r2", default_policy())
    check("top-level Result with errs_equivalent",
          expr_top,
          ["(r1 is Ok) == (r2 is Ok)", "r1->Ok_0", "r2->Ok_0"],
          forbidden_substrs=["Err_0"])  # err side collapsed

    expr_top_strict = build_equal_expr(
        result_u32_err, "r1", "r2",
        EqualPolicy(errs_equivalent=False))
    check("top-level Result strict",
          expr_top_strict,
          ["r1->Ok_0", "r1->Err_0", "r2->Err_0"])

    # Seq<Result<u32, MyErr>> — was buggy pre-PR-G (raw `==`).
    seq_of_result = TypeInfo(TypeKind.SEQ, "Seq<Result<u32, MyErr>>",
                             type_args=[result_u32_err])
    expr_seq = build_equal_expr(seq_of_result, "s1", "s2", default_policy())
    check("Seq<Result<…>> with errs_equivalent uses forall element-wise",
          expr_seq,
          ["s1.len() == s2.len()", "forall|i: int|", "0 <= i < s1.len()",
           "s1[i]", "s2[i]", "is Ok"],
          forbidden_substrs=["s1[i]->Err_0", "s2[i]->Err_0"])

    # Same Seq under strict policy — element-wise structural equality is
    # fine since errs_equivalent=False means we don't want to collapse
    # anything. The fallback path emits `s1 =~= s2` (extensional equality),
    # which is sound + matches how Verus auto-promotes `==` to `=~=` for
    # Seq inside ensures positions.
    expr_seq_strict = build_equal_expr(
        seq_of_result, "s1", "s2", EqualPolicy(errs_equivalent=False))
    check("Seq<Result<…>> strict policy uses =~=",
          expr_seq_strict, ["s1 =~= s2"])

    # Seq<u32> — fallback path uses `=~=` (semantically equivalent to
    # `==` for Seq, matches Verus's ensures-position auto-promotion).
    seq_u32 = TypeInfo(TypeKind.SEQ, "Seq<u32>", type_args=[u32_ty])
    expr_seq_u32 = build_equal_expr(seq_u32, "s1", "s2", default_policy())
    check("Seq<u32> fallback uses =~=", expr_seq_u32, ["s1 =~= s2"],
          forbidden_substrs=["forall"])

    # PR-N: Vec<u8> wrapper (kind=SEQ, spec_view=Seq<u8>) — Vec is
    # external_body so struct-eq is not derivable. Must compare via @.
    vec_u8_view = TypeInfo(TypeKind.SEQ, "Seq<u8>", type_args=[u8_ty])
    vec_u8 = TypeInfo(TypeKind.SEQ, "Vec<u8>", type_args=[u8_ty],
                      spec_view=vec_u8_view)
    expr_vec_u8 = build_equal_expr(vec_u8, "v1", "v2", default_policy())
    check("Vec<u8> (SEQ + spec_view) compares via @",
          expr_vec_u8, ["(v1)@ =~= (v2)@"],
          forbidden_substrs=["v1 == v2"])

    # PR-N: Struct{ id: Vec<u8> } — STRUCT recurses into the Vec field,
    # which must in turn compare via @ (the Case-1 ironkv shape).
    from specdet.adapters.verus.native.extract.types import FieldInfo as _FI
    end_point = TypeInfo(
        TypeKind.STRUCT, "EndPoint",
        fields=[_FI("id", vec_u8)],
    )
    expr_end_point = build_equal_expr(end_point, "r1", "r2", default_policy())
    check("Struct{id: Vec<u8>} drops to view-eq on the Vec field",
          expr_end_point, ["(r1.id)@ =~= (r2.id)@"],
          forbidden_substrs=["r1.id == r2.id"])

    # A tuple containing PointsToRaw is not itself a PointsToRaw permission.
    # The old substring test collapsed the entire tuple to true and silently
    # discarded observable sibling fields such as the returned pointer/value.
    points_to_raw = TypeInfo(
        TypeKind.STRUCT, "PointsToRaw", is_opaque=True)
    tracked_points_to_raw = TypeInfo(
        TypeKind.TRACKED, "Tracked<PointsToRaw>",
        type_args=[points_to_raw])
    tuple_with_points_to_raw = TypeInfo(
        TypeKind.STRUCT, "(u8, Tracked<PointsToRaw>)",
        fields=[_FI("0", u8_ty), _FI("1", tracked_points_to_raw)])
    expr_tuple_points_to_raw = build_equal_expr(
        tuple_with_points_to_raw, "r1", "r2", default_policy())
    check("Tuple containing PointsToRaw keeps observable sibling fields",
          expr_tuple_points_to_raw, ["r1.0 == r2.0"])

    # PR-N: HashMap<K,V> wrapper (kind=MAP, spec_view=Map<K,V>) — symmetric
    # to the Vec case. struct-eq is not derivable; compare via @.
    map_view = TypeInfo(TypeKind.MAP, "Map<int, u32>",
                        type_args=[int_ty, u32_ty])
    hashmap = TypeInfo(TypeKind.MAP, "HashMap<int, u32>",
                       type_args=[int_ty, u32_ty], spec_view=map_view)
    expr_hashmap = build_equal_expr(hashmap, "h1", "h2", default_policy())
    check("HashMap<K,V> (MAP + spec_view) compares via @",
          expr_hashmap, ["(h1)@ =~= (h2)@"],
          forbidden_substrs=["h1 == h2"])

    class _ResolvedView:
        layer = "L3"
        rationale = "impl View for StringHashSet"
        view_type_text = "Set<Seq<char>>"
        prelude_decl = None
        is_resolved = True

        @staticmethod
        def view_expr(value: str) -> str:
            return f"({value}).view()"

    class _ViewFirstRegistry:
        @staticmethod
        def resolve(_expr):
            return _ResolvedView()

    ext_equal_with_view = TypeInfo(
        TypeKind.STRUCT,
        "StringHashSet",
        is_ext_equal=True,
    )
    expr_ext_equal_with_view = build_equal_expr(
        ext_equal_with_view,
        "s1",
        "s2",
        default_policy(),
        view_registry=_ViewFirstRegistry(),
    )
    check("Registered View takes precedence over ext_equal wrapper",
          expr_ext_equal_with_view,
          ["(s1).view() =~= (s2).view()"],
          forbidden_substrs=["s1) =~= (s2"])

    # Map<int, Result<u32, MyErr>> — was buggy pre-PR-G. dom comparison
    # uses =~= (extensional set equality).
    map_of_result = TypeInfo(TypeKind.MAP, "Map<int, Result<u32, MyErr>>",
                             type_args=[int_ty, result_u32_err])
    expr_map = build_equal_expr(map_of_result, "m1", "m2", default_policy())
    check("Map<_, Result<…>> with errs_equivalent uses dom+forall",
          expr_map,
          ["m1.dom() =~= m2.dom()", "forall|k: int|",
           "m1.dom().contains(k)", "m1[k]", "m2[k]"],
          forbidden_substrs=["m1[k]->Err_0", "m2[k]->Err_0"])

    # Map<int, u32> — fallback uses `=~=` (extensional map equality).
    map_int_u32 = TypeInfo(TypeKind.MAP, "Map<int, u32>",
                           type_args=[int_ty, u32_ty])
    expr_map_iu = build_equal_expr(map_int_u32, "m1", "m2", default_policy())
    check("Map<int, u32> fallback uses =~=", expr_map_iu, ["m1 =~= m2"],
          forbidden_substrs=["forall"])

    # Result<Seq<Result<u32, MyErr>>, MyErr> — outer Err collapsed AND
    # inner Seq elementwise lift.
    outer = TypeInfo(
        TypeKind.RESULT,
        "Result<Seq<Result<u32, MyErr>>, MyErr>",
        type_args=[seq_of_result, err_ty],
        variants=[VariantInfo("Ok", seq_of_result),
                  VariantInfo("Err", err_ty)],
    )
    expr_outer = build_equal_expr(outer, "r1", "r2", default_policy())
    check("Result<Seq<Result<…>>, _> with errs_equivalent",
          expr_outer,
          ["(r1 is Ok) == (r2 is Ok)",
           "r1->Ok_0.len() == r2->Ok_0.len()",
           "forall|i: int|"],
          forbidden_substrs=["r1->Err_0", "r1->Ok_0[i]->Err_0"])

    # Struct with field of type Result — STRUCT branch recurses field-by-field.
    from specdet.adapters.verus.native.extract.types import FieldInfo
    struct_with_result = TypeInfo(
        TypeKind.STRUCT, "Holder",
        fields=[FieldInfo("payload", result_u32_err)],
    )
    expr_struct = build_equal_expr(struct_with_result, "h1", "h2",
                                   default_policy())
    check("Struct with Result field collapses Err",
          expr_struct,
          ["h1.payload", "h2.payload", "is Ok"],
          forbidden_substrs=["h1.payload->Err_0"])

    # ext_equal short-circuit (struct): an ext_equal-annotated struct
    # collapses to ``lhs =~= rhs`` instead of recursive field expansion.
    # Per the Verus extensional_equality guide, this drills through any
    # Seq / Set / Map / spec_fn / nested ext_equal fields automatically.
    ext_struct = TypeInfo(
        TypeKind.STRUCT, "SingleDelivery",
        fields=[FieldInfo("send_state", map_int_u32),
                FieldInfo("receive_state", u32_ty)],
        is_ext_equal=True,
    )
    expr_ext_struct = build_equal_expr(ext_struct, "p1", "p2", default_policy())
    check("ext_equal struct short-circuits to =~=",
          expr_ext_struct, ["p1) =~= (p2"],
          forbidden_substrs=["p1.send_state", "p1.receive_state"])

    # ext_equal short-circuit (enum): same behaviour on enums; per-variant
    # expansion is skipped.
    ext_enum = TypeInfo(
        TypeKind.ENUM, "CSingleMessage",
        variants=[VariantInfo("Ack", u32_ty),
                  VariantInfo("InvalidMessage", None)],
        is_ext_equal=True,
    )
    expr_ext_enum = build_equal_expr(ext_enum, "m1", "m2", default_policy())
    check("ext_equal enum short-circuits to =~=",
          expr_ext_enum, ["m1) =~= (m2"],
          forbidden_substrs=["is Ack", "m1->Ack_0"])

    # ext_equal opacity guard: opaque (external_body) struct WITHOUT
    # ``spec_view`` must short-circuit to raw ``==``; the extensional
    # drill would try to descend into field expressions that Verus
    # rejects.
    ext_opaque = TypeInfo(
        TypeKind.STRUCT, "OpaqueExt",
        fields=[FieldInfo("data", u32_ty)],
        is_opaque=True,
        is_ext_equal=True,  # weird combo, but defend against it
    )
    expr_ext_opaque = build_equal_expr(ext_opaque, "o1", "o2", default_policy())
    check("ext_equal + opaque uses raw ==",
          expr_ext_opaque, ["o1 == o2"],
          forbidden_substrs=["=~="])

    # Opacity + spec_view interaction: when an external_body struct ALSO
    # exposes ``spec fn view(self) -> ...`` (canonical example is the
    # ironkv ``HashMap<V>`` wrapper: STRUCT with one private exec field
    # ``m`` plus ``is_opaque=True`` and ``spec_view=Map<K,V>``), the
    # equal_fn must NOT short-circuit to ``r1 == r2`` — that abstract
    # equality is too strong for the surrounding ensures clauses (which
    # pin ``r@``, not ``r``) to discharge, leading to R0=unknown on
    # otherwise-deterministic constructors. Compare via the view's @
    # projection instead. Note: ``fields`` is non-empty (matches the
    # real Tier 1.5 patch shape), so the line 1716 transparent-alias
    # path does NOT fire here — execution flows to the view branch at
    # line 1734.
    opaque_with_view = TypeInfo(
        TypeKind.STRUCT, "HashMap<V>",
        fields=[FieldInfo("m", TypeInfo(TypeKind.UNKNOWN,
                                        "collections::HashMap<K, V>"))],
        is_opaque=True,
        spec_view=TypeInfo(TypeKind.MAP, "Map<K, V>",
                           type_args=[int_ty, u32_ty]),
    )
    expr_opaque_view = build_equal_expr(opaque_with_view, "r1", "r2",
                                        default_policy())
    check("opaque struct with spec_view compares via @ (not bare ==)",
          expr_opaque_view, ["(r1)@ == (r2)@"],
          forbidden_substrs=["r1 == r2"])

    # ext_equal + ignore_fields policy: short-circuit MUST be skipped because
    # the extensional comparator can't honour per-field ignores.
    policy_ignore = EqualPolicy(ignore_fields={"send_state"})
    expr_ext_ignore = build_equal_expr(ext_struct, "p1", "p2", policy_ignore)
    check("ext_equal + ignore_fields skips short-circuit",
          expr_ext_ignore, ["p1.receive_state"],
          forbidden_substrs=["=~=", "p1.send_state"])

    # _contains_result helper sanity
    assert _contains_result(result_u32_err) is True, "Result detected"
    assert _contains_result(seq_of_result) is True, "Seq<Result> detected"
    assert _contains_result(map_of_result) is True, "Map<_, Result> detected"
    assert _contains_result(int_ty) is False, "int has no Result"
    assert _contains_result(seq_u32) is False, "Seq<u32> has no Result"
    assert _contains_result(struct_with_result) is True, "Struct with field"

    # _container_needs_elementwise gated by policy.
    assert _container_needs_elementwise(result_u32_err, default_policy()) is True
    assert _container_needs_elementwise(
        result_u32_err, EqualPolicy(errs_equivalent=False)) is False

    # Self-referential type — must not infinite-loop in _contains_result.
    self_ref = TypeInfo(TypeKind.STRUCT, "Self")
    self_ref.fields = [FieldInfo("next", self_ref)]
    assert _contains_result(self_ref) is False, "self-ref returns False"

    # --- PR-F fixtures: Tracked<T> / Ghost<T> / PointsTo<V> equality ---
    bool_ty = TypeInfo(TypeKind.BOOL, "bool")
    usize_ty = TypeInfo(TypeKind.USIZE, "usize")

    tracked_u32 = TypeInfo(TypeKind.TRACKED, "Tracked<u32>",
                           type_args=[u32_ty])
    ghost_seq_u32 = TypeInfo(TypeKind.GHOST, "Ghost<Seq<u32>>",
                             type_args=[seq_u32])
    points_to_u32 = TypeInfo(TypeKind.POINTS_TO, "PointsTo<u32>",
                             type_args=[u32_ty])

    expr_tracked = build_equal_expr(tracked_u32, "t1", "t2", default_policy())
    check("Tracked<u32> compares through @",
          expr_tracked, ["(t1)@", "(t2)@"])

    dealloc_view = TypeInfo(TypeKind.UNKNOWN, "DeallocData")
    dealloc_ty = TypeInfo(
        TypeKind.STRUCT, "Dealloc",
        fields=[FieldInfo("no_copy", TypeInfo(TypeKind.UNKNOWN, "NoCopy"))],
        spec_view=dealloc_view,
        is_opaque=True,
    )
    tracked_dealloc = TypeInfo(
        TypeKind.TRACKED, "Tracked<Dealloc>",
        type_args=[dealloc_ty],
    )
    expr_tracked_dealloc = build_equal_expr(
        tracked_dealloc, "t1", "t2", default_policy())
    check("Tracked<T-with-view> compares through double @",
          expr_tracked_dealloc, ["(t1)@@", "(t2)@@"],
          forbidden_substrs=["no_copy"])

    expr_ghost = build_equal_expr(ghost_seq_u32, "g1", "g2", default_policy())
    check("Ghost<Seq<u32>> compares through @ then raw == on Seq",
          expr_ghost, ["(g1)@", "(g2)@"])

    expr_pt = build_equal_expr(points_to_u32, "p1", "p2", default_policy())
    check("PointsTo<u32> emits is_init/ptr().addr/value clauses",
          expr_pt,
          ["(p1).is_init() == (p2).is_init()",
           "(p1).ptr().addr() == (p2).ptr().addr()",
           "(p1).is_init() ==> (",
           "(p1).value()", "(p2).value()"])

    # Ghost<Result<u32, MyErr>> — PR-F + PR-G interaction: policy must
    # still collapse the Err side through the wrapper.
    ghost_result = TypeInfo(TypeKind.GHOST, "Ghost<Result<u32, MyErr>>",
                            type_args=[result_u32_err])
    expr_gr = build_equal_expr(ghost_result, "g1", "g2", default_policy())
    check("Ghost<Result<…>> with errs_equivalent collapses Err inside",
          expr_gr,
          ["(g1)@ is Ok) == ((g2)@ is Ok)", "Ok_0"],
          forbidden_substrs=["Err_0"])  # err collapsed

    # PR-F + PR-G: Tracked<Seq<Result<u32, MyErr>>> should yield forall lift.
    tracked_seq_result = TypeInfo(
        TypeKind.TRACKED, "Tracked<Seq<Result<u32, MyErr>>>",
        type_args=[seq_of_result])
    expr_tsr = build_equal_expr(tracked_seq_result, "t1", "t2",
                                default_policy())
    check("Tracked<Seq<Result<…>>> projects + elementwise lift",
          expr_tsr,
          ["(t1)@", "(t2)@", "forall|i: int|", "is Ok"],
          forbidden_substrs=["Err_0"])

    # --- Bug A: struct-form enum with field name shared across variants.
    # Verus refuses to auto-generate ``arrow_v`` when ``v`` is not unique
    # across all variants. The struct-form branch must detect this and
    # fall back to whole-variant structural equality for variants that
    # contain any ambiguous field.
    payload_ty = TypeInfo(TypeKind.SEQ, "Seq<u8>", type_args=[u8_ty])
    sr_inner = TypeInfo(TypeKind.STRUCT, "CMessage::SetRequest",
                        fields=[FieldInfo("v", payload_ty),
                                FieldInfo("k", u32_ty)])
    rep_inner = TypeInfo(TypeKind.STRUCT, "CMessage::Reply",
                         fields=[FieldInfo("v", payload_ty),
                                 FieldInfo("rk", u32_ty)])
    del_inner = TypeInfo(TypeKind.STRUCT, "CMessage::Delegate",
                         fields=[FieldInfo("h", u32_ty)])
    cmessage_ty = TypeInfo(
        TypeKind.ENUM, "CMessage",
        variants=[
            VariantInfo("SetRequest", sr_inner, struct_form=True),
            VariantInfo("Reply", rep_inner, struct_form=True),
            VariantInfo("Delegate", del_inner, struct_form=True),
        ],
    )
    assert cmessage_ty.ambiguous_struct_variant_fields() == {"v"}
    expr_cmsg = build_equal_expr(cmessage_ty, "lhs", "rhs", default_policy())
    # ambiguous-field variants must NOT emit ``->v`` accessors anywhere
    check("CMessage ambiguous `v` falls back to whole-variant equality",
          expr_cmsg,
          ["(lhs is SetRequest) ==> (lhs == rhs)",
           "(lhs is Reply) ==> (lhs == rhs)",
           # Delegate has only ``h`` (unambiguous) — keeps per-field form
           "(lhs is Delegate) ==> ((lhs->h == rhs->h))"],
          forbidden_substrs=["lhs->v", "rhs->v"])

    # An enum whose struct-form fields are all unique keeps per-field form.
    a_inner = TypeInfo(TypeKind.STRUCT, "Msg::A",
                       fields=[FieldInfo("a", u32_ty)])
    b_inner = TypeInfo(TypeKind.STRUCT, "Msg::B",
                       fields=[FieldInfo("b", u32_ty)])
    msg_ty = TypeInfo(
        TypeKind.ENUM, "Msg",
        variants=[
            VariantInfo("A", a_inner, struct_form=True),
            VariantInfo("B", b_inner, struct_form=True),
        ],
    )
    assert msg_ty.ambiguous_struct_variant_fields() == set()
    expr_msg = build_equal_expr(msg_ty, "lhs", "rhs", default_policy())
    check("Msg unambiguous struct-form keeps per-field accessors",
          expr_msg,
          ["lhs->a == rhs->a", "lhs->b == rhs->b"],
          forbidden_substrs=["(lhs is A) ==> (lhs == rhs)"])

    # A2 regression — _strip_unary_deref must preserve binary `*` (multiplication)
    # while still rewriting genuine `*p` derefs. Pre-fix, gen_det stripped `*`
    # from `4 * va_range.len`, producing the unparseable `4 va_range.len`.
    got = _strip_unary_deref("4 * va_range.len", "va_range", "va_range")
    if got != "4 * va_range.len":
        failures.append(
            f"A2: _strip_unary_deref must preserve binary `*` in '4 * va_range.len'; "
            f"got {got!r}"
        )
    got = _strip_unary_deref("(*va_range).len", "va_range", "va_range")
    if got != "(va_range).len":
        failures.append(
            f"A2: _strip_unary_deref must strip unary `*` at expression start "
            f"in '(*va_range).len'; got {got!r}"
        )
    got = _strip_unary_deref("foo(*va_range)", "va_range", "va_range")
    if got != "foo(va_range)":
        failures.append(
            f"A2: _strip_unary_deref must strip unary `*` after `(`; "
            f"got {got!r}"
        )
    got = _strip_unary_deref("x + *va_range", "va_range", "va_range")
    if got != "x + va_range":
        failures.append(
            f"A2: _strip_unary_deref must strip unary `*` after binary `+`; "
            f"got {got!r}"
        )
    got = _strip_unary_deref("len * 4 + va_range", "va_range", "va_range")
    if got != "len * 4 + va_range":
        failures.append(
            f"A2: _strip_unary_deref must not corrupt unrelated `*` operators; "
            f"got {got!r}"
        )

    # L3 view bounds: only generic parameters actually projected through
    # `T::V` require a View-like bound. HashMapWithView<Key, Value> maps to
    # Map<<Key as View>::V, Value>; Value intentionally needs no View bound.
    if not _generic_l3_view_bounds_satisfied(
        "HashMapWithView<Key, Value>",
        "<Key: View + Eq + Hash, Value>",
        "Map<<Key as View>::V, Value>",
    ):
        failures.append(
            "L3 view-bound gate should accept unprojected generic Value"
        )
    if _generic_l3_view_bounds_satisfied(
        "HashMapWithView<Key, Value>",
        "<Key: Eq + Hash, Value>",
        "Map<<Key as View>::V, Value>",
    ):
        failures.append(
            "L3 view-bound gate should reject projected Key without View"
        )
    if not _generic_l3_view_bounds_satisfied(
        "StringHashMap<Value>",
        "<Value>",
        "Map<Seq<char>, Value>",
    ):
        failures.append(
            "L3 view-bound gate should accept concrete-key StringHashMap view"
        )

    # A3 regression — `Self` in a trait-fn ensures must be replaced by the
    # synthetic `__DetSelf` generic, and `__DetSelf` must be bounded by the
    # trait name (so `Self::method` calls resolve under verus).
    src_trait = (
        "verus! {\n"
        "pub trait KeyTrait: Sized {\n"
        "    spec fn zero_spec() -> Self;\n"
        "    fn zero() -> (z: Self)\n"
        "        ensures z == Self::zero_spec()\n"
        "    { Self::zero_spec() }\n"
        "}\n"
        "}\n"
    )
    from specdet.adapters.verus.native.extract.extractor import extract_spec
    spec_trait = extract_spec(src_trait, "zero", type_sources=[])
    ds_trait = build_det_check_spec(spec_trait)
    tpl = ds_trait.det_check_template
    if re.search(r'\bSelf\b', tpl):
        failures.append(
            f"A3: rendered template must not contain bare 'Self' after substitution; "
            f"template head:\n{tpl[:400]}"
        )
    if "__DetSelf: KeyTrait" not in tpl:
        failures.append(
            f"A3: synthesized fn must bound __DetSelf by trait name 'KeyTrait'; "
            f"template head:\n{tpl[:400]}"
        )

    # Regression — _substitute_input for &mut self must strip leading `*`
    # on `*old(self)` (and bare `*self`). Pre-fix, gen_det left a stray
    # `*pre_self_` in the requires block, which Verus rejected with E0614
    # because `HostState` (the by-value param type) cannot be dereferenced.
    self_param = Param(name='self', type='HostState',
                       is_self=True, is_ref=True, is_mut_ref=True)
    spec_self = FunctionSpec(
        name="dummy",
        params=[self_param],
        return_type=TypeInfo(TypeKind.UNIT, "()"),
        requires=[],
        ensures=[],
        type_defs={},
    )
    got = _substitute_input("let x = *old(self);", spec_self)
    if got.strip() != "let x = pre_self_;":
        failures.append(
            f"requires-self-deref: expected `let x = pre_self_;`, "
            f"got {got!r}"
        )
    got = _substitute_input("foo(*self)", spec_self)
    if got.strip() != "foo(pre_self_)":
        failures.append(
            f"requires-self-deref: expected `foo(pre_self_)`, got {got!r}"
        )

    # Regression — a mutable-reference pre-state is stored by value in the
    # determinism theorem, but helper calls may retain a `&T` parameter.
    # Borrow only when the substituted pre-state is the complete call
    # argument; view and equality expressions must remain owned.
    mut_param = Param(name='s', type='String',
                      is_self=False, is_ref=True, is_mut_ref=True)
    spec_mut = FunctionSpec(
        name='truncate',
        params=[mut_param],
        return_type='()',
        requires=[],
        ensures=[],
        type_defs={},
    )
    got = _substitute_input(
        "string_bytes(old(s)) == encode_utf8(old(s)@)",
        spec_mut,
    )
    if got.strip() != "string_bytes(&pre_s) == encode_utf8(pre_s@)":
        failures.append(
            "requires-mut-ref-call-borrow: expected "
            "`string_bytes(&pre_s) == encode_utf8(pre_s@)`, "
            f"got {got!r}"
        )
    got = _substitute_input("string_bytes(s)", spec_mut)
    if got.strip() != "string_bytes(&pre_s)":
        failures.append(
            "requires-mut-ref-bare-call-borrow: expected "
            f"`string_bytes(&pre_s)`, got {got!r}"
        )

    # Regression — _substitute_self_type must produce turbofish form for
    # ``Self::method(...)`` calls when self_type has generic args. Without
    # this, ``HashMap<V>::get_spec(...)`` is emitted in expression position,
    # which Rust rejects with E0423 (`HashMap` parsed as a value, not a path).
    got = _substitute_self_type("r == Self::get_spec(self_@, key)", "HashMap<V>")
    if got != "r == HashMap::<V>::get_spec(self_@, key)":
        failures.append(
            "Self::method turbofish: expected `HashMap::<V>::get_spec(...)`, "
            f"got {got!r}"
        )
    # Plain (no-generic) self_type still works.
    got = _substitute_self_type("r == Self::zero_spec()", "SHTKey")
    if got != "r == SHTKey::zero_spec()":
        failures.append(
            "Self::method (no generics): expected `SHTKey::zero_spec()`, "
            f"got {got!r}"
        )
    # Type-position Self in the same string still uses full form.
    got = _substitute_self_type("r1: Self, r2: Self", "HashMap<V>")
    if got != "r1: HashMap<V>, r2: HashMap<V>":
        failures.append(
            "Self in type-position must keep angle-bracket form; "
            f"got {got!r}"
        )

    # Regression — _typeinfo_to_typeexpr must collapse a raw generic
    # ``ty.name="Foo<T>"`` to short head ``Foo`` whether ``type_args``
    # is populated (LLM-completed types) or empty (path-resolver
    # fallback in extractor). Without this the registry's short-name
    # index misses and we silently fall through to field-by-field
    # structural equality even when a View is registered (root cause
    # of the ~40 "equal_fn over-strict" ironkv unknowns).
    ti_generic = TypeInfo(
        kind=TypeKind.STRUCT, name="StrictlyOrderedMap<K>",
        type_args=[TypeInfo(kind=TypeKind.STRUCT, name="K")],
    )
    te_g = _typeinfo_to_typeexpr(ti_generic)
    if te_g.head != "StrictlyOrderedMap":
        failures.append(
            "_typeinfo_to_typeexpr must strip generic suffix from head; "
            f"got head={te_g.head!r}"
        )
    # Same when type_args is empty (extractor's raw-name fallback).
    ti_generic_noargs = TypeInfo(
        kind=TypeKind.STRUCT, name="StrictlyOrderedMap<K>", type_args=[]
    )
    te_g2 = _typeinfo_to_typeexpr(ti_generic_noargs)
    if te_g2.head != "StrictlyOrderedMap":
        failures.append(
            "_typeinfo_to_typeexpr must strip generic suffix even when "
            f"type_args=[]; got head={te_g2.head!r}"
        )
    # Non-generic name stays unchanged.
    ti_plain = TypeInfo(kind=TypeKind.STRUCT, name="Foo")
    te_p = _typeinfo_to_typeexpr(ti_plain)
    if te_p.head != "Foo":
        failures.append(
            f"_typeinfo_to_typeexpr plain name: expected head='Foo', got {te_p.head!r}"
        )
    ti_qualified = TypeInfo(
        kind=TypeKind.UNKNOWN,
        name="alloc::collections::BTreeSet<Key, A>",
        type_args=[
            TypeInfo(kind=TypeKind.UNKNOWN, name="Key"),
            TypeInfo(kind=TypeKind.UNKNOWN, name="A"),
        ],
    )
    te_q = _typeinfo_to_typeexpr(ti_qualified)
    if te_q.head != "BTreeSet":
        failures.append(
            "_typeinfo_to_typeexpr must strip module qualification from head; "
            f"got head={te_q.head!r}"
        )

    # Regression — _shim_bounds_satisfied must reject a View shim that
    # introduces a trait bound the caller's generics doesn't satisfy.
    # Root cause of the rerun6 KeyIterator regression: the L4 shim
    # ``impl<K: KeyTrait + VerusClone + View> View for KeyIterator<K>``
    # requires ``K: View``, but the synthesized ``spec fn det_end_equal
    # <K: KeyTrait + VerusClone>`` doesn't carry that bound, so the
    # emitted ``(r1).view()`` failed to typecheck (E0277).
    key_iter_shim = (
        "impl<K: KeyTrait + VerusClone + View> View for KeyIterator<K> {\n"
        "    type V = Option<<K as View>::V>;\n"
        "    closed spec fn view(&self) -> Self::V { self.k@ }\n"
        "}"
    )
    caller_gens_strict = "<K: KeyTrait + VerusClone>"
    if _shim_bounds_satisfied(key_iter_shim, caller_gens_strict):
        failures.append(
            "_shim_bounds_satisfied must reject KeyIterator shim against "
            "caller lacking `K: View`"
        )
    # When caller carries the View bound, the shim is accepted.
    caller_gens_with_view = "<K: KeyTrait + VerusClone + View>"
    if not _shim_bounds_satisfied(key_iter_shim, caller_gens_with_view):
        failures.append(
            "_shim_bounds_satisfied must accept KeyIterator shim when caller "
            "has `K: View`"
        )
    # Shim with bounds that match caller exactly — accept.
    som_shim = (
        "impl<K: KeyTrait + VerusClone> View for StrictlyOrderedMap<K> {\n"
        "    type V = Map<K, ID>;\n"
        "    closed spec fn view(&self) -> Self::V { self.m@ }\n"
        "}"
    )
    if not _shim_bounds_satisfied(som_shim, caller_gens_strict):
        failures.append(
            "_shim_bounds_satisfied must accept StrictlyOrderedMap shim "
            "against matching caller bounds"
        )
    # Empty caller-generics ⇒ shim is monomorphic at call site; accept.
    if not _shim_bounds_satisfied(key_iter_shim, ""):
        failures.append(
            "_shim_bounds_satisfied must accept any shim when caller has "
            "no generics (monomorphic)"
        )

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All gen_det self-tests passed.")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        raise SystemExit(_run_self_tests())
    print("usage: python -m specdet.adapters.verus.native.codegen.gen_det test")
    raise SystemExit(2)
