from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from specdet.storage.artifacts import ArtifactStore
from specdet.domain.models import Stage
from specdet.domain.proposals import Proposal, ValidationRecord

TOOL = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_installed_help_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "specdet", "--help"],
                cwd=directory, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("analyze", process.stdout)

    def test_core_cli_import_loads_neither_language_nor_provider(self):
        script = (
            "import sys; import specdet.cli.main; "
            "assert not any(n.startswith(('z3', 'tree_sitter', 'specdet.providers', "
            "'spec_determinism')) for n in sys.modules)"
        )
        process = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_report_reads_versioned_artifact_without_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ArtifactStore(root).artifact("summary.json", "run_summary", {
                "results": [], "run_dir": str(root), "counts": {"inconclusive": 1},
            })
            process = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "specdet", "report", "--run", str(root), "--json"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["counts"], {"inconclusive": 1})

    def test_import_layers_have_no_legacy_runtime_dependencies(self):
        for directory in ("domain", "analysis", "ports", "storage"):
            for path in (TOOL / "src/specdet" / directory).rglob("*.py"):
                text = path.read_text()
                for forbidden in ("from spec_determinism", "import spec_determinism", "import z3", "import tree_sitter"):
                    self.assertNotIn(forbidden, text, str(path))

    def test_adopt_understands_the_assistance_record_format(self):
        from specdet.assistance.records import write_record

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal = Proposal("request", Stage.PROOF_GENERATION, "verus", "proof", "source", "problem", {"proof": ""})
            write_record(root / "proposal.json", "proposal", proposal)
            write_record(root / "validation.json", "validation", ValidationRecord(proposal.id, "accepted_for_check"))
            process = subprocess.run(
                [
                    sys.executable, "-I", "-B", "-m", "specdet", "adopt",
                    "--proposal", str(root / "proposal.json"),
                    "--validation", str(root / "validation.json"),
                    "--out", str(root / "adoption.json"),
                ], capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            adopted = ArtifactStore(root).read_artifact("adoption.json", expected_kind="adoption")
            self.assertEqual(adopted["proposal_id"], proposal.id)
            self.assertEqual(adopted["status"], "accepted_for_check")


@unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Set SPECDET_VERUS to run real verifier integration")
class VerusIntegrationTests(unittest.TestCase):
    def test_input_fixture_itself_is_accepted_by_verus(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "contracts.rs"
            shutil.copyfile(TOOL / "examples/basic/contracts.rs", source)
            process = subprocess.run(
                [os.environ["SPECDET_VERUS"], str(source)],
                cwd=directory, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)

    def test_standalone_cli_preserves_inputs_and_reports_honest_results(self):
        source = TOOL / "examples/basic/contracts.rs"
        before = source.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run(
                [
                    sys.executable, "-I", "-B", "-m", "specdet", "analyze",
                    str(source), "--config", str(TOOL / "examples/basic/specdet.toml"),
                    "--verus", os.environ["SPECDET_VERUS"],
                    "--out", str(Path(directory) / "runs"), "--json",
                    "--timeout", "30", "--solver-timeout-ms", "1000", "--max-rounds", "30",
                ],
                cwd=directory, capture_output=True, text=True, timeout=180,
            )
            self.assertIn(process.returncode, (0, 1, 3), process.stderr + process.stdout)
            report = json.loads(process.stdout)
            by_name = {item["target"]["name"]: item for item in report["results"]}
            self.assertEqual(by_name["identity"]["verdict"], "deterministic", report)
            self.assertEqual(by_name["replace_value"]["verdict"], "deterministic", report)
            self.assertIn(by_name["loose"]["verdict"], ("nondeterministic", "inconclusive"), report)
            self.assertTrue((Path(report["run_dir"]) / "snapshot.json").is_file())
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
