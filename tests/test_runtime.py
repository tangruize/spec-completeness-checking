from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from specdet.config import Config, load_config
from specdet.domain.models import (
    AnalysisReport, CheckEvidence, ProofCheckEvidence, SearchResult, SolverStatus,
    Stage, StageError, TargetRef, Verdict, canonical_json, digest, CounterexampleEvidence,
)
from specdet.analysis.verdicts import classify
from specdet.storage.artifacts import ArtifactStore
from specdet.storage.workspace import prepare_project
from specdet.adapters.verus.execution import ProcessResult, classify_process, run_process
from specdet.runtime import RunDeadlineExceeded, bounded_timeout_ms, check_deadline, run_deadline


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.target = TargetRef("test", "source.test", "f", 1)

    def report(self, baseline):
        return AnalysisReport(
            target=self.target, run_id="run", problem_id="problem",
            baseline=CheckEvidence("problem", baseline, "baseline"),
        )

    def test_canonical_hash_stable(self):
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))
        self.assertNotEqual(digest({"a": 1}), digest({"a": 2}))
        self.assertEqual(json.loads(canonical_json(self.target))["name"], "f")

    def test_unknown_and_narrowed_unsat_are_not_global_proofs(self):
        report = self.report(SolverStatus.UNKNOWN)
        report.search = SearchResult(evidence=(
            CheckEvidence("problem", SolverStatus.UNSAT, "refinement", constraints=({"x": 1},)),
        ))
        self.assertEqual(classify(report), Verdict.INCONCLUSIVE)

    def test_sat_refinement_proves_nondeterminism(self):
        report = self.report(SolverStatus.UNKNOWN)
        report.search = SearchResult(evidence=(
            CheckEvidence("problem", SolverStatus.SAT, "refinement"),
        ))
        self.assertEqual(classify(report), Verdict.NONDETERMINISTIC)
        self.assertEqual(report.baseline.status, SolverStatus.UNKNOWN)

    def test_proof_success_keeps_original_unknown(self):
        report = self.report(SolverStatus.UNKNOWN)
        report.proofs.append(ProofCheckEvidence("problem", "candidate", "verified", 1))
        self.assertEqual(classify(report), Verdict.DETERMINISTIC)
        self.assertEqual(report.baseline.status, SolverStatus.UNKNOWN)

    def test_generated_proof_is_not_verified(self):
        report = self.report(SolverStatus.UNKNOWN)
        report.proofs.append(ProofCheckEvidence("problem", "candidate", "generated", 0))
        self.assertEqual(classify(report), Verdict.INCONCLUSIVE)

    def test_conflicting_evidence_fails_closed(self):
        report = self.report(SolverStatus.SAT)
        report.proofs.append(ProofCheckEvidence("problem", "candidate", "verified", 1))
        self.assertEqual(classify(report), Verdict.INCONCLUSIVE)
        self.assertIn("inconsistent_evidence", [d.code for d in report.diagnostics])

    def test_unverified_translation_is_model_only(self):
        report = self.report(SolverStatus.UNSAT)
        self.assertEqual(classify(report, trusted_translation=False), Verdict.INCONCLUSIVE)
        self.assertEqual(report.annotations[0]["model_verdict"], "deterministic")

    def test_permitted_annotation_does_not_promote_unknown(self):
        report = self.report(SolverStatus.UNKNOWN)
        report.annotations.append({"permitted": True, "origin": "manual"})
        self.assertEqual(classify(report), Verdict.INCONCLUSIVE)

    def test_verified_source_witness_confirms_nondeterminism_without_changing_raw_smt(self):
        report = self.report(SolverStatus.UNKNOWN)
        bindings = {"n": 1, "r1": {"kind": "vec", "items": [0]}, "r2": {"kind": "vec", "items": [1]}}
        report.counterexample = CounterexampleEvidence(
            "problem", "obligation", "verified", 1, bindings, {},
            "replay.json", "certificate", digest([bindings, {}]),
        )
        self.assertEqual(classify(report), Verdict.NONDETERMINISTIC)
        self.assertEqual(report.baseline.status, SolverStatus.UNKNOWN)
        bindings["n"] = 2
        self.assertEqual(classify(report), Verdict.INCONCLUSIVE)


class StorageTests(unittest.TestCase):
    def test_reading_a_missing_store_does_not_create_it(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            store = ArtifactStore(missing)
            with self.assertRaises(FileNotFoundError):
                store.read_artifact("report.json")
            self.assertFalse(missing.exists())

    def test_artifact_digest_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root)
            ref = store.artifact("test.json", "test", {"x": 42})
            self.assertEqual(store.read_artifact(ref.path, expected_kind="test"), {"x": 42})
            with self.assertRaises(ValueError):
                store.path("../escape.json")
            raw = json.loads((root / ref.path).read_text())
            raw["payload"]["x"] = 43
            (root / ref.path).write_text(json.dumps(raw))
            with self.assertRaises(ValueError):
                store.read_artifact(ref.path)

    def test_snapshot_is_a_real_copy_and_excludes_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "project"
            source.mkdir()
            (source / "f.rs").write_text("original")
            output = source / "results"
            run = output / "run"
            run.mkdir(parents=True)
            config = Config(project_root=source, output_dir=output)
            project = prepare_project(config, run)
            project.source_path("f.rs").write_text("changed snapshot")
            self.assertEqual((source / "f.rs").read_text(), "original")
            self.assertEqual(set(project.files), {"f.rs"})
            self.assertFalse((project.root / "results").exists())

    def test_symlinks_cannot_escape_write_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "project"
            source.mkdir()
            outside = root / "outside.rs"
            outside.write_text("untouched")
            (source / "link.rs").symlink_to(outside)
            config = Config(project_root=source, output_dir=root / "out")
            with self.assertRaises(StageError):
                prepare_project(config, root / "out/run")
            self.assertEqual(outside.read_text(), "untouched")


