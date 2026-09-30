from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "examples/real_systems/hfs/run.py"


class HfsProfileBoundaryTests(unittest.TestCase):
    def test_private_source_snapshots_cannot_be_written_into_either_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            for output in (source / "evidence", ROOT / "hfs-private-test-evidence"):
                with self.subTest(output=output):
                    process = subprocess.run(
                        [sys.executable, str(RUNNER), "--source-root", str(source), "--out", str(output)],
                        capture_output=True, text=True, timeout=15,
                    )
                    self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
                    self.assertIn("--out must be outside", process.stderr)
                    self.assertFalse(output.exists())
