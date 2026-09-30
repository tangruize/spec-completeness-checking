from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import unittest
from unittest.mock import patch

from specdet.cli.output import compact_summary, print_summary
from specdet.cli.main import main


def report(verdict="inconclusive"):
    return {
        "run_id": "run", "run_dir": "/evidence/run", "status": "completed", "exit_code": 3,
        "duration_ms": 1250, "counts": {verdict: 1}, "diagnostics": [],
        "results": [{
            "target": {"file": "src/cache.rs", "name": "pop", "line": 12, "source_digest": "source"},
            "problem_id": "problem", "status": "completed", "verdict": verdict,
            "analysis_kind": "concrete_determinism", "duration_ms": 1000,
            "baseline": {"problem_id": "problem", "status": "unknown", "role": "baseline",
                         "reason": "incomplete quantifiers", "artifact": "/evidence/query.smt2"},
            "proofs": [], "diagnostics": [], "counterexample": None,
            "coverage": {"observation_policy": "verus-observable-v1", "trusted_translation": True,
                         "ignored_dimensions": ["return.Err.payload"], "feasibility": "not_run"},
            "resources": {"scope": "target_all_phases_and_revisions",
                          "counterexample_candidates": {"used": 0, "limit": 16}},
            "artifacts": [{"kind": "observation_plan", "path": "/evidence/observation-plan.json", "digest": "obs"}],
        }],
    }


