"""Source-only aliases, trait-bound view methods, and actual type syntax."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from specdet.adapters.verus.native.codegen.gen_det import build_det_check_spec, render_template
from specdet.adapters.verus.native.codegen.pairing import InputPair, pair_identifiers
from specdet.adapters.verus.native.extract.extractor import extract_spec
from specdet.adapters.verus.native.extract.types import FunctionSpec, Param, TypeInfo, TypeKind
from specdet.adapters.verus.native.view.impl_scanner import ImplScan
from specdet.adapters.verus.native.view.registry import SourceBoundView, ViewRegistry
from specdet.adapters.verus.native.source import inject_into_source
from specdet.adapters.verus.type_context import (
    TypeContextError, bound_views, parse_input_type, resolve_function_types,
)


FIXTURES = Path(__file__).parent / "fixtures" / "verusage"


def function(type_name="T", generics="<T>") -> FunctionSpec:
    ty = TypeInfo(TypeKind.UNKNOWN, type_name)
    return FunctionSpec("f", [Param("x", ty)], ty, [], ["ret == x"],
                        result_binding="ret", generics_decl=generics)


class SourceAliasTests(unittest.TestCase):
    def test_corpus_vaddr_resolves_in_return_struct_ghost_seq_and_view(self):
        path = FIXTURES / "atmosphere/va_range_index.rs"
        source = path.read_text()
        original = extract_spec(source, "index")
        before = deepcopy(original)
        resolved = resolve_function_types(original, {str(path): source})
        self.assertEqual(resolved.return_type.kind, TypeKind.USIZE)
        self.assertEqual(resolved.return_type.name, "VAddr")
        record = resolved.params[0].type
        start = next(f.type for f in record.fields if f.name == "start")
        ghost = next(f.type for f in record.fields if f.name == "view")
        for alias in (start, ghost.type_args[0].type_args[0], record.spec_view.type_args[0]):
            self.assertEqual((alias.kind, alias.name), (TypeKind.USIZE, "VAddr"))
        self.assertEqual(original, before)
        self.assertEqual(original.return_type.kind, TypeKind.UNKNOWN)
        self.assertIsNot(resolved, original)
        self.assertEqual((resolved.requires, resolved.ensures), (original.requires, original.ensures))
        self.assertIn("r1: VAddr", build_det_check_spec(resolved).det_check_template)

    def test_nested_generic_aliases_preserve_every_annotation(self):
        source = """verus! {
type Address = usize;
type Values<T> = Seq<T>;
struct Packet { values: Values<Address> }
fn f(x: &Packet) -> (ret: Address) ensures ret == 0, {}
}"""
        spec = extract_spec(source, "f")
        resolved = resolve_function_types(spec, [source])
        field = resolved.params[0].type.fields[0].type
        self.assertEqual((field.name, field.kind), ("Values<Address>", TypeKind.SEQ))
        self.assertEqual((field.type_args[0].name, field.type_args[0].kind), ("Address", TypeKind.USIZE))
        self.assertEqual(resolved.return_type.name, "Address")
        self.assertEqual(spec.params[0].type.fields[0].type.kind, TypeKind.UNKNOWN)

    def test_alias_chain_to_a_source_struct_is_materialized_without_renaming_it(self):
        source = "type Number = u8; struct Record { value: Number } type Handle = Record; type Outer = Handle;"
        spec = function("Outer", "")
        result = resolve_function_types(spec, [source])
        self.assertEqual((result.return_type.kind, result.return_type.name), (TypeKind.STRUCT, "Outer"))
        self.assertEqual(result.return_type.fields[0].type.kind, TypeKind.U8)
        self.assertEqual(result.return_type.fields[0].type.name, "Number")

    def test_explicit_module_graph_resolves_imports_without_filename_guessing(self):
        sources = {
            "src/api.rs": "use crate::internal::state::State; fn f() -> (ret: State) ensures true, {}",
            "unusual/shared.inc.rs": "pub struct State { value: int }",
        }
        modules = {"src/api.rs": "api", "unusual/shared.inc.rs": "internal::state"}
        result = resolve_function_types(extract_spec(sources["src/api.rs"], "f"), sources, modules=modules)
        self.assertEqual(result.return_type.kind, TypeKind.STRUCT)
        self.assertEqual(result.return_type.fields[0].type.kind, TypeKind.INT)

    def test_included_declarations_share_imports_with_their_owner(self):
        sources = {
            "owner.rs": "use crate::state::State;",
            "included.rs": "fn f() -> (ret: State) ensures true, {}",
            "state.rs": "pub struct State { value: int }",
        }
        modules = {"owner.rs": "api", "included.rs": "api", "state.rs": "state"}
        result = resolve_function_types(extract_spec(sources["included.rs"], "f"), sources, modules=modules)
        self.assertEqual(result.return_type.kind, TypeKind.STRUCT)

    def test_source_identity_replaces_fields_recovered_by_short_name(self):
        source = """verus! {
mod first { pub struct State {} }
mod second { pub struct State { pub value: u8, pub preserved: bool } }
mod api {
    use crate::second::State;
    pub fn f() -> (r: State) ensures true, {}
}
}"""
        original = extract_spec(source, "f")
        original.return_type = TypeInfo(TypeKind.STRUCT, "State", fields=[])
        resolved = resolve_function_types(original, [source])
        self.assertEqual([field.name for field in resolved.return_type.fields], ["value", "preserved"])
        self.assertEqual(resolved.return_type.fields[1].type.kind, TypeKind.BOOL)

    def test_direct_generic_enum_payloads_are_instantiated_from_their_source(self):
        source = "verus! { enum E<T> { Value(T), Empty } fn f() -> (r: E<u8>) ensures true, {} }"
        resolved = resolve_function_types(extract_spec(source, "f"), [source])
        self.assertEqual(resolved.return_type.variants[0].inner.kind, TypeKind.U8)

    def test_alias_materialization_preserves_opaque_and_ext_equal_attributes(self):
        source = "#[verifier::ext_equal] struct Model { n: u8 } type Alias = Model;"
        result = resolve_function_types(function("Alias", ""), [source])
        self.assertTrue(result.return_type.is_ext_equal)
        source = "#[verifier::external_body] struct Model { n: u8 } type Alias = Model;"
        result = resolve_function_types(function("Alias", ""), [source])
        self.assertTrue(result.return_type.is_opaque)

    def test_type_and_const_generic_alias_arguments_and_defaults(self):
        source = "type Bytes<T = u8, const N: usize = 4> = [T; N];"
        for name in ("Bytes", "Bytes<u16, 8>"):
            with self.subTest(name=name):
                resolved = resolve_function_types(function(name, ""), [source])
                self.assertEqual(resolved.return_type.name, name)
                self.assertEqual(resolved.return_type.kind, TypeKind.SEQ)
                self.assertEqual(resolved.return_type.type_args[0].kind,
                                 TypeKind.U8 if name == "Bytes" else TypeKind.U16)

    def test_generic_parameters_shadow_source_aliases_of_the_same_name(self):
        source = "type T = usize; type Identity<U> = U;"
        spec = function("Identity<T>", "<T>")
        result = resolve_function_types(spec, [source])
        self.assertEqual((result.return_type.kind, result.return_type.name),
                         (TypeKind.UNKNOWN, "Identity<T>"))

    def test_aliases_resolve_in_the_declaring_module_not_an_unrelated_module(self):
        source = """verus! {
mod a { pub type Address = usize; pub struct Record { pub value: Address } }
mod b { pub type Address = bool; fn f(x: crate::a::Record) -> (ret: crate::a::Address) ensures true, {} }
}"""
        spec = extract_spec(source, "f")
        # The exact qualified input remains distinguishable even if the native
        # extractor has not populated this source struct.
        spec.params[0].type = TypeInfo(TypeKind.STRUCT, "crate::a::Record", fields=[])
        resolved = resolve_function_types(spec, [source])
        self.assertEqual((resolved.return_type.kind, resolved.return_type.name), (TypeKind.USIZE, "crate::a::Address"))

    def test_caller_generic_arguments_keep_their_qualified_source_context(self):
        source = """verus! {
mod a { pub type Value = bool; pub type Items<T> = Seq<T>; }
mod b { pub type Value = usize; fn f(x: crate::a::Items<Value>) -> (ret: crate::a::Items<Value>) ensures true, {} }
}"""
        result = resolve_function_types(extract_spec(source, "f"), [source])
        self.assertEqual(result.return_type.kind, TypeKind.SEQ)
        self.assertEqual(result.return_type.type_args[0].kind, TypeKind.USIZE)
        self.assertEqual(result.return_type.name, "crate::a::Items<Value>")

    def test_explicit_import_aliases_and_qualified_aliases_are_resolved(self):
        source = """verus! {
mod a { pub type Address = usize; }
use crate::a::Address as Addr;
fn f(x: Addr) -> (ret: Addr) ensures ret == x, {}
}"""
        spec = extract_spec(source, "f")
        result = resolve_function_types(spec, [source])
        self.assertEqual(result.return_type.name, "Addr")
        self.assertEqual(result.return_type.kind, TypeKind.USIZE)

    def test_ambiguous_and_cyclic_aliases_are_explicit_errors(self):
        cases = [
            (["type A = usize;", "type A = bool;"], "A"),
            (["mod a { type A = usize; } mod b { type A = bool; }"], "A"),
            (["#[cfg(first)] type A = usize; #[cfg(second)] type A = bool;"], "A"),
            (["type A = B; type B = A;"], "A"),
            (["type A = Seq<A>;"], "A"),
            (["type A<T> = B<T>; type B<T> = A<T>;"], "A<u8>"),
        ]
        for sources, name in cases:
            with self.subTest(sources=sources, name=name), self.assertRaises(TypeContextError):
                resolve_function_types(function(name, ""), sources)

    def test_nominal_recursion_does_not_become_an_alias_cycle_or_infinite_model(self):
        source = "type Link = Node; struct Node { next: Option<Box<Link>> }"
        result = resolve_function_types(function("Link", ""), [source])
        self.assertEqual(result.return_type.kind, TypeKind.STRUCT)
        result.return_type.to_dict()

    def test_no_filesystem_lookup_or_input_mutation(self):
        spec = function("Address", "")
        original = deepcopy(spec)
        with patch.object(Path, "read_text", side_effect=AssertionError("implicit source lookup")):
            resolved = resolve_function_types(spec, ["type Address = usize;"])
            views = bound_views(function(), ["trait NoView {}"])
        self.assertEqual(resolved.return_type.kind, TypeKind.USIZE)
        self.assertEqual(views, {})
        self.assertEqual(spec, original)


class BoundViewTests(unittest.TestCase):
    def test_corpus_own_view_method_does_not_imply_vstd_view(self):
        path = FIXTURES / "storage/region_sizes.rs"
        source = path.read_text()
        spec = extract_spec(source, "get_region_sizes")
        before = deepcopy(spec)
        evidence = bound_views(spec, {str(path): source})
        entry = evidence["PMRegions"]
        self.assertEqual(entry.viewed_type, "PersistentMemoryRegionsView")
        self.assertFalse(entry.canonical_view)
        self.assertEqual(entry.trait, "PersistentMemoryRegions")
        self.assertIn(str(path), entry.rationale)
        registry = ViewRegistry.from_sources(source, bound_views=evidence)
        resolution = registry.resolve(parse_input_type(spec.params[0].type, spec))
        self.assertEqual(resolution.layer, "source-bound")
        self.assertEqual(resolution.view_expr("x"), "(x).view()")
        self.assertIsNone(resolution.prelude_decl)
        self.assertEqual(spec, before)
        self.assertNotIn("PMRegions: View", spec.generics_decl)

    def test_corpus_real_supertrait_establishes_canonical_associated_view(self):
        path = FIXTURES / "anvil-library/vec_filter.rs"
        source = path.read_text()
        spec = extract_spec(source, "vec_filter")
        # Isolate the inherited evidence, rather than relying on the redundant
        # direct View bound which also exists in this source function.
        inherited = replace(spec, generics_decl="<V: VerusClone + Sized>")
        view = bound_views(inherited, {str(path): source})["V"]
        self.assertEqual(view.viewed_type, "<V as View>::V")
        self.assertTrue(view.canonical_view)
        self.assertIn("VerusClone -> View", view.rationale)
        self.assertNotIn("V: View", inherited.generics_decl)

    def test_project_style_trait_names_without_source_evidence_are_not_views(self):
        spec = function(generics="<T: VerusClone + PersistentMemoryRegions>")
        self.assertEqual(bound_views(spec, []), {})
        self.assertEqual(bound_views(spec, [
            "trait VerusClone: Sized {} trait PersistentMemoryRegions: Sized {}"
        ]), {})

    def test_generic_transitive_own_view_return_is_instantiated(self):
        source = """verus! {
trait Base<A> { spec fn view(&self) -> Seq<A>; }
trait Middle<B>: Base<Vec<B>> {}
trait Outer<C>: Middle<C> {}
}"""
        spec = function(generics="<U, T: Outer<U>>")
        entry = bound_views(spec, [source])["T"]
        self.assertEqual(entry.viewed_type, "Seq<Vec<U>>")
        self.assertEqual(entry.trait, "Base<Vec<U>>")
        self.assertFalse(entry.canonical_view)

    def test_transitive_canonical_diamond_is_one_view_not_ambiguous(self):
        source = "trait Base: View {} trait A: Base {} trait B: Base {} trait Both<T>: A + B {}"
        spec = function(generics="<T: Both<u8> + View>")
        evidence = bound_views(spec, [source])["T"]
        self.assertEqual(evidence.viewed_type, "<T as View>::V")
        self.assertTrue(evidence.canonical_view)

    def test_where_bounds_and_self_supertrait_where_clauses_are_followed(self):
        source = "trait Parent where Self: View {}"
        spec = replace(function(generics="<T, U>"), where_decl="where T: Parent, U: View<V = int>")
        evidence = bound_views(spec, [source])
        self.assertEqual(evidence["T"].viewed_type, "<T as View>::V")
        self.assertEqual(evidence["U"].viewed_type, "<U as View>::V")

    def test_trait_self_is_available_under_both_source_and_native_names(self):
        source = """verus! {
trait Storage {
    spec fn view(&self) -> Seq<u8>;
    fn copy(&self) -> (ret: Self) ensures ret.view() == self.view();
}
}"""
        spec = extract_spec(source, "copy")
        evidence = bound_views(spec, [source])
        self.assertEqual(evidence["Self"], evidence["__DetSelf"])
        self.assertEqual(evidence["__DetSelf"].viewed_type, "Seq<u8>")
        self.assertEqual(parse_input_type("Self", spec).head, "__DetSelf")
        registry = ViewRegistry.from_sources(source, bound_views=evidence)
        det = build_det_check_spec(spec, view_registry=registry)
        self.assertIn("__DetSelf: Storage", det.equal_fn_def)
        self.assertIn("(r1).view() =~= (r2).view()", det.equal_fn_def)
        self.assertNotIn("__DetSelf: View", det.equal_fn_def)

    def test_canonical_trait_self_has_the_native_associated_return_type(self):
        source = "trait Observed: View { fn copy(&self) -> (ret: Self) ensures ret@ == self@; }"
        spec = extract_spec(source, "copy")
        evidence = bound_views(spec, [source])
        self.assertEqual(evidence["__DetSelf"].viewed_type, "<__DetSelf as View>::V")

    def test_own_associated_return_does_not_capture_caller_parameter_names(self):
        source = "trait Own<T> { type V; spec fn view(&self) -> (Self::V, T); }"
        spec = function(generics="<T: Own<int>>")
        evidence = bound_views(spec, [source])["T"]
        self.assertEqual(evidence.viewed_type, "(<T as Own<int>>::V, int)")
        self.assertFalse(evidence.canonical_view)

    def test_qualified_traits_imports_and_module_return_types_are_preserved(self):
        source = """verus! {
mod a { pub struct Model {} pub trait Inspect { spec fn view(&self) -> Model; } }
mod b { pub trait Inspect {} }
use crate::a::Inspect as InspectA;
fn f<T: InspectA>(x: T) -> (ret: T) ensures ret.view() == x.view(), { x }
}"""
        spec = extract_spec(source, "f")
        evidence = bound_views(spec, [source])["T"]
        self.assertEqual(evidence.trait, "a::Inspect")
        self.assertEqual(evidence.viewed_type, "crate::a::Model")
        self.assertIn("a::Inspect::view", evidence.rationale)

    def test_a_source_trait_named_view_is_not_assumed_to_be_vstd_view(self):
        source = "trait View { spec fn view(&self) -> int; }"
        entry = bound_views(function(generics="<T: View>"), [source])["T"]
        self.assertFalse(entry.canonical_view)
        self.assertEqual(entry.viewed_type, "int")
        self.assertEqual(
            bound_views(function(generics="<T: other::View>"), []), {},
        )

    def test_inherited_view_is_used_for_output_equality_without_extra_bounds(self):
        source = "trait Parent: View {} trait Child<A>: Parent {}"
        spec = function(generics="<T: Child<u8>>")
        registry = ViewRegistry.from_sources(source, bound_views=bound_views(spec, [source]))
        before = deepcopy(spec)
        det = build_det_check_spec(spec, view_registry=registry)
        self.assertIn("(r1).view() =~= (r2).view()", det.equal_fn_def)
        self.assertIn("<T: Child<u8>>", det.equal_fn_def)
        self.assertNotIn("impl View", det.equal_fn_def)
        self.assertNotIn("T: View", det.equal_fn_def)
        self.assertEqual(spec, before)

    def test_conflicting_methods_declarations_and_trait_cycles_are_errors(self):
        cases = [
            ("trait A { spec fn view(&self) -> int; } trait B { spec fn view(&self) -> int; }",
             "<T: A + B>"),
            ("trait A { spec fn view(&self) -> int; } trait A: View {}", "<T: A>"),
            ("mod a { trait A: View {} } mod b { trait A: View {} }", "<T: A>"),
            ("trait A: B {} trait B: A {}", "<T: A>"),
            ("trait A: View { spec fn view(&self) -> int; }", "<T: A>"),
        ]
        for source, generics in cases:
            with self.subTest(source=source), self.assertRaises(TypeContextError):
                bound_views(function(generics=generics), [source])

    def test_nonview_methods_are_not_mistaken_for_spec_projections(self):
        for source in (
            "trait A { fn view(&self) -> u8; }",
            "trait A { spec fn view(&self, extra: int) -> int; }",
            "trait A { spec fn view() -> int; }",
        ):
            with self.subTest(source=source):
                self.assertEqual(bound_views(function(generics="<T: A>"), [source]), {})
        with self.assertRaises(TypeContextError):
            bound_views(function(generics="<T: A>"),
                        ["trait A { spec fn view<U>(&self) -> Seq<U>; }"])

    def test_registry_does_not_consult_bound_views_unless_explicitly_supplied(self):
        source = "trait Parent: View {}"
        spec = function(generics="<T: Parent>")
        expr = parse_input_type("T", spec)
        self.assertEqual(ViewRegistry.from_sources(source).resolve(expr).layer, "uncovered")
        registry = ViewRegistry.from_sources(source, bound_views=bound_views(spec, [source]))
        self.assertEqual(registry.resolve(expr).layer, "source-bound")
        self.assertEqual(registry.equal_expr("a", "b", expr), "((a).view() =~= (b).view())")
        self.assertEqual(registry.resolve(parse_input_type("other::T", spec)).layer, "uncovered")
        self.assertEqual(registry.accepted_views, {})

    def test_registry_requires_explicit_source_method_evidence(self):
        with self.assertRaisesRegex(ValueError, "evidence"):
            ViewRegistry({}, ImplScan("<memory>"), bound_views={"T": {"viewed_type": "int"}})
        evidence = SourceBoundView("int", "source trait A::view", "A")
        registry = ViewRegistry({}, ImplScan("<memory>"), bound_views={"T :: Item": evidence})
        self.assertEqual(registry.resolve(parse_input_type("T::Item", function())).layer, "source-bound")


class ActualTypeSyntaxTests(unittest.TestCase):
    def test_slices_references_fixed_arrays_and_vectors_keep_their_real_shape(self):
        spec = function(generics="<'a, T, const N: usize>")
        cases = [
            ("[u8]", "array", "", "", "u8"),
            ("[u8; N]", "array", "", "N", "u8"),
            ("&[u8]", "ref", "", "", ""),
            ("&'a mut [u8; 4]", "ref", "", "", ""),
            ("Vec<T>", "generic", "Vec", "", "T"),
        ]
        for raw, kind, head, extra, inner in cases:
            with self.subTest(raw=raw):
                expr = parse_input_type(TypeInfo(TypeKind.SEQ, raw), spec)
                self.assertEqual((expr.kind, expr.head, expr.extra), (kind, head, extra))
                self.assertEqual(expr.raw, raw)
                if inner:
                    self.assertEqual(expr.args[0].head, inner)
                    self.assertNotEqual(expr.args[0].kind, "unknown")
        borrowed = parse_input_type("&'a mut [u8; 4]", spec)
        self.assertTrue(borrowed.is_mut)
        self.assertEqual((borrowed.args[0].kind, borrowed.args[0].extra), ("array", "4"))

    def test_reference_to_slice_now_reaches_the_existing_array_view_rule(self):
        spec = function()
        registry = ViewRegistry.from_sources("")
        for raw in ("[u8]", "&[u8]", "[u8; 4]"):
            with self.subTest(raw=raw):
                resolution = registry.resolve(parse_input_type(raw, spec))
                self.assertEqual(resolution.layer, "L1")
                self.assertEqual(resolution.view_type_text, "Seq<u8>")
                self.assertIn("@", resolution.view_expr("x"))

    def test_self_inside_nested_types_uses_actual_impl_or_trait_context(self):
        spec = replace(function(), self_type="m::Record<T>")
        expr = parse_input_type("Vec<Self>", spec)
        self.assertEqual(expr.raw, "Vec<m::Record<T>>")
        self.assertEqual(expr.args[0].raw, "m::Record<T>")
        expr = parse_input_type("&Self", replace(spec, self_type=None, trait_name="Parent"))
        self.assertEqual(expr.args[0].head, "__DetSelf")

    def test_real_spec_function_and_impl_fn_syntax_are_not_misclassified_as_containers(self):
        spec = function()
        self.assertEqual(parse_input_type("spec_fn(T)->bool", spec).kind, "fn")
        self.assertEqual(parse_input_type("impl Fn(&T)->bool", spec).kind, "impl")
        self.assertEqual(parse_input_type("m::Record<T>", spec).raw, "m::Record<T>")

    def test_malformed_type_syntax_is_not_recovered_by_string_guessing(self):
        for raw in ("Vec<T", "[u8;", "T; type Injected = bool", "unknown!(T)"):
            with self.subTest(raw=raw), self.assertRaises(TypeContextError):
                parse_input_type(raw, function())


@unittest.skipUnless(os.environ.get("SPECDET_NATIVE_VERUS"), "Set SPECDET_NATIVE_VERUS to the raw Verus ELF")
class NativeSourceBoundProofTests(unittest.TestCase):
    def test_custom_and_inherited_view_bounds_verify_without_strengthening(self):
        source = """use vstd::prelude::*;
