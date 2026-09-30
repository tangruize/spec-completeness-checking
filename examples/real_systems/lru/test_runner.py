from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from uuid import uuid4

from run import LOG_LIMIT, ROOT, run_bounded, sha256


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "runs" / f"runner-test-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.environment = dict(os.environ, TMPDIR=str(self.directory))

    def test_log_size_is_bounded(self):
        result = run_bounded(
            [sys.executable, "-c", f"import os; os.write(1, b'x' * {LOG_LIMIT + 37})"],
            self.directory, 10, self.environment,
        )
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["log_bytes_retained"], LOG_LIMIT)
        self.assertEqual(result["log_bytes_discarded"], 37)
        self.assertEqual((self.directory / "command.log").stat().st_size, LOG_LIMIT)

    def test_timeout_is_not_success(self):
        result = run_bounded(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            self.directory, 1, self.environment,
        )
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["returncode"], 0)

    def test_output_is_restricted_to_ignored_runs(self):
        result = run_bounded(
            [
                sys.executable, str(ROOT / "run.py"), "--verus", "unused",
                "--out", "../outside-this-example",
            ],
            self.directory, 10, self.environment,
        )
        self.assertEqual(result["returncode"], 2)
        self.assertIn(
            "--out must be inside",
            (self.directory / "command.log").read_text(),
        )

    def test_original_seal_and_encoding_only_delta(self):
        seal = json.loads((ROOT / "initial-seal.json").read_text())
        for relative, expected in seal["sealed_files"].items():
            self.assertEqual(sha256(ROOT / relative), expected)
        self.assertEqual(
            (ROOT / "spec/compatible.rs").read_text(),
            (ROOT / "spec/initial.rs").read_text().replace(
                "seq![key]", "Seq::<u64>::empty().push(key)"
            ),
        )


if __name__ == "__main__":
    unittest.main()
