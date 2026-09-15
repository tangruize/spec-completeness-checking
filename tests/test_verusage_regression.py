from __future__ import annotations

import json
import os
import shutil
import unittest
from collections import Counter
from pathlib import Path
from uuid import uuid4

from examples.verusage.provenance import (
    FIXTURE_ROOT, REPOSITORIES, TOOL_ROOT, contained_path, functions,
    selected_function, sha256, span_bytes,
)
from examples.verusage.run import (
    case_config, evaluate_report, load_manifest, run_case, summarize,
    verifier_from_environment,
)
from specdet.adapters.verus.backend import VerusBackend
from specdet.analysis.pipeline import select_targets
from specdet.config import ToolchainConfig
from specdet.domain.models import digest
from specdet.storage.workspace import PreparedProject


class VerusageProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest()
        cls.fixtures = {entry["path"]: entry for entry in cls.manifest["fixtures"]}

    def test_three_distinct_source_targets_per_repository(self):
        cases = self.manifest["cases"]
        self.assertEqual(Counter(case["repo"] for case in cases), dict.fromkeys(REPOSITORIES, 3))
        self.assertEqual(len({case["id"] for case in cases}), 27)
        self.assertEqual(
            len({(case["source"], case["original"]["span"]["start_byte"]) for case in cases}), 27,
        )
        self.assertEqual(Counter(case["mode"] for case in cases), {"exec": 20, "proof": 7})
        for case in cases:
            if case["mode"] == "exec":
                self.assertEqual(bool(case["view_inputs"]), case["abstract"] != "not_applicable", case["id"])
        for repo in REPOSITORIES:
            selected = [case for case in cases if case["repo"] == repo]
            if any(case["mode"] == "exec" for case in selected):
                self.assertTrue(any(case.get("view_inputs") for case in selected), repo)
            else:
                self.assertIn(repo, self.manifest["coverage_notes"])
                self.assertTrue(all(case["abstract"] == "not_applicable" for case in selected))

    def test_fixture_tree_is_small_and_fully_manifested(self):
        files = {path.relative_to(FIXTURE_ROOT).as_posix() for path in FIXTURE_ROOT.rglob("*.rs")}
        self.assertEqual(files, set(self.fixtures))
        self.assertLess(sum(entry["fixture_bytes"] for entry in self.fixtures.values()), 250_000)
        selection = (TOOL_ROOT / "examples/verusage/selection.json").read_bytes()
        self.assertEqual(sha256(selection), self.manifest["selection_sha256"])
        for entry in self.fixtures.values():
            with self.subTest(fixture=entry["path"]):
                path = contained_path(FIXTURE_ROOT, entry["path"])
                self.assertFalse(path.is_symlink())
                data = path.read_bytes()
                self.assertEqual(sha256(data), entry["fixture_sha256"])
                self.assertEqual(len(data), entry["fixture_bytes"])
                self.assertFalse(Path(entry["source"]).is_absolute())
                self.assertNotIn("..", Path(entry["source"]).parts)
                self.assertEqual(len(entry["full_source_sha256"]), 64)
                if entry["classification"] == "full_source":
                    self.assertEqual(entry["fixture_sha256"], entry["full_source_sha256"])
                    self.assertEqual(entry["declared_edits"], [])
                    self.assertEqual(entry["omitted_source_lines"], [])
                else:
                    self.assertTrue(entry["transformations"])
                    self.assertTrue(entry["declared_edits"])

    def test_every_copied_dependency_span_is_byte_identical(self):
        for entry in self.fixtures.values():
            with self.subTest(fixture=entry["path"]):
                data = contained_path(FIXTURE_ROOT, entry["path"]).read_bytes()
                self.assertTrue(entry["dependencies"])
                self.assertTrue(entry["copied_spans"])
                covered = set()
                for copied in entry["copied_spans"]:
                    value = span_bytes(data, copied["fixture"])
                    self.assertEqual(sha256(value), copied["fixture"]["sha256"])
                    self.assertEqual(sha256(value), copied["source"]["sha256"])
                    covered.update(range(copied["source"]["start_line"], copied["source"]["end_line"] + 1))
                omitted = {
                    line for first, last in entry["omitted_source_lines"]
                    for line in range(first, last + 1)
                }
                self.assertFalse(covered & omitted)
                self.assertEqual(covered | omitted, set(range(1, entry["full_source_lines"] + 1)))
                for edit in entry["declared_edits"]:
                    self.assertEqual(
                        sha256(edit["replacement"].encode("utf-8")), edit["replacement_sha256"],
                    )

    def test_selected_original_signatures_and_clauses_are_unchanged(self):
        for case in self.manifest["cases"]:
            with self.subTest(case=case["id"]):
                entry = self.fixtures[case["fixture"]]
                data = contained_path(FIXTURE_ROOT, case["fixture"]).read_bytes()
                selected = selected_function(data, case)
                self.assertEqual(selected, case["selected"])
                self.assertEqual(case["source_sha256"], entry["full_source_sha256"])
                self.assertEqual(case["fixture_sha256"], entry["fixture_sha256"])
                self.assertEqual(selected["header"], case["original"]["header"])
                self.assertEqual(selected["header"], case["selected_original_snippet"])
                self.assertEqual(selected["owners"], case["original"]["owners"])
                self.assertEqual(selected["mode"], case["original"]["mode"])
                self.assertEqual(
                    sha256(selected["header"].encode("utf-8")), case["original"]["header_span"]["sha256"],
                )
                changed = selected["span"]["sha256"] != case["original"]["span"]["sha256"]
                self.assertEqual(changed, case["implementation_body_changed"])
                if changed:
                    self.assertEqual(entry["classification"], "contract_only")
                    self.assertTrue(selected["external_body"])
                    self.assertEqual(
                        span_bytes(data, selected["body_span"]).decode("utf-8").strip(),
                        "{\n    unimplemented!()\n}",
                    )

    def test_all_copied_spec_and_view_definitions_keep_exact_context(self):
        for entry in self.fixtures.values():
            with self.subTest(fixture=entry["path"]):
                data = contained_path(FIXTURE_ROOT, entry["path"]).read_bytes()
                specs = [item for item in functions(data) if item["mode"] == "spec"]
                recorded = entry["copied_spec_functions"]
                self.assertEqual(len(specs), len(recorded))
                for item, saved in zip(specs, recorded):
                    self.assertEqual(item["qualified_name"], saved["qualified_name"])
                    self.assertEqual(item["span"], saved["fixture"])
                    self.assertEqual(item["span"]["sha256"], saved["source"]["sha256"])
                    self.assertEqual(item["external_body"], saved["originally_external_body"])
                self.assertEqual(
                    sum(saved["is_view"] for saved in recorded),
                    sum(item["name"] == "view" for item in specs),
                )
        views = []
        for path in ("atmosphere/va_range_new.rs", "atmosphere/va_range_index.rs"):
            view = next(
                item for item in self.fixtures[path]["copied_spec_functions"]
                if item["qualified_name"] == "VaRange4K::view"
            )
            views.append(view)
        self.assertNotEqual(views[0]["source"]["sha256"], views[1]["source"]["sha256"])
        self.assertFalse(views[0]["originally_external_body"])
        self.assertTrue(views[1]["originally_external_body"])

    def test_legacy_mutable_semantics_are_explicit_not_rewritten(self):
        entry = self.fixtures["vest/set_range.rs"]
        data = contained_path(FIXTURE_ROOT, entry["path"]).read_bytes()
        prefix = b"#![verifier::deprecated_postcondition_mut_ref_style(true)]\n"
        self.assertEqual(entry["classification"], "compatibility_annotated")
        self.assertTrue(data.startswith(prefix))
        self.assertEqual(sha256(data[len(prefix):]), entry["full_source_sha256"])
        case = next(case for case in self.manifest["cases"] if case["id"] == "vest.set-range")
        self.assertIn("data@ == seq_splice(old(data)@, i, input@)", case["selected"]["header"])
        self.assertNotIn("final(", case["selected"]["header"])
        self.assertFalse(case["implementation_body_changed"])

    def test_backend_discovers_exact_exec_targets_and_excludes_proof_targets(self):
        output = TOOL_ROOT / "examples/verusage/runs/structural-not-created"
        for case in self.manifest["cases"]:
            with self.subTest(case=case["id"]):
                path = contained_path(FIXTURE_ROOT, case["fixture"])
                files = {path.name: sha256(path.read_bytes())}
                project = PreparedProject(path.parent, path.parent, digest(files), files)
                config = case_config(case, "concrete", output, ToolchainConfig())
                backend = VerusBackend(config)
                targets, _ = backend.discover(project)
                self.assertEqual(config.include, (path.name,))
                if case["mode"] == "exec":
                    selected = select_targets(targets, config.selectors)
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(selected[0].name, case["function"])
                    self.assertEqual(selected[0].line, case["selected"]["span"]["start_line"])
                else:
                    self.assertFalse(any(target.name == case["function"] for target in targets))
                self.assertEqual(sha256(path.read_bytes()), self.fixtures[case["fixture"]]["fixture_sha256"])

    def test_direct_abstract_probes_require_an_explicit_override(self):
        case = next(case for case in self.manifest["cases"] if case["id"] == "ironkv.vec-match")
        output = TOOL_ROOT / "examples/verusage/runs/structural-not-created"
        default = case_config(case, "abstract", output, ToolchainConfig())
        direct = case_config(
            case, "abstract", output, ToolchainConfig(), abstract_require_concrete=False,
        )
        self.assertEqual(default.analysis_kind, "abstract_determinism")
        self.assertTrue(default.abstract_require_concrete)
        self.assertFalse(direct.abstract_require_concrete)
        self.assertEqual(default.project_root, direct.project_root)
        self.assertEqual(default.selectors, direct.selectors)
        self.assertEqual(default.abstract_inputs, direct.abstract_inputs)


