"""Pure, source-backed alias types and generic spec-view method evidence.

Sources are strings, or a mapping of diagnostic filenames to strings. Nothing
is read from disk and no trait bounds, clauses, or source annotations are added
to the input FunctionSpec. Unsupported/ambiguous source evidence is an error,
not permission to invent a View implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass

import tree_sitter as ts

from .native.extract.attrs import parse_item_attrs
from .native.extract.extractor import _extract_params, _parse_type_node, _parser
from .native.extract.type_registry import (
    TypeDef, TypeExpr, _apply_attrs, _parse_enum_item, _parse_struct_item,
    _parse_type_alias, _parse_type_expr,
)
from .native.extract.types import FieldInfo, FunctionSpec, TypeInfo, TypeKind, VariantInfo
from .native.view.registry import SourceBoundView, _source_bound_key


Sources = Mapping[str, str] | Iterable[str]


class TypeContextError(ValueError):
    """Source context cannot establish an unambiguous native type or view."""


_COMMENTS = {"line_comment", "block_comment"}
_CANONICAL_VIEW = {"View", "vstd::view::View", "vstd::prelude::View"}


def _children(node: ts.Node | None) -> list[ts.Node]:
    return [c for c in node.named_children if c.type not in _COMMENTS] if node else []


def _unwrap(node: ts.Node) -> ts.Node:
    if node.type == "declaration_with_attrs":
        items = [c for c in _children(node) if c.type not in {"attribute_item", "inner_attribute_item"}]
        if len(items) == 1:
            return items[0]
    return node


def _type_node(text: str) -> ts.Node:
    tree = _parser.parse(f"type __TypeContext = {text};".encode())
    items = _children(tree.root_node)
    item = _unwrap(items[0]) if len(items) == 1 else None
    node = item.child_by_field_name("type") if item and item.type == "type_item" else None
    if tree.root_node.has_error or node is None:
        raise TypeContextError(f"Cannot parse source type: {text!r}")
    return node


def _key(text: str) -> tuple[str, ...]:
    try:
        return _source_bound_key(text)
    except ValueError as exc:
        raise TypeContextError(str(exc)) from exc


def _substitute_type(
    text: str, bindings: Mapping[str, str], *, associated: Mapping[str, str] | None = None,
) -> str:
    """Substitute type/const/lifetime parameters at AST token byte spans."""
    if not bindings and not associated:
        return text
    root = _type_node(text)
    offset = root.start_byte
    edits: list[tuple[int, int, str]] = []

    def visit(node: ts.Node) -> None:
        if node.type in _COMMENTS or node.type in {
            "string_literal", "raw_string_literal", "char_literal",
        }:
            return
        if node.type == "macro_invocation":
            raise TypeContextError("Cannot substitute generic parameters inside a type macro")
        if associated and node.type == "scoped_type_identifier":
            path, name = node.child_by_field_name("path"), node.child_by_field_name("name")
            if path is not None and name is not None and path.text == b"Self" and name.text.decode() in associated:
                edits.append((node.start_byte - offset, node.end_byte - offset, associated[name.text.decode()]))
                return
        if node.type in {"identifier", "type_identifier", "lifetime"}:
            name = node.text.decode()
            if name in bindings:
                parent = node.parent
                # The final name in a path is an associated item, not a
                # parameter; path roots such as T::Item do get substituted.
                if (parent is not None and parent.type in {"scoped_identifier", "scoped_type_identifier"}
                        and parent.child_by_field_name("name") == node):
                    return
                edits.append((node.start_byte - offset, node.end_byte - offset, bindings[name]))
                return
        for child in _children(node):
            visit(child)

    visit(root)
    data = root.text
    for start, end, replacement in sorted(edits, reverse=True):
        data = data[:start] + replacement.encode() + data[end:]
    result = data.decode()
    _type_node(result)
    return result


def _type_expr(node: ts.Node, generics: set[str], consts: set[str]) -> TypeExpr:
    """Use the native type grammar without its uppercase-const heuristic."""
    raw = node.text.decode()
    kind = node.type
    if kind in {"type_identifier", "primitive_type", "unit_type", "scoped_type_identifier"}:
        result = _parse_type_expr(node, generics)
        if kind == "scoped_type_identifier":
            # Associated types must not collapse to their final identifier.
            path = node.child_by_field_name("path")
            if path is not None and (
                path.text.decode() in generics or path.type == "bracketed_type"
            ):
                result.head = raw
        return result
    if kind in {"generic_type", "generic_type_with_turbofish"}:
        head = node.child_by_field_name("type")
        args = node.child_by_field_name("type_arguments")
        if head is None or args is None:
            raise TypeContextError(f"Unsupported generic type: {raw}")
        head_expr = _type_expr(head, generics, consts)
        children = []
        for child in _children(args):
            if child.type == "lifetime":
                continue
            if child.text.decode() in consts or child.type in {"integer_literal", "block", "boolean_literal"}:
                children.append(TypeExpr("unknown", raw=child.text.decode(), extra="const_arg"))
            else:
                children.append(_type_expr(child, generics, consts))
        return TypeExpr("generic", head=head_expr.head, args=children, raw=raw)
    if kind in {"reference_type", "pointer_type"}:
        inner = node.child_by_field_name("type")
        if inner is None:
            raise TypeContextError(f"Unsupported reference type: {raw}")
        mutable = any(c.type == "mutable_specifier" for c in node.children)
        return TypeExpr("ref" if kind == "reference_type" else "ptr",
                        args=[_type_expr(inner, generics, consts)], is_mut=mutable, raw=raw)
    if kind == "array_type":
        inner = node.child_by_field_name("element")
        size = node.child_by_field_name("length")
        if inner is None:
            raise TypeContextError(f"Unsupported array type: {raw}")
        return TypeExpr("array", args=[_type_expr(inner, generics, consts)], raw=raw,
                        extra=size.text.decode() if size is not None else "")
    if kind == "tuple_type":
        return TypeExpr("tuple", args=[_type_expr(c, generics, consts) for c in _children(node)], raw=raw)
    if kind == "function_type":
        params = node.child_by_field_name("parameters")
        ret = node.child_by_field_name("return_type")
        head = node.child_by_field_name("trait")
        arguments = [_type_expr(c, generics, consts) for c in _children(params)]
        arguments.append(_type_expr(ret, generics, consts) if ret else TypeExpr("unit", raw="()"))
        return TypeExpr("fn", head=head.text.decode() if head else "fn", args=arguments, raw=raw)
    if kind in {"abstract_type", "dynamic_type", "bounded_type"}:
        arguments = [
            _type_expr(c, generics, consts) for c in _children(node)
            if c.type not in {"lifetime", "removed_trait_bound"}
        ]
        return TypeExpr("impl" if kind == "abstract_type" else "dyn", args=arguments, raw=raw)
    raise TypeContextError(f"Unsupported source type syntax: {kind} ({raw})")


@dataclass(frozen=True)
class _Generic:
    name: str
    kind: str
    bounds: tuple[str, ...] = ()
    default: str | None = None


def _bounds(node: ts.Node | None) -> tuple[str, ...]:
    return tuple(
        c.text.decode() for c in _children(node)
        if c.type not in {"lifetime", "removed_trait_bound"}
    )


def _generics(node: ts.Node | None) -> list[_Generic]:
    result = []
    for child in _children(node):
        name = child.child_by_field_name("name")
        if name is None and child.type == "lifetime_parameter":
            name = next((c for c in _children(child) if c.type == "lifetime"), None)
        if name is None:
            raise TypeContextError(f"Unsupported source generic parameter: {child.text.decode()}")
        kind = {"type_parameter": "type", "const_parameter": "const",
                "lifetime_parameter": "lifetime"}.get(child.type)
        if kind is None:
            raise TypeContextError(f"Unsupported source generic parameter: {child.type}")
        default = child.child_by_field_name("default_type") or child.child_by_field_name("value")
        result.append(_Generic(name.text.decode(), kind,
                               _bounds(child.child_by_field_name("bounds")),
                               default.text.decode() if default else None))
    return result


def _function_header(function: FunctionSpec) -> ts.Node:
    source = f"fn __FunctionContext{function.generics_decl}() {function.where_decl} {{}}"
    tree = _parser.parse(source.encode())
    items = _children(tree.root_node)
    fn = _unwrap(items[0]) if len(items) == 1 else None
    if tree.root_node.has_error or fn is None or fn.type != "function_item":
        raise TypeContextError("Cannot parse the function's generic/where context")
    return fn


def _self_type(function: FunctionSpec) -> str:
    return function.self_type or "__DetSelf"


def parse_input_type(ty: TypeInfo | str, function: FunctionSpec) -> TypeExpr:
    """Parse the actual source type, retaining slices, references and arrays.

    TypeInfo's name is its source annotation (not its semantic kind). Reference
    flags on Param are intentionally not reintroduced: a mutable PRE binding
    may already have been lowered to a value by the native generator.
    """
    raw = ty.name if isinstance(ty, TypeInfo) else ty
    raw = _substitute_type(raw, {"Self": _self_type(function)})
    params = _generics(_function_header(function).child_by_field_name("type_parameters"))
    types = {p.name for p in params if p.kind == "type"} | {"__DetSelf"}
    consts = {p.name for p in params if p.kind == "const"}
    return _type_expr(_type_node(raw), types, consts)


@dataclass(frozen=True)
class _Site:
    source: str
    module: tuple[str, ...] = ()


@dataclass
class _Declaration:
    name: str
    site: _Site
    node: ts.Node
    attrs: list[ts.Node]
    type_def: TypeDef | None = None

    @property
    def qualified(self) -> str:
        return "::".join((*self.site.module, self.name))

    @property
    def identity(self) -> tuple[str, int]:
        return self.site.source, self.node.start_byte

    @property
    def parameters(self) -> list[_Generic]:
        return _generics(self.node.child_by_field_name("type_parameters"))


@dataclass(frozen=True)
class _FunctionSite:
    name: str
    site: _Site
    params: tuple[str, ...]
    self_type: str = ""
    trait: str = ""


class _Sources:
    def __init__(self, sources: Sources):
        if isinstance(sources, str):
            sources = (sources,)
        entries = sources.items() if isinstance(sources, Mapping) else (
            (f"<source:{i}>", text) for i, text in enumerate(sources)
        )
        self.declarations: dict[str, list[_Declaration]] = {}
        self.short: dict[str, list[_Declaration]] = {}
        self.imports: dict[_Site, dict[str, list[str]]] = {}
        self.globs: dict[_Site, list[str]] = {}
        self.functions: list[_FunctionSite] = []
        for label, text in entries:
            if not isinstance(label, str) or not isinstance(text, str):
                raise TypeContextError("Sources must be source strings, not paths or file objects")
            tree = _parser.parse(text.encode())
            self.walk(tree.root_node, _Site(label))

    def use(self, node: ts.Node, site: _Site, prefix: str = "") -> None:
        text = node.text.decode()
        if node.type == "scoped_use_list":
            path = node.child_by_field_name("path").text.decode()
            for child in _children(node.child_by_field_name("list")):
                self.use(child, site, prefix + path + "::")
        elif node.type == "use_list":
            for child in _children(node):
                self.use(child, site, prefix)
        elif node.type == "use_wildcard":
            self.globs.setdefault(site, []).append(prefix + text.removesuffix("::*").removesuffix("*"))
        else:
            if node.type == "use_as_clause":
                path = prefix + node.child_by_field_name("path").text.decode()
                name = node.child_by_field_name("alias").text.decode()
            else:
                path = prefix.rstrip("::") if text == "self" else prefix + text
                name = path.rsplit("::", 1)[-1]
            self.imports.setdefault(site, {}).setdefault(name, []).append(path)

    def walk(self, container: ts.Node, site: _Site) -> None:
        for wrapped in _children(container):
            attrs = [c for c in _children(wrapped) if c.type == "attribute_item"]
            node = _unwrap(wrapped)
            kind = node.type
            if kind in {"verus_block", "declaration_list"}:
                self.walk(node, site)
            elif kind == "mod_item":
                body = node.child_by_field_name("body")
                name = node.child_by_field_name("name")
                if body is not None and name is not None:
                    self.walk(body, _Site(site.source, (*site.module, name.text.decode())))
            elif kind in {"struct_item", "enum_item", "type_item", "union_item", "trait_item"}:
                name = node.child_by_field_name("name")
                if name is None or node.has_error:
                    raise TypeContextError(f"Malformed source type declaration in {site.source}")
                declaration = _Declaration(name.text.decode(), site, node, attrs)
                parser = {"struct_item": _parse_struct_item, "enum_item": _parse_enum_item,
                          "type_item": _parse_type_alias}.get(kind)
                if parser:
                    declaration.type_def = parser(node, list(site.module), site.source)
                    _apply_attrs(declaration.type_def, attrs)
                self.declarations.setdefault(declaration.qualified, []).append(declaration)
                self.short.setdefault(declaration.name, []).append(declaration)
                if kind == "trait_item":
                    for method in _children(node.child_by_field_name("body")):
                        self.function(_unwrap(method), site, trait=declaration.qualified)
            elif kind == "impl_item":
                target = node.child_by_field_name("type")
                for method in _children(node.child_by_field_name("body")):
                    self.function(_unwrap(method), site, self_type=target.text.decode() if target else "")
            elif kind == "use_declaration":
                argument = node.child_by_field_name("argument")
                if argument is not None:
                    self.use(argument, site)
            else:
                self.function(node, site)

    def function(self, node: ts.Node, site: _Site, self_type: str = "", trait: str = "") -> None:
        if node.type not in {"function_item", "function_signature_item"}:
            return
        name = node.child_by_field_name("name")
        params = node.child_by_field_name("parameters")
        if name is not None and params is not None:
            self.functions.append(_FunctionSite(
                name.text.decode(), site, tuple(p.name for p in _extract_params(params)), self_type, trait,
            ))

    def function_site(self, function: FunctionSpec) -> _Site | None:
        short = function.name.rsplit("::", 1)[-1]
        candidates = [f for f in self.functions if f.name == short
                      and f.params == tuple(p.name for p in function.params)]
        if function.self_type:
            candidates = [f for f in candidates if f.self_type and _key(f.self_type) == _key(function.self_type)]
        elif function.trait_name:
            head, _ = _head_and_args(_type_node(function.trait_name))
            candidates = [f for f in candidates if f.trait == head or f.trait.rsplit("::", 1)[-1] == head]
        else:
            candidates = [f for f in candidates if not f.trait and not f.self_type]
        if "::" in function.name:
            module = tuple(function.name.split("::")[:-1])
            candidates = [f for f in candidates if f.site.module == module]
        sites = {f.site for f in candidates}
        if len(sites) > 1:
            raise TypeContextError(f"Ambiguous source context for function {function.name}")
        return next(iter(sites)) if sites else None

    @staticmethod
    def absolute(path: str, site: _Site | None) -> str:
        parts = path.removeprefix("::").split("::")
        if parts[0] == "crate":
            return "::".join(parts[1:])
        module = list(site.module) if site else []
        if parts[0] == "self":
            return "::".join(module + parts[1:])
        if parts[0] == "super":
            while parts and parts[0] == "super":
                if not module:
                    raise TypeContextError(f"Cannot resolve {path} outside its source module")
                module.pop()
                parts.pop(0)
            return "::".join(module + parts)
        return path.removeprefix("::")

    def lookup(
        self, path: str, site: _Site | None, seen: frozenset[tuple[_Site | None, str]] = frozenset(),
    ) -> tuple[str, _Declaration | None]:
        marker = site, path
        if marker in seen:
            raise TypeContextError(f"Cyclic source import through {path}")
        seen = seen | {marker}
        if path.startswith(("::", "crate::", "self::", "super::")):
            qualified = self.absolute(path, site)
            candidates = self.declarations.get(qualified, [])
        else:
            qualified = "::".join((*site.module, path)) if site else path
            candidates = self.declarations.get(qualified, [])
            first, *rest = path.split("::")
            imports = self.imports.get(site, {}).get(first, [])
            if imports:
                if candidates or len(set(imports)) != 1:
                    raise TypeContextError(f"Ambiguous source import for {path}")
                target = self.absolute(imports[0], site)
                if rest:
                    target += "::" + "::".join(rest)
                return self.lookup("::" + target, site, seen)
            if not candidates and site:
                globbed = []
                for glob in self.globs.get(site, []):
                    target = self.absolute(glob, site) + "::" + path
                    if target in self.declarations or target in _CANONICAL_VIEW:
                        globbed.append(target)
                if len(set(globbed)) > 1:
                    raise TypeContextError(f"Ambiguous wildcard source import for {path}")
                if globbed:
                    return self.lookup("::" + globbed[0], site, seen)
            if not candidates:
                qualified = path
                candidates = self.declarations.get(path, [])
                if not candidates and site is None and "::" not in path:
                    candidates = self.short.get(path, [])
        if len(candidates) > 1:
            locations = ", ".join(f"{c.site.source}:{c.node.start_point[0] + 1}" for c in candidates)
            raise TypeContextError(f"Ambiguous source type/trait {path}: {locations}")
        return (candidates[0].qualified, candidates[0]) if candidates else (qualified, None)

    def qualify_type(
        self, text: str, site: _Site | None, protected: set[str], *, absolute: bool = False,
    ) -> str:
        """Keep caller type arguments in their own module when instantiating."""
        root = _type_node(text)
        edits = []

        def visit(node: ts.Node) -> None:
            if node.type in {"type_identifier", "scoped_type_identifier"}:
                name = node.text.decode()
                if name.split("::", 1)[0] in protected:
                    return
                qualified, declaration = self.lookup(name, site)
                if declaration is not None:
                    target = f"crate::{qualified}" if absolute else qualified
                    if name != target:
                        edits.append((node.start_byte - root.start_byte, node.end_byte - root.start_byte, target))
                return
            if node.type in {"macro_invocation", "higher_ranked_trait_bound"}:
                raise TypeContextError("Unsupported binding syntax in a generic type argument")
            for child in _children(node):
                if child.type not in {"lifetime", "identifier", "block"}:
                    visit(child)

        visit(root)
        data = root.text
        for start, end, replacement in sorted(edits, reverse=True):
            data = data[:start] + replacement.encode() + data[end:]
        return data.decode()


def _head_and_args(node: ts.Node) -> tuple[str, list[ts.Node]]:
    if node.type in {"type_identifier", "scoped_type_identifier"}:
        return node.text.decode(), []
    if node.type in {"generic_type", "generic_type_with_turbofish"}:
        head = node.child_by_field_name("type")
        return head.text.decode(), _children(node.child_by_field_name("type_arguments"))
    return "", []


def _instantiate(
    declaration: _Declaration, args: list[ts.Node], *,
    context: _Sources | None = None, site: _Site | None = None,
    protected: set[str] | None = None,
) -> dict[str, str]:
    lifetimes = [a.text.decode() for a in args if a.type == "lifetime"]
    values = [a.text.decode() for a in args if a.type != "lifetime"]
    bindings: dict[str, str] = {}
    for param in declaration.parameters:
        actuals = lifetimes if param.kind == "lifetime" else values
        value_site = site
        if actuals:
            value = actuals.pop(0)
        elif param.kind == "lifetime":
            value = "'_"
        elif param.default is not None:
            value = _substitute_type(param.default, bindings) if param.kind == "type" else param.default
            value_site = declaration.site
        else:
            raise TypeContextError(f"Missing generic argument {param.name} for {declaration.qualified}")
        if context is not None and param.kind == "type":
            value = context.qualify_type(
                value, value_site, protected or set(),
                absolute=value_site is None or value_site.module != declaration.site.module,
            )
        bindings[param.name] = value
    if lifetimes or values:
        raise TypeContextError(f"Too many generic arguments for {declaration.qualified}")
    return bindings


class _TypeResolver:
    def __init__(self, sources: _Sources, function: FunctionSpec):
        self.sources = sources
        self.function = function
        self.site = sources.function_site(function)
        header = _function_header(function)
        self.generics = {p.name for p in _generics(header.child_by_field_name("type_parameters"))}

    def resolve(
        self, info: TypeInfo, site: _Site | None,
        aliases: tuple[tuple[str, int], ...] = (),
        nominal: frozenset[tuple[str, int]] = frozenset(),
        generic_names: frozenset[str] = frozenset(),
    ) -> TypeInfo:
        try:
            node = _type_node(info.name)
        except TypeContextError:
            if info.name not in {"", "?"}:
                raise
            return deepcopy(info)
        head, args = _head_and_args(node)
        declaration = None
        if head and head not in (self.generics | set(generic_names) | {"Self", "__DetSelf"}):
            _, declaration = self.sources.lookup(head, site)
        if declaration and declaration.node.type == "type_item":
            if declaration.identity in aliases:
                raise TypeContextError(f"Cyclic source alias through {declaration.qualified}")
            bindings = _instantiate(declaration, args, context=self.sources, site=site,
                                    protected=self.generics | set(generic_names))
            target = declaration.node.child_by_field_name("type")
            if target is None:
                raise TypeContextError(f"Alias {declaration.qualified} has no target")
            expanded = _substitute_type(target.text.decode(), bindings)
            target_node = _type_node(expanded)
            if target_node.type == "reference_type" and any(
                c.type == "mutable_specifier" for c in target_node.children
            ):
                raise TypeContextError("Aliases hiding mutable references require explicit state lowering")
            resolved = self.resolve(_parse_type_node(target_node), declaration.site,
                                    (*aliases, declaration.identity), nominal, generic_names)
            resolved.name = info.name
            return resolved

        result = deepcopy(info)
        result.type_args = [self.resolve(a, site, aliases, nominal, generic_names) for a in info.type_args]
        if declaration and declaration.type_def and declaration.type_def.kind in {"struct", "enum"}:
            if declaration.identity in nominal:
                return TypeInfo(TypeKind.UNKNOWN, info.name, type_args=result.type_args)
            if info.kind == TypeKind.UNKNOWN:
                result = self.materialize(declaration, info.name, args, nominal, site)
            nominal = nominal | {declaration.identity}
            site = declaration.site
            generic_names = generic_names | {p.name for p in declaration.parameters}
        result.fields = [
            FieldInfo(f.name, self.resolve(f.type, site, (), nominal, generic_names)) for f in result.fields
        ]
        result.variants = [
            VariantInfo(v.name, self.resolve(v.inner, site, (), nominal, generic_names) if v.inner else None,
                        v.discriminant, v.struct_form)
            for v in result.variants
        ]
        if result.spec_view is not None:
            result.spec_view = self.resolve(result.spec_view, site, (), nominal, generic_names)
        return result

    def materialize(
        self, declaration: _Declaration, spelling: str, args: list[ts.Node],
        nominal: frozenset[tuple[str, int]], site: _Site | None,
    ) -> TypeInfo:
        definition = declaration.type_def
        bindings = _instantiate(declaration, args, context=self.sources, site=site,
                                protected=self.generics)
        attributes = parse_item_attrs(declaration.attrs)

        def expanded_type(text: str) -> TypeInfo:
            return self.resolve(_parse_type_node(_type_node(text)), declaration.site,
                                nominal=nominal | {declaration.identity})

        def field_type(text: str) -> TypeInfo:
            return expanded_type(_substitute_type(text, bindings))

        type_args = [expanded_type(bindings[p.name]) for p in declaration.parameters if p.kind == "type"]
        if definition.kind == "struct":
            return TypeInfo(
                TypeKind.STRUCT, spelling,
                fields=[FieldInfo(f.name, field_type(f.type_text)) for f in definition.fields],
                type_args=type_args,
                is_opaque=attributes.is_external_body, is_ext_equal=attributes.is_ext_equal,
            )
        variants = []
        for variant in definition.variants:
            if variant.kind == "tuple" and len(variant.fields) > 1:
                raise TypeContextError("Native aliases to multi-field tuple variants are unsupported")
            inner = None
            if variant.fields:
                if variant.kind == "struct":
                    inner = TypeInfo(TypeKind.STRUCT, f"{declaration.qualified}::{variant.name}",
                                     fields=[FieldInfo(f.name, field_type(f.type_text)) for f in variant.fields])
                else:
                    inner = field_type(variant.fields[0].type_text)
            variants.append(VariantInfo(variant.name, inner, struct_form=variant.kind == "struct"))
        return TypeInfo(TypeKind.ENUM, spelling, variants=variants, type_args=type_args,
                        is_ext_equal=attributes.is_ext_equal)


def resolve_function_types(function: FunctionSpec, sources: Sources) -> FunctionSpec:
    """Return a detached model with recursively resolved alias kinds.

    Parameter, field, return and container annotations retain their alias
    spelling, so generated Rust still refers to the exact original sources.
    Clauses, generics and parameter modes are not rewritten.
    """
    context = _Sources(sources)
    resolver = _TypeResolver(context, function)
    result = deepcopy(function)
    for param in result.params:
        param.type = resolver.resolve(param.type, resolver.site)
    result.return_type = resolver.resolve(result.return_type, resolver.site)
    for name, info in tuple(result.type_defs.items()):
        _, declaration = context.lookup(name, resolver.site)
        site = declaration.site if declaration else resolver.site
        result.type_defs[name] = resolver.resolve(info, site)
    return result


@dataclass(frozen=True)
class _ViewCandidate:
    identity: tuple
    evidence: SourceBoundView


def _trait_bounds(declaration: _Declaration) -> list[str]:
    bounds = list(_bounds(declaration.node.child_by_field_name("bounds")))
    where = next((c for c in _children(declaration.node) if c.type == "where_clause"), None)
    for predicate in _children(where):
        left = predicate.child_by_field_name("left")
        if left is not None and left.text == b"Self":
            bounds.extend(_bounds(predicate.child_by_field_name("bounds")))
    return bounds


def _view_return(method: ts.Node) -> str | None:
    name = method.child_by_field_name("name")
    if name is None or name.text != b"view":
        return None
    mode = next((c for c in _children(method) if c.type == "function_mode"), None)
    params = _children(method.child_by_field_name("parameters"))
    if mode is None or mode.text != b"spec":
        return None
    if len(params) != 1 or params[0].type != "self_parameter":
        return None
    if any(c.type == "mutable_specifier" for c in params[0].children):
        raise TypeContextError("A source-bound view requires a mutable receiver")
    if method.child_by_field_name("type_parameters") or any(
        c.type == "where_clause" for c in _children(method)
    ):
        raise TypeContextError("A source-bound view has additional method-level generic obligations")
    returned = method.child_by_field_name("return_type")
    if returned is None:
        raise TypeContextError("A source-bound view has no return type")
    if returned.type == "named_return_type":
        children = _children(returned)
        returned = children[-1] if children else None
    if returned is None:
        raise TypeContextError("Cannot parse a source-bound view return type")
    return returned.text.decode()


def _bound_candidates(
    context: _Sources, receiver: str, bound: str, site: _Site | None,
    chain: tuple[str, ...] = (), stack: tuple[tuple[str, int], ...] = (),
    protected: frozenset[str] = frozenset(),
) -> list[_ViewCandidate]:
    node = _type_node(bound)
    head, args = _head_and_args(node)
    if not head:
        # Fn(...) bounds do not establish a zero-argument spec view.
        if node.type == "function_type":
            return []
        raise TypeContextError(f"Unsupported source trait bound: {bound}")
    qualified, declaration = context.lookup(head, site)
    trail = (*chain, bound)
    if declaration is None:
        if qualified not in _CANONICAL_VIEW:
            return []
        if any(arg.type != "type_binding" for arg in args):
            raise TypeContextError("The canonical View bound has unexpected positional arguments")
        evidence = SourceBoundView(
            viewed_type=f"<{receiver} as View>::V",
            rationale=f"Source bound {receiver}: {' -> '.join(trail)} establishes vstd::View",
            trait="vstd::view::View", source_file=site.source if site else "<function>",
            canonical_view=True,
        )
        return [_ViewCandidate(("vstd::view::View",), evidence)]
    if declaration.node.type != "trait_item":
        raise TypeContextError(f"Source bound {bound} does not resolve to a trait")
    if declaration.identity in stack:
        raise TypeContextError(f"Cyclic source trait hierarchy through {declaration.qualified}")
    bindings = _instantiate(declaration, args, context=context, site=site, protected=set(protected))
    bindings["Self"] = receiver
    instance = declaration.qualified
    if declaration.parameters:
        instance += "<" + ", ".join(bindings[p.name] for p in declaration.parameters) + ">"
    candidates = []
    methods = [_unwrap(c) for c in _children(declaration.node.child_by_field_name("body"))]
    associated = {
        c.child_by_field_name("name").text.decode()
        for c in methods if c.type == "associated_type" and c.child_by_field_name("name") is not None
    }
    for method in methods:
        if method.type not in {"function_item", "function_signature_item"}:
            continue
        returned = _view_return(method)
        if returned is None:
            continue
        viewed = _substitute_type(
            returned, bindings,
            associated={
                name: f"<{receiver} as {'crate::' if declaration.site.module else ''}{instance}>::{name}"
                for name in associated
            },
        )
        viewed = context.qualify_type(viewed, declaration.site, set(protected),
                                      absolute=bool(declaration.site.module))
        signature = method.text.decode()
        body = method.child_by_field_name("body")
        if body is not None:
            signature = method.text[:body.start_byte - method.start_byte].decode().strip()
        line = method.start_point[0] + 1
        candidates.append(_ViewCandidate(
            (declaration.identity, _key(instance), method.start_byte),
            SourceBoundView(
                viewed, f"Source bound {receiver}: {' -> '.join(trail)}; "
                f"{declaration.qualified}::view ({declaration.site.source}:{line})",
                instance, declaration.site.source, line, signature,
            ),
        ))
    for inherited in _trait_bounds(declaration):
        inherited = _substitute_type(inherited, bindings)
        candidates.extend(_bound_candidates(
            context, receiver, inherited, declaration.site, trail, (*stack, declaration.identity),
            protected,
        ))
    return candidates


def bound_views(function: FunctionSpec, sources: Sources) -> dict[str, SourceBoundView]:
    """Establish callable ``.view()`` methods from actual source bounds.

    A custom trait method stays a custom method: this does not add ``T: View``
    or manufacture an impl. Canonical View supertraits are followed
    transitively, including instantiated generic supertraits.
    """
    context = _Sources(sources)
    site = context.function_site(function)
    header = _function_header(function)
    protected = frozenset(
        p.name for p in _generics(header.child_by_field_name("type_parameters"))
    ) | {"__DetSelf"}
    constraints: dict[str, list[str]] = {}
    for param in _generics(header.child_by_field_name("type_parameters")):
        if param.kind == "type":
            constraints.setdefault(param.name, []).extend(param.bounds)
    where = next((c for c in _children(header) if c.type == "where_clause"), None)
    for predicate in _children(where):
        left = predicate.child_by_field_name("left")
        if left is not None and left.type != "lifetime":
            constraints.setdefault(left.text.decode(), []).extend(_bounds(predicate.child_by_field_name("bounds")))
    if function.trait_name and not function.self_type:
        constraints.setdefault("Self", []).append(function.trait_name)
    result = {}
    for subject, bounds in constraints.items():
        receiver = _substitute_type(subject, {"Self": _self_type(function)})
        candidates: dict[tuple, SourceBoundView] = {}
        for bound in bounds:
            bound = _substitute_type(bound, {"Self": _self_type(function)})
            for candidate in _bound_candidates(context, receiver, bound, site, protected=protected):
                candidates.setdefault(candidate.identity, candidate.evidence)
        if len(candidates) > 1:
            owners = ", ".join(entry.trait for entry in candidates.values())
            raise TypeContextError(f"Ambiguous source view method for {receiver}: {owners}")
        if candidates:
            evidence = next(iter(candidates.values()))
            if receiver in result and result[receiver] != evidence:
                raise TypeContextError(f"Ambiguous source view evidence for {receiver}")
            result[receiver] = evidence
            if subject == "Self" and not function.self_type:
                result["Self"] = evidence
    return result
