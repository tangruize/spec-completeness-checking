"""Source-fidelity and real contract-attribute extraction regression checks."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from specdet.adapters.verus.native.codegen.expressions import _parse
from specdet.adapters.verus.native.extract.extractor import extract_spec

from seal import HERE, MANIFEST, SEALED, SELECTION, build, check


class NanvixPackagingTests(unittest.TestCase):
    def test_source_inputs_are_local_and_ignored(self) -> None:
        profile = tomllib.loads((HERE / "specdet.toml").read_text())
        self.assertEqual(profile["project"]["root"], "inputs/sealed")
        self.assertIn("/inputs/", (HERE / ".gitignore").read_text().splitlines())
        self.assertEqual(MANIFEST.parent.name, "inputs")
        selection = json.loads(SELECTION.read_text())
        self.assertEqual(set(selection), {"schema_version", "source_revision", "source_hashes", "scope"})

    def test_changed_source_is_rejected_before_extraction(self) -> None:
        with patch("seal.Path.read_bytes", return_value=b"unreviewed input"):
            with self.assertRaisesRegex(ValueError, "Source differs from the reviewed"):
                build(Path("not-read-from-disk"))


class NanvixCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not MANIFEST.is_file():
            raise unittest.SkipTest("Generate ignored local inputs with run.py --source-root first")

    def test_sealed_bytes_and_headers(self) -> None:
        manifest = check()
        source = (SEALED / "bitmap_contract.rs").read_text()
        for target in manifest["targets"]:
            with self.subTest(target=target["name"]):
                self.assertEqual(source.count(target["original_header"]), 1)

    def test_all_clauses_are_individual_native_expressions(self) -> None:
        source = (SEALED / "bitmap_contract.rs").read_text()
        for target, expected_ensures in {
            "number_of_bits": 3, "usage": 3, "alloc": 2, "set": 2,
        }.items():
            with self.subTest(target=target):
                line = source[:source.index(f"pub fn {target}(")].count("\n") + 1
                contract = extract_spec(source, target, source_line=line)
                self.assertEqual(len(contract.requires), 1)
                self.assertEqual(len(contract.ensures), expected_ensures)
                for clause in (*contract.requires, *contract.ensures):
                    _parse(clause)


if __name__ == "__main__":
    unittest.main()