class VerusageOutcomeTests(unittest.TestCase):
    def report(self, status="unknown", *, verdict="inconclusive", role="baseline"):
        return {
            "status": "completed", "verdict": verdict, "problem_id": "frozen",
            "baseline": {"status": status, "role": role, "problem_id": "frozen"},
            "proofs": [], "diagnostics": [],
        }

    def test_unknown_is_not_a_counterexample_or_a_determinism_proof(self):
        negative = {"concrete": "nondeterministic_or_unknown"}
        result = evaluate_report(negative, "concrete", self.report(), 3)
        self.assertTrue(result["matched"])
        self.assertFalse(result["semantic_coverage"])
        self.assertEqual(result["outcome"], "inconclusive")
        positive = {"concrete": "deterministic"}
        self.assertFalse(evaluate_report(positive, "concrete", self.report(), 3)["matched"])
        self.assertFalse(evaluate_report(
            negative, "concrete", self.report(verdict="nondeterministic"), 1,
        )["matched"])

    def test_only_confirmed_same_problem_sat_counts(self):
        case = {"concrete": "nondeterministic_or_unknown"}
        sat = self.report("sat", verdict="nondeterministic")
        self.assertTrue(evaluate_report(case, "concrete", sat, 1)["semantic_coverage"])
        sat["baseline"]["problem_id"] = "different"
        self.assertFalse(evaluate_report(case, "concrete", sat, 1)["matched"])
        restricted_unsat = self.report("unsat", verdict="deterministic", role="refinement")
        self.assertFalse(evaluate_report(
            {"concrete": "deterministic"}, "concrete", restricted_unsat, 0,
        )["matched"])

    def test_compiler_and_unsupported_failures_never_match_unknown(self):
        case = {"concrete": "nondeterministic_or_unknown"}
        for status in ("failed", "unsupported", "running"):
            with self.subTest(status=status):
                report = self.report()
                report["status"] = status
                result = evaluate_report(case, "concrete", report, 2)
                self.assertFalse(result["matched"])
                self.assertFalse(result["semantic_coverage"])
        report = self.report()
        report["proofs"] = [{"status": "compile_error"}]
        self.assertFalse(evaluate_report(case, "concrete", report, 3)["matched"])

    def test_no_view_is_explicitly_not_an_abstract_proof(self):
        report = {
            "status": "not_applicable", "verdict": "not_evaluated",
            "diagnostics": [{"code": "no_abstract_inputs", "message": "No input View"}],
        }
        result = evaluate_report({"abstract": "not_applicable"}, "abstract", report, 0)
        self.assertTrue(result["matched"])
        self.assertFalse(result["semantic_coverage"])
        report["diagnostics"] = []
        self.assertFalse(evaluate_report({"abstract": "not_applicable"}, "abstract", report, 0)["matched"])

    def skipped_abstract(self, concrete):
        return {
            "status": "skipped", "verdict": "not_evaluated", "problem_id": "",
            "concrete_result": concrete, "baseline": None, "proofs": [], "search": None,
            "diagnostics": [{
                "code": "concrete_prerequisite_unproved",
                "message": "Abstract checking requires concrete determinism",
                "severity": "warning",
            }],
        }

    def test_unknown_or_sat_prerequisite_does_not_become_abstract_coverage(self):
        case = {"concrete": "nondeterministic_or_unknown", "abstract": "nondeterministic_or_unknown"}
        for status, verdict in (("unknown", "inconclusive"), ("sat", "nondeterministic")):
            with self.subTest(status=status):
                report = self.skipped_abstract(self.report(status, verdict=verdict))
                result = evaluate_report(case, "abstract", report, 3)
                self.assertTrue(result["matched"])
                self.assertFalse(result["semantic_coverage"])
                self.assertEqual(result["solver_statuses"], [])
                self.assertEqual(result["outcome"], "concrete_prerequisite_not_proved")
                self.assertEqual(result["concrete_prerequisite"]["solver_statuses"], [status])
                self.assertEqual(
                    result["concrete_prerequisite"]["api_exit_code"], 3 if status == "unknown" else 1,
                )

    def test_failed_prerequisite_and_missing_skip_evidence_are_failures(self):
        case = {"concrete": "nondeterministic_or_unknown", "abstract": "nondeterministic_or_unknown"}
        concrete = self.report()
        concrete.update(status="failed", proofs=[{"status": "compile_error"}])
        result = evaluate_report(case, "abstract", self.skipped_abstract(concrete), 3)
        self.assertFalse(result["matched"])
        self.assertFalse(result["semantic_coverage"])
        for change in (
            {"status": "completed"},
            {"diagnostics": []},
            {"baseline": self.report()["baseline"]},
        ):
            with self.subTest(change=change):
                report = self.skipped_abstract(self.report())
                report.update(change)
                self.assertFalse(evaluate_report(case, "abstract", report, 3)["matched"])

    def test_concrete_proof_cannot_be_reused_as_an_abstract_proof(self):
        case = {"concrete": "deterministic", "abstract": "deterministic"}
        report = self.report("unsat", verdict="deterministic")
        report["concrete_result"] = self.report("unsat", verdict="deterministic")
        self.assertFalse(evaluate_report(case, "abstract", report, 0)["matched"])
        report["problem_id"] = "paired-inputs"
        report["baseline"]["problem_id"] = "paired-inputs"
        self.assertTrue(evaluate_report(case, "abstract", report, 0)["semantic_coverage"])

    def test_all_declared_view_inputs_must_be_paired_independently(self):
        case = {"abstract": "deterministic", "view_inputs": ["data", "input"]}
        pairs = [
            {
                "parameter": name, "mode": "paired", "left": f"left_{name}", "right": f"right_{name}",
                "relation": "view_equivalence", "view_type": "Seq<u8>", "source": "type_view",
                "state": "pre" if name == "data" else "value",
            }
            for name in case["view_inputs"]
        ]
        report = self.report("unsat", verdict="deterministic")
        report["coverage"] = {"input_observations": pairs}
        self.assertTrue(evaluate_report(case, "abstract", report, 0)["semantic_coverage"])
        for change in (
            {"mode": "shared"},
            {"left": "left_data"},
            {"right": "left_input"},
            {"parameter": "data"},
            {"source": ""},
        ):
            with self.subTest(change=change):
                altered = [dict(pair) for pair in pairs]
                altered[1].update(change)
                report["coverage"]["input_observations"] = altered
                result = evaluate_report(case, "abstract", report, 0)
                self.assertFalse(result["matched"])
                self.assertFalse(result["semantic_coverage"])


