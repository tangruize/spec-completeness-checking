from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from specdet.adapters.verus.backend import VerusBackend
from specdet.adapters.verus.discovery import discover_project, scan_source
from specdet.config import BuildConfig, Config
from specdet.domain.models import StageError, digest, text_digest
from specdet.storage.workspace import PreparedProject


class DiscoveryTests(unittest.TestCase):
    def test_precise_qualified_methods_and_modes(self):
        source = """// fn bogus() {}
verus! {
mod nested {
struct A {}
struct B {}
impl A {
 pub fn value(&self) -> (r: u8) ensures r == 1, { 1 }
 pub proof fn lemma() {}
 pub open spec fn model(&self) -> int { 0 }
}
impl B {
 pub fn value(&self) -> (r: u8) ensures r == 2, { 2 }
}
fn private() {}
}
}
"""
        locations, diagnostics = scan_source(source, "src/code.rs", visibility="public")
        self.assertEqual([x.target.line for x in locations], [7, 12])
        self.assertEqual([x.target.name for x in locations], ["value", "value"])
        self.assertNotEqual(locations[0].target.id, locations[1].target.id)
        self.assertIn("nested::A::value", locations[0].target.qualified_name)
        self.assertEqual(locations[0].target.module, "nested")
        self.assertEqual(locations[0].target.source_digest, text_digest(source))
        self.assertGreater(locations[0].insertion, locations[0].end)
        self.assertFalse(diagnostics)

    def test_contract_declarations_are_discovered(self):
        source = """verus! {
pub trait T { fn declared(&self) -> (r: u8) ensures r == 3; }
pub assume_specification[other::specified](x: u8) -> (r: u8) ensures r == x;
}"""
        locations, diagnostics = scan_source(source, "lib.rs")
        self.assertEqual([x.target.name for x in locations], ["declared", "specified"])
        self.assertTrue(all(x.declaration and x.has_contract for x in locations))
        self.assertFalse(diagnostics)

    def test_generic_external_selector_keeps_leaf_name_and_exact_native_target(self):
        locations, diagnostics = scan_source(
            "verus! { pub assume_specification<T>[external::f::<T>](x:T)->(r:T) requires true, ensures r==x; }",
            "source.rs",
        )
        self.assertFalse(diagnostics)
        self.assertEqual(locations[0].target.name, "f")
        self.assertEqual(locations[0].extractor_name, "external::f::<T>")
        self.assertIn("external::f::<T>", locations[0].target.qualified_name)
        self.assertTrue(locations[0].has_contract)

    def test_postconditions_are_detected_structurally_not_inside_literals(self):
        locations, diagnostics = scan_source(
            'verus! { pub fn f(x:&str) requires x == "ensures", {} }',
            "source.rs",
        )
        self.assertFalse(diagnostics)
        self.assertFalse(locations[0].has_contract)

    def test_attribute_contract_is_not_borrowed_by_a_neighbor(self):
        locations, diagnostics = scan_source(
            "#[verus_spec(ret => requires x < 255, ensures ret == x)]\n"
            "fn f(x:u8)->u8 {x}\nfn g(x:u8)->u8 {x}",
            "source.rs",
        )
        self.assertEqual([location.has_contract for location in locations], [True, False])
        self.assertTrue(any(item.code == "partial_discovery" for item in diagnostics))

    def test_contract_attributes_respect_known_and_unknown_cfg_conditions(self):
        for condition, present, unsupported in (
            ("verus_keep_ghost", True, False), ("test", False, False),
            ("unresolved", False, True),
        ):
            locations, _ = scan_source(
                f"#[cfg_attr({condition}, verus_spec(ret => ensures ret == x))]\n"
                "fn f(x:u8)->u8 {x}",
                "source.rs",
            )
            self.assertEqual(locations[0].has_contract, present)
            self.assertEqual(bool(locations[0].unsupported), unsupported)

    def test_macros_and_recovery_are_never_total(self):
        locations, diagnostics = scan_source(
            "verus! { generate! { fn hidden() {} } pub fn known() {} }\nfn broken(",
            "lib.rs",
        )
        self.assertIn("known", [x.target.name for x in locations])
        self.assertTrue(diagnostics)
        self.assertTrue(all(x.code == "partial_discovery" for x in diagnostics))

    def test_explicit_features_and_unresolved_cfg(self):
        source = """verus! {
#[cfg(feature = "enabled")] pub fn yes() {}
#[cfg(not(feature = "enabled"))] pub fn no() {}
#[cfg(target_os = "other")] pub fn unknown() {}
}"""
        locations, diagnostics = scan_source(source, "lib.rs", features=("enabled",))
        self.assertEqual([x.target.name for x in locations], ["yes"])
        self.assertEqual(len(diagnostics), 1)

    def test_unicode_offsets_are_bytes(self):
        source = '// café\nverus! { pub fn f()->(r:u8) ensures r==0, { 0 } }'
        locations, _ = scan_source(source, "lib.rs")
        data = source.encode()
        item = locations[0]
        self.assertTrue(data[item.start:item.end].decode().startswith("pub fn"))
        self.assertEqual(item.target.line, 2)

    def test_native_module_graph_and_input_filters(self):
        root = Path.cwd() / f"backend-discovery-{uuid4().hex}"
        root.mkdir()
        try:
            sources = {
                "src/lib.rs": "pub mod child;",
                "src/child.rs": "verus! { pub fn f() {} mod inner { pub fn g() {} } }",
                "src/unlinked.rs": "verus! { pub fn orphan() {} }",
                "skip.rs": "verus! { pub fn skipped() {} }",
            }
            for relative, source in sources.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
            hashes = {name: text_digest(source) for name, source in sources.items()}
            project = PreparedProject(root, root, digest(hashes), hashes)
            config = Config(
                root, root / "out", include=("src/**/*.rs",),
                build=BuildConfig(adapter="verus.native", entrypoint="src/lib.rs"),
            )
            locations, diagnostics = discover_project(
                config, project, lambda relative: sources[relative],
            )
            self.assertEqual([x.target.name for x in locations], ["f", "g"])
            self.assertEqual([x.target.module for x in locations], ["child", "child::inner"])
            self.assertTrue(any(d.details.get("file") == "src/unlinked.rs" for d in diagnostics))
        finally:
            shutil.rmtree(root)

    def test_parser_crashes_and_missing_dependencies_do_not_request_assistance(self):
        root = Path.cwd() / f"backend-parser-error-{uuid4().hex}"
        root.mkdir()
        try:
            source = "verus! { pub fn f() {} }"
            (root / "source.rs").write_text(source)
            files = {"source.rs": text_digest(source)}
            project = PreparedProject(root, root, digest(files), files)
            for error in (ValueError("parser initialization failed"), ImportError("missing parser")):
                backend = VerusBackend(Config(root, root / "out"))
                with patch("specdet.adapters.verus.discovery.parser", side_effect=error):
                    with self.assertRaises(StageError) as caught:
                        backend.discover(project)
                self.assertFalse(caught.exception.recoverable)
        finally:
            shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