class ConfigTests(unittest.TestCase):
    def test_whole_run_default_and_explicit_override_are_separate_from_per_call_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / "specdet.toml"
            path.write_text('schema_version=1\n[project]\nroot="."\n')
            self.assertEqual(load_config(path).limits.run_timeout_seconds, 60)
            path.write_text(
                'schema_version=1\n[project]\nroot="."\n'
                '[limits]\nrun_timeout_seconds=180\nverifier_timeout_seconds=17\n'
            )
            config = load_config(path)
            self.assertEqual(config.limits.run_timeout_seconds, 180)
            self.assertEqual(config.limits.verifier_timeout_seconds, 17)
            path.write_text('schema_version=1\n[limits]\nrun_timeout_seconds=0\n')
            with self.assertRaisesRegex(ValueError, "run_timeout_seconds"):
                load_config(path)

    def test_paths_are_relative_to_config_not_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "project").mkdir()
            path = base / "specdet.toml"
            path.write_text(
                'schema_version=1\n[project]\nroot="project"\n'
                '[artifacts]\noutput_dir="out"\n'
                '[adapters.verus]\nexecutable="tool/verus"\n'
            )
            config = load_config(path)
            self.assertEqual(config.project_root, base / "project")
            self.assertEqual(config.output_dir, base / "out")
            self.assertEqual(config.toolchain.executable, str(base / "tool/verus"))
            self.assertNotIn("nanvix", canonical_json(config.to_dict()))

    def test_no_environment_flag_can_enable_llm(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / "specdet.toml"
            path.write_text('schema_version=1\n[project]\nroot="."\n')
            with patch.dict(os.environ, {"SPEC_DET_LLM_PROOF": "1"}):
                self.assertEqual(load_config(path).assistance.get("mode", "off"), "off")

    def test_invalid_budget_and_offline_live_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / "specdet.toml"
            path.write_text('schema_version=1\n[limits]\nmax_search_rounds=-1\n')
            with self.assertRaises(ValueError):
                load_config(path)
            config = Config(
                project_root=base, output_dir=base / "out", offline=True,
                assistance={"mode": "live"},
            )
            with self.assertRaises(ValueError):
                config.validate()


class ProcessTests(unittest.TestCase):
    def test_verification_requires_real_goal_and_clean_result(self):
        self.assertEqual(classify_process(ProcessResult((), 0, "0 verified, 0 errors", "", 1))[0], "error")
        self.assertEqual(classify_process(ProcessResult((), 0, "1 verified, 0 errors", "", 1)), ("verified", 1))
        self.assertEqual(classify_process(ProcessResult((), 1, "1 verified, 0 errors", "error: type", 1))[0], "compile_error")
        self.assertEqual(classify_process(ProcessResult((), 1, "", "postcondition not satisfied", 1))[0], "unproved")
        self.assertEqual(classify_process(ProcessResult((), -1, "", "", 1, True))[0], "timeout")

    def test_dependency_verification_does_not_count_as_a_selected_target_proof(self):
        dependency = "verification results:: 1902 verified, 0 errors\n"
        empty_target = "verification results:: 0 verified, 0 errors (partial verification)\n"
        target = "verification results:: 1 verified, 0 errors (partial verification)\n"
        self.assertEqual(classify_process(ProcessResult((), 0, dependency + empty_target, "", 1)), ("error", 0))
        self.assertEqual(classify_process(ProcessResult((), 0, dependency + target, "", 1)), ("verified", 1))

    @unittest.skipUnless(os.name == "posix", "Whole-run signal cancellation is POSIX-only")
    def test_deadline_reaps_owned_process_group_and_preserves_partial_output(self):
        script = (
            "import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "print(p.pid, flush=True); print('partial evidence', flush=True); time.sleep(30)"
        )
        with tempfile.TemporaryDirectory() as directory:
            with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as unrelated:
                try:
                    started = time.monotonic()
                    with self.assertRaises(RunDeadlineExceeded):
                        with run_deadline(0.5, started=started):
                            result = run_process(
                                [sys.executable, "-c", script], Path(directory), 20, os.environ.copy(),
                            )
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertTrue(result.timed_out)
                    self.assertIn("partial evidence", result.stdout)
                    pid = int(result.stdout.splitlines()[0])
                    stat = Path(f"/proc/{pid}/stat")
                    if stat.exists():
                        self.assertEqual(stat.read_text().split()[2], "Z")
                    self.assertIsNone(unrelated.poll())
                finally:
                    unrelated.terminate()
                    unrelated.wait(timeout=3)

    @unittest.skipUnless(os.name == "posix", "Whole-run signal cancellation is POSIX-only")
    def test_deadline_is_scoped_and_does_not_steal_an_existing_alarm(self):
        original = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(RunDeadlineExceeded):
            with run_deadline(0.05, started=time.monotonic()):
                self.assertGreater(bounded_timeout_ms(10000), 0)
                self.assertLessEqual(bounded_timeout_ms(10000), 50)
                time.sleep(1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), original)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        check_deadline()
        self.assertEqual(bounded_timeout_ms(123), 123)
        signal.setitimer(signal.ITIMER_REAL, 10)
        try:
            with self.assertRaisesRegex(ValueError, "existing process alarm"):
                with run_deadline(1, started=time.monotonic()):
                    self.fail("Must not replace the caller's timer")
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

if __name__ == "__main__":
    unittest.main()