verus! {
pub trait Custom { spec fn view(&self) -> int; }
pub trait Parent: View {}
pub trait Child<A>: Parent {}
fn custom<T: Custom>(x: T) -> (ret: T)
    ensures ret.view() == x.view(),
{ x }
fn inherited<T: Child<u8>>(x: T) -> (ret: T)
    ensures ret.view() == x.view(),
{ x }
}"""
        generated = []
        for name in ("custom", "inherited"):
            spec = extract_spec(source, name)
            registry = ViewRegistry.from_sources(source, bound_views=bound_views(spec, [source]))
            left, right = pair_identifiers(spec)["x"]
            pair = InputPair("x", left, right, f"{left}.view() == {right}.view()")
            det = build_det_check_spec(spec, view_registry=registry, input_pairs=(pair,))
            self.assertNotIn("T: View", det.equal_fn_def)
            self.assertNotIn("impl View", det.equal_fn_def)
            generated.append(render_template(det, []))
        executable = Path(os.environ["SPECDET_NATIVE_VERUS"])
        with executable.open("rb") as stream:
            self.assertEqual(stream.read(4), b"\x7fELF")
        compiled_source = inject_into_source(source, "\n\n".join(generated))
        path = Path(f".source_type_context_{os.getpid()}_{uuid4().hex}.rs")
        try:
            path.write_text(compiled_source)
            result = subprocess.run(
                [str(executable), "--crate-type=lib", "--crate-name=source_type_context",
                 "--edition=2021", "--num-threads=2", str(path)],
                capture_output=True, text=True, timeout=120,
                env=dict(os.environ, TMPDIR=str(Path.cwd())),
            )
            self.assertEqual(path.read_text(), compiled_source)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("0 errors", result.stdout + result.stderr)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