class CliOutputTests(unittest.TestCase):
    def render(self, summary, **options):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            print_summary(summary, **options)
        return stdout.getvalue(), stderr.getvalue()

    def test_full_json_remains_the_original_report(self):
        summary = report()
        before = deepcopy(summary)
        stdout, stderr = self.render(summary, json_output=True)
        self.assertEqual(json.loads(stdout), summary)
        self.assertEqual(summary, before)
        self.assertFalse(stderr)

    def test_unknown_explains_target_blocker_observation_and_actual_budget(self):
        summary = report()
        summary["results"][0]["diagnostics"] = [
            {"stage": "proof_checking", "code": "unproved", "severity": "error", "message": "Proof not closed"},
            {"stage": "counterexample", "code": "unsupported_witness", "severity": "warning",
             "message": "No concrete constructor catalog for NonNull<u8>"},
        ]
        stdout, _ = self.render(summary)
        self.assertIn("baseline: unknown (incomplete quantifiers)", stdout)
        self.assertIn("counterexample/unsupported_witness", stdout)
        self.assertIn("No concrete constructor catalog for NonNull<u8>", stdout)
        self.assertIn("ignored: return.Err.payload", stdout)
        self.assertIn("witness candidates 0/16", stdout)
        self.assertLess(stdout.index("unsupported_witness"), stdout.index("proof_checking/unproved"))
        self.assertIn("Elapsed (whole run): 1.250s", stdout)
        self.assertTrue(stdout.rstrip().endswith("Elapsed (whole run): 1.250s"))

    def test_verified_witness_and_unknown_baseline_are_both_preserved(self):
        summary = report("nondeterministic")
        summary["results"][0]["counterexample"] = {
            "problem_id": "problem", "kind": "source_verified_constructive",
            "status": "verified", "verified_goals": 1,
            "certificate_digest": "certificate", "bindings_digest": "bindings",
            "artifact": "/evidence/certificate.json", "bindings": {"r1": 0, "r2": 1}, "type_arguments": {},
        }
        summary["results"][0]["diagnostics"] = [{
            "stage": "proof_checking", "code": "unproved", "severity": "error", "message": "Old attempt failed",
        }]
        before = deepcopy(summary)
        result = compact_summary(summary)["results"][0]
        self.assertEqual(result["verdict"], "nondeterministic")
        self.assertEqual(result["baseline"]["status"], "unknown")
        self.assertEqual(result["decisive_evidence"]["kind"], "source_verified_constructive")
        stdout, _ = self.render(summary)
        self.assertIn("source_verified_constructive", stdout)
        self.assertNotIn("Old attempt failed", stdout)
        self.assertEqual(summary, before)

    def test_local_unsat_or_untrusted_model_is_never_displayed_as_a_global_proof(self):
        summary = report()
        row = summary["results"][0]
        row["search"] = {"rounds": 1, "evidence": [
            {"problem_id": "problem", "status": "unsat", "role": "refinement", "constraints": [{"capacity": 1}]},
        ]}
        brief = compact_summary(summary)["results"][0]
        self.assertIsNone(brief["decisive_evidence"])
        self.assertEqual(brief["search"]["refinement_status_counts"], {"unsat": 1})
        stdout, _ = self.render(summary)
        self.assertIn("local UNSAT does not prove global determinism", stdout)
        row["coverage"]["trusted_translation"] = False
        row["proofs"] = [{"problem_id": "problem", "status": "verified", "verified_goals": 1, "artifact": "proof"}]
        self.assertIsNone(compact_summary(summary)["results"][0]["decisive_evidence"])
        stdout, _ = self.render(summary)
        self.assertIn("translation to the analyzed model is not confirmed", stdout)
        self.assertNotIn("evidence: verified_original_obligation", stdout)

    def test_actual_success_basis_distinguishes_original_proof_and_sat_slice(self):
        for verdict, evidence, kind in (
            ("deterministic", {"status": "unsat", "role": "baseline"}, "solver_query"),
            ("nondeterministic", {"status": "sat", "role": "refinement"}, "solver_query"),
        ):
            with self.subTest(verdict=verdict):
                summary = report(verdict)
                summary["results"][0]["search"] = {"evidence": [{"problem_id": "problem", **evidence}]}
                result = compact_summary(summary)["results"][0]
                self.assertEqual(result["decisive_evidence"]["kind"], kind)
                self.assertEqual(result["decisive_evidence"]["role"], evidence["role"])
        summary = report("deterministic")
        summary["results"][0]["proofs"] = [
            {"problem_id": "other", "status": "verified", "verified_goals": 10, "artifact": "wrong"},
            {"problem_id": "problem", "status": "verified", "verified_goals": 1, "artifact": "right"},
        ]
        self.assertEqual(compact_summary(summary)["results"][0]["decisive_evidence"]["artifact"], "right")

    def test_compact_output_bounds_previews_and_links_to_complete_evidence(self):
        summary = report("nondeterministic")
        row = summary["results"][0]
        row["proofs"] = [{"status": "unproved", "native": {"stdout": "SOURCE_BODY_" * 100000}}]
        row["counterexample"] = {
            "problem_id": "problem", "kind": "source_verified_constructive", "status": "verified",
            "verified_goals": 1, "certificate_digest": "certificate", "artifact": "/evidence/witness.json",
            "bindings": {"r1": list(range(10000))}, "type_arguments": {},
        }
        row["diagnostics"] = [
            {"stage": "search", "code": f"gap_{i}", "severity": "warning",
             "message": "large diagnostic " * 5000, "details": {"stdout": "hidden log" * 10000}}
            for i in range(20)
        ]
        row["coverage"]["ignored_dimensions"] = [f"representation.field{i}" for i in range(100)]
        result = compact_summary(summary)
        encoded = json.dumps(result)
        self.assertLess(len(encoded.encode()), 8192)
        self.assertNotIn("SOURCE_BODY_", encoded)
        self.assertNotIn("hidden log", encoded)
        self.assertTrue(result["results"][0]["counterexample"]["values_omitted"])
        self.assertEqual(result["results"][0]["diagnostics_omitted"], 17)
        self.assertTrue(all(len(item["message"]) <= 500 for item in result["results"][0]["diagnostics"]))
        self.assertEqual(result["results"][0]["coverage"]["ignored_dimensions_omitted"], 92)
        self.assertEqual(result["full_report"], "/evidence/run/summary.json")

    def test_target_compiler_error_is_visible_without_dumping_its_log(self):
        summary = report("not_evaluated")
        summary["results"][0].update(status="failed", baseline=None)
        summary["results"][0]["diagnostics"] = [{
            "stage": "proof_checking", "code": "compile_error", "severity": "error",
            "message": "Generated harness did not compile",
            "details": {"functions": [{"process": {
                "stderr": "warning: verbose detail\nerror[E0308]: mismatched types\nvery long source listing",
            }}]},
        }]
        stdout, _ = self.render(summary)
        self.assertIn("error[E0308]: mismatched types", stdout)
        self.assertNotIn("very long source listing", stdout)

    def test_repeated_recovery_warnings_do_not_bury_blockers_or_budget_limits(self):
        summary = report()
        row = summary["results"][0]
        row["diagnostics"] = [
            {"stage": "extract", "code": "extraction_gap", "severity": "warning",
             "message": f"Unresolved attribute at line {index}"}
            for index in range(60)
        ] + [
            {"stage": "proof_checking", "code": "unproved", "severity": "error", "message": "Proof not closed"},
            {"stage": "counterexample", "code": "unsupported_witness", "severity": "warning",
             "message": "Opaque pointer has no constructor"},
            {"stage": "proof_generation", "code": "proof_budget_exhausted", "severity": "info",
             "message": "Proof attempt budget exhausted"},
        ]
        brief = compact_summary(summary)["results"][0]
        self.assertEqual(
            [item["code"] for item in brief["diagnostics"]],
            ["unsupported_witness", "proof_budget_exhausted", "unproved"],
        )
        self.assertEqual(brief["diagnostics_omitted"], 60)
        row["diagnostics"] = row["diagnostics"][:60]
        brief = compact_summary(summary)["results"][0]
        self.assertEqual(len(brief["diagnostics"]), 1)
        self.assertEqual(brief["diagnostics_omitted"], 59)

    def test_large_observation_items_are_bounded_with_explicit_preview_markers(self):
        summary = report()
        row = summary["results"][0]
        row["coverage"].update({
            "input_observations": [{"type": "long type " * 10000} for _ in range(8)],
            "ignored_dimensions": ["long dimension " * 10000 for _ in range(12)],
        })
        before = deepcopy(summary)
        brief = compact_summary(summary)["results"][0]
        self.assertLess(len(json.dumps(brief).encode()), 16384)
        self.assertTrue(brief["coverage"]["input_observations"][0]["preview_truncated"])
        self.assertIn("[truncated]", brief["coverage"]["ignored_dimensions"][0])
        self.assertEqual(brief["coverage"]["input_observations_omitted"], 4)
        self.assertEqual(brief["coverage"]["ignored_dimensions_omitted"], 4)
        self.assertEqual(summary, before)

    def test_unchecked_scope_disabled_search_and_query_digest_remain_visible(self):
        summary = report()
        row = summary["results"][0]
        row["baseline"].update(query_digest="query", constraints=[{"x": 1}])
        row["resources"]["counterexample_candidates"] = {"used": 0, "limit": 0}
        row["coverage"].update(global_or_unmentioned_heap="not_modeled", pre_feasibility="not_run")
        brief = compact_summary(summary)["results"][0]
        self.assertEqual(brief["baseline"]["query_digest"], "query")
        self.assertEqual(brief["baseline"]["constraint_count"], 1)
        stdout, _ = self.render(summary)
        self.assertIn("global_or_unmentioned_heap=not_modeled", stdout)
        self.assertIn("pre_feasibility=not_run", stdout)
        self.assertIn("witness candidates 0/0 (disabled)", stdout)

    def test_relative_artifact_links_resolve_against_their_target_not_the_run(self):
        summary = report()
        row = summary["results"][0]
        row["artifact_dir"] = "/evidence/run/targets/target-id"
        row["artifacts"] = [
            {"kind": "observation_plan", "path": "revisions/0/observation-plan.json", "digest": "old"},
            {"kind": "observation_plan", "path": "revisions/1/observation-plan.json", "digest": "new"},
        ]
        result = compact_summary(summary)["results"][0]
        self.assertEqual(
            result["artifacts"]["observation_plan"]["path"],
            "/evidence/run/targets/target-id/revisions/1/observation-plan.json",
        )
        self.assertEqual(result["artifacts"]["observation_plan"]["digest"], "new")

    def test_abstract_prerequisite_and_old_report_missing_metadata_remain_explicit(self):
        summary = report("not_evaluated")
        row = summary["results"][0]
        row.update(status="skipped", analysis_kind="abstract_determinism",
                   concrete_result={"status": "completed", "verdict": "inconclusive", "problem_id": "concrete"})
        row.pop("resources")
        row.pop("duration_ms")
        result = compact_summary(summary)["results"][0]
        self.assertEqual(result["concrete_prerequisite"]["verdict"], "inconclusive")
        self.assertIsNone(result["duration_ms"])
        self.assertEqual(result["resources"], {})
        stdout, _ = self.render(summary)
        self.assertIn("concrete prerequisite: inconclusive", stdout)
        self.assertNotIn("budget (target total)", stdout)
        summary.pop("duration_ms")
        stdout, _ = self.render(summary)
        self.assertTrue(stdout.rstrip().endswith("Elapsed (whole run): unavailable in recorded report"))

    def test_interrupted_compact_invocation_reports_actual_elapsed_time(self):
        stdout = StringIO()
        with patch("specdet.cli.main._config", side_effect=KeyboardInterrupt), redirect_stdout(stdout):
            code = main(["analyze", "--compact-json"])
        data = json.loads(stdout.getvalue())
        self.assertEqual(code, 130)
        self.assertEqual(data["status"], "interrupted")
        self.assertGreaterEqual(data["duration_ms"], 0)
        self.assertEqual(data["diagnostics"][0]["code"], "interrupted")