@unittest.skipUnless(os.environ.get("SPECDET_VERUS"), "Set SPECDET_VERUS to run the real VeruSAGE corpus")
class VerusageVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest()
        cls.toolchain = verifier_from_environment()
        configured = os.environ.get("SPECDET_VERUS_RUN_DIR")
        base = Path(configured).resolve() if configured else TOOL_ROOT / "examples/verusage/runs"
        cls.output = base / f"unittest-{uuid4().hex}"
        if cls.output.is_relative_to(FIXTURE_ROOT) or FIXTURE_ROOT.is_relative_to(cls.output):
            raise ValueError("Verifier evidence must not overlap fixtures")
        cls.output.mkdir(parents=True)
        cls.keep = bool(configured)
        cls.records = []

    @classmethod
    def tearDownClass(cls):
        summary = summarize(cls.records)
        (cls.output / "corpus-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        if not cls.keep and summary["exit_code"] == 0:
            shutil.rmtree(cls.output)

    def assert_corpus(self, analysis):
        for case in self.manifest["cases"]:
            with self.subTest(repo=case["repo"], case=case["id"], analysis=analysis):
                result = run_case(case, analysis, self.output, toolchain=self.toolchain)
                self.records.append(result)
                self.assertTrue(result["matched"], json.dumps(result, indent=2))

    def test_concrete_corpus(self):
        self.assert_corpus("concrete")

    @unittest.skipUnless(
        os.environ.get("SPECDET_VERUS_ABSTRACT") == "1",
        "Set SPECDET_VERUS_ABSTRACT=1 once the paired-input abstract backend is ready",
    )
    def test_abstract_corpus(self):
        self.assert_corpus("abstract")


if __name__ == "__main__":
    unittest.main()
