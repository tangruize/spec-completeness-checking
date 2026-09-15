from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from specdet.adapters.verus.backend import VerusBackend
from specdet.adapters.verus.discovery import scan_source
from specdet.adapters.verus.execution import ProcessResult
from specdet.adapters.verus.proposal_validation import proof_structure
from specdet.config import Config
from specdet.domain.models import (
    Obligation, ProofCandidate, SolverStatus, StageError, digest, text_digest,
)
from specdet.storage.workspace import PreparedProject


class RecordingExecutor:
    def __init__(self, results=(), mutate=False):
        self.calls = []
        self.results = list(results)
        self.mutate = mutate

    def verify(self, source, project_root, artifact_dir, function, module="", *, all_functions=False):
        self.calls.append((source, project_root, artifact_dir, function, module, source.read_text()))
        (artifact_dir / "logs").mkdir(parents=True)
        if self.mutate:
            source.write_text(source.read_text() + "\n// mutation")
        if self.results:
            return self.results.pop(0)
        return ProcessResult(("verifier",), 0, "1 verified, 0 errors", "", 1)


class BackendCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / f"backend-check-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.source = "use vstd::prelude::*;\nverus! { pub fn f(x:u8)->(r:u8) ensures r==x, { x } }\nfn main() {}"
        (self.project_root / "f.rs").write_text(self.source)
        hashes = {"f.rs": text_digest(self.source)}
        self.project = PreparedProject(self.project_root, self.project_root, digest(hashes), hashes)
        self.backend = VerusBackend(Config(self.project_root, self.root / "out"))
        self.executor = RecordingExecutor()
        self.backend._executor = self.executor
        location = scan_source(self.source, "f.rs")[0][0]
        native = {
            "source": self.source, "source_context": self.backend._source_context(location),
            "semantic_config": self.backend._semantic_config(),
            "snapshot_digest": self.project.snapshot_digest,
            "det_spec": {}, "schemas": [], "fn_det": "f_det",
            "template": "proof fn f_det(x:u8,r1:u8,r2:u8) requires r1==x,r2==x, ensures r1==r2, {}",
            "guarded_template": "proof fn f_det(x:u8,r1:u8,r2:u8,g:bool) requires r1==x,r2==x,g ==> r1==0, ensures r1==r2, {}",
            "overlays": {},
        }
        self.obligation = Obligation(digest(native), location.target, native)
        self.baseline = ProofCandidate(self.obligation.problem_id, self.obligation.id)
        self.scan = patch.object(
            self.backend, "_scan_candidate", create=True,
            side_effect=lambda obligation, proof, helpers: proof_structure(proof, helpers, {"f", "f_det"}),
        )
        self.scan.start()
        self.addCleanup(self.scan.stop)

    def test_baseline_compiles_once_without_mutating_snapshot(self):
        evidence = self.backend.check(self.project, self.obligation, self.baseline, self.root / "baseline", baseline=True)
        self.assertEqual(evidence.status, "verified")
        self.assertEqual(len(self.executor.calls), 1)
        self.assertIn("g ==> r1==0", self.executor.calls[0][-1])
        self.assertEqual((self.project_root / "f.rs").read_text(), self.source)
        self.assertNotEqual(self.executor.calls[0][0], self.project_root / "f.rs")
        self.assertTrue((self.root / "baseline" / "harness.rs").exists())

    def test_readonly_snapshot_produces_writable_owned_attempt(self):
        source = self.project_root / "f.rs"
        source.chmod(0o444)
        evidence = self.backend.check(self.project, self.obligation, self.baseline, self.root / "readonly", baseline=True)
        self.assertEqual(evidence.status, "verified")
        self.assertEqual(source.stat().st_mode & 0o777, 0o444)
        self.assertTrue(Path(evidence.native["source"]).stat().st_mode & 0o200)

    def test_candidate_checks_original_goal_and_every_helper(self):
        candidate = ProofCandidate(
            self.obligation.problem_id, self.obligation.id,
            "lemma();", "proof fn lemma() ensures true {}", "accepted",
        )
        evidence = self.backend.check(self.project, self.obligation, candidate, self.root / "candidate")
        self.assertEqual(evidence.status, "verified")
        self.assertEqual([call[3] for call in self.executor.calls], ["f_det", "lemma"])
        harness = self.executor.calls[0][-1]
        self.assertNotIn("g ==> r1==0", harness)
        self.assertIn("requires r1==x,r2==x, ensures r1==r2", harness)
        self.assertEqual(evidence.verified_goals, 2)

    def test_unproved_helper_cannot_be_a_verified_candidate(self):
        self.executor.results = [
            ProcessResult((), 0, "1 verified, 0 errors", "", 1),
            ProcessResult((), 1, "", "postcondition not satisfied", 1),
        ]
        candidate = ProofCandidate(self.obligation.problem_id, self.obligation.id, "lemma();", "proof fn lemma() ensures false {}", "accepted")
        evidence = self.backend.check(self.project, self.obligation, candidate, self.root / "bad-helper")
        self.assertEqual(evidence.status, "unproved")

    def test_source_mutation_invalidates_evidence(self):
        self.executor.mutate = True
        evidence = self.backend.check(self.project, self.obligation, self.baseline, self.root / "mutation", baseline=True)
        self.assertEqual(evidence.status, "error")
        self.assertIn("verifier_modified_input", [d.code for d in evidence.diagnostics])
        self.assertEqual((self.project_root / "f.rs").read_text(), self.source)
        with self.assertRaises(StageError):
            self.backend._baseline_key(self.obligation, evidence)

    def test_rejected_candidate_is_never_a_query_baseline(self):
        candidate = ProofCandidate(self.obligation.problem_id, "wrong", "assert(false);", origin="accepted")
        evidence = self.backend.check(self.project, self.obligation, candidate, self.root / "rejected")
        self.assertEqual(evidence.status, "rejected")
        self.assertFalse(self.executor.calls)
        with self.assertRaises(StageError):
            self.backend._baseline_key(self.obligation, evidence)

    def test_nested_native_content_cannot_change_goal(self):
        self.obligation.native["template"] = "proof fn f_det() ensures true {}"
        with self.assertRaises(StageError):
            self.backend.check(self.project, self.obligation, self.baseline, self.root / "tampered", baseline=True)
        self.assertFalse(self.executor.calls)

    def test_timeout_compile_error_and_unproved_remain_distinct(self):
        for index, (result, expected) in enumerate((
            (ProcessResult((), -1, "", "", 1, timed_out=True), "timeout"),
            (ProcessResult((), 1, "", "error: mismatched types", 1), "compile_error"),
            (ProcessResult((), 1, "", "postcondition not satisfied", 1), "unproved"),
        )):
            self.executor.results = [result]
            evidence = self.backend.check(self.project, self.obligation, self.baseline, self.root / str(index), baseline=True)
            self.assertEqual(evidence.status, expected)

    def test_smt_selection_uses_exact_function_def_not_size(self):
        evidence = self.backend.check(self.project, self.obligation, self.baseline, self.root / "logs", baseline=True)
        logs = Path(evidence.native["logs"])
        wrong = logs / "large.smt2"
        wrong.write_text(";; Function-Def f::other::f_det\n" + ";" * 10000)
        right = logs / "small.smt2"
        right.write_text(";; Function-Def f::f_det\n(check-sat)\n")
        self.assertEqual(self.backend._select_smt(self.obligation, evidence), right)
        (logs / "duplicate.smt2").write_text(right.read_text())
        with self.assertRaises(StageError):
            self.backend._select_smt(self.obligation, evidence)

    def test_unknown_is_not_a_counterexample(self):
        self.assertEqual(self.backend._solver_status("unknown"), SolverStatus.UNKNOWN)
        self.assertNotEqual(self.backend._solver_status("unknown"), SolverStatus.SAT)
        with self.assertRaises(StageError):
            self.backend._solver_status("timeout treated as sat")


if __name__ == "__main__":
    unittest.main()
