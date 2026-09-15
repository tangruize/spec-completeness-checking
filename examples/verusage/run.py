"""Run sealed VeruSAGE contracts without the original corpus or a live LLM."""

from __future__ import annotations

import argparse
import json
import os
import traceback
from collections import Counter, defaultdict
from pathlib import Path

from specdet.adapters.verus.backend import VerusBackend
from specdet.api import analyze, result_exit_code
from specdet.config import Config, Limits, ToolchainConfig
from specdet.domain.models import digest

from .provenance import FIXTURE_ROOT, MANIFEST_PATH, contained_path, sha256
from .reference_proofs import has_reference, reference_driver


def load_manifest() -> dict:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest["schema_version"] != 1:
        raise ValueError("Unsupported VeruSAGE fixture manifest")
    for fixture in manifest["fixtures"]:
        path = contained_path(FIXTURE_ROOT, fixture["path"])
        if sha256(path.read_bytes()) != fixture["fixture_sha256"]:
            raise ValueError(f"Fixture digest mismatch: {fixture['path']}")
    return manifest


def verifier_from_environment() -> ToolchainConfig:
    executable = os.environ.get("SPECDET_VERUS", "")
    if not executable:
        raise ValueError("Set SPECDET_VERUS to a non-mutating native verifier executable")
    path = Path(executable).expanduser().resolve()
    with path.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise ValueError("This corpus runner requires the native ELF, not a source-rewriting wrapper")
    return ToolchainConfig(
        executable=str(path),
        rust_toolchain=os.environ.get("SPECDET_RUST_TOOLCHAIN", ""),
    )


def case_config(
    case: dict, analysis: str, output: Path, toolchain: ToolchainConfig,
    *, abstract_require_concrete: bool | None = None,
) -> Config:
    source = contained_path(FIXTURE_ROOT, case["fixture"])
    return Config(
        source.parent, output,
        include=(source.name,),
        selectors=(case["selector"],) if case["mode"] == "exec" else (),
        toolchain=toolchain,
        limits=Limits(
            solver_timeout_ms=1000, max_search_rounds=8, verifier_timeout_seconds=30,
        ),
        analysis_kind=f"{analysis}_determinism",
        # An absence-of-input-View test is about applicability, not its concrete prerequisite.
        abstract_require_concrete=(
            bool(case.get("view_inputs"))
            if abstract_require_concrete is None else abstract_require_concrete
        ),
        proof_strategies=("baseline", "rules", "accepted", "assisted") if has_reference(case) else ("baseline", "rules"),
        max_proof_attempts=3 if has_reference(case) else 2,
        offline=True,
        assistance={"mode": "off"},
    )


def query_evidence(report: dict) -> list[dict]:
    baseline = report.get("baseline")
    evidence = [baseline] if baseline else []
    evidence.extend((report.get("search") or {}).get("evidence", []))
    return [
        item for item in evidence
        if item.get("problem_id") == report.get("problem_id")
        and item.get("role") in {"baseline", "refinement"}
    ]


def evaluate_report(case: dict, analysis: str, report: dict, api_code: int) -> dict:
    expected = case[analysis]
    status, verdict = report.get("status"), report.get("verdict")
    diagnostics = report.get("diagnostics", [])
    errors = [
        {"code": item["code"], "message": item["message"]}
        for item in diagnostics if item.get("severity", "error") == "error"
    ]
    evidence = query_evidence(report)
    statuses = sorted({item["status"] for item in evidence})
    witness = report.get("counterexample") or {}
    source_confirmed = (
        witness.get("problem_id") == report.get("problem_id")
        and witness.get("kind") == "source_verified_constructive"
        and witness.get("status") == "verified"
        and witness.get("verified_goals", 0) > 0
        and bool(witness.get("certificate_digest"))
        and witness.get("bindings_digest") == digest([
            witness.get("bindings"), witness.get("type_arguments"),
        ])
    )
    result = {
        "expected": expected,
        "status": status,
        "verdict": verdict,
        "solver_statuses": statuses,
        "api_exit_code": api_code,
        "matched": False,
        "semantic_coverage": False,
        "outcome": "failure",
        "errors": errors,
    }
    bad_proof = any(
        proof.get("status") in {"compile_error", "error", "timeout"}
        for proof in report.get("proofs", [])
    )
    if status in {"failed", "unsupported", "running"} or api_code == 2 or bad_proof:
        result["reason"] = "Infrastructure/translation failure is not semantic coverage"
        return result
    if expected == "not_applicable":
        no_view = any(item["code"] == "no_abstract_inputs" for item in diagnostics)
        matched = analysis == "abstract" and status == "not_applicable" and no_view
        result.update(
            matched=matched,
            outcome="not_applicable" if matched else "failure",
            reason="No source-defined nontrivial input View; not an abstract proof",
        )
        return result
    concrete = report.get("concrete_result")
    if analysis == "abstract" and concrete and (
        concrete.get("status") != "completed" or concrete.get("verdict") != "deterministic"
    ):
        prerequisite = evaluate_report(case, "concrete", concrete, result_exit_code([concrete]))
        skipped = (
            status == "skipped" and verdict == "not_evaluated"
            and not report.get("problem_id") and not report.get("baseline")
            and not report.get("proofs") and not report.get("search")
            and any(item["code"] == "concrete_prerequisite_unproved" for item in diagnostics)
        )
        matched = expected == "nondeterministic_or_unknown" and prerequisite["matched"] and skipped
        result.update(
            matched=matched,
            outcome="concrete_prerequisite_not_proved" if matched else "failure",
            reason="Abstract analysis was not performed because concrete determinism was not proved",
            concrete_prerequisite=prerequisite,
        )
        return result
    if analysis == "abstract" and concrete and (
        report.get("problem_id") and report["problem_id"] == concrete.get("problem_id")
    ):
        result["reason"] = "Concrete evidence cannot establish a distinct paired-input abstract obligation"
        return result
    if analysis == "abstract" and case.get("view_inputs"):
        pairs = [
            item for item in report.get("coverage", {}).get("input_observations", [])
            if item.get("mode") == "paired"
        ]
        expected_inputs = set(case["view_inputs"])
        paired_inputs = [item["parameter"] for item in pairs]
        symbols = [item.get(side) for item in pairs for side in ("left", "right")]
        independent = (
            all(isinstance(symbol, str) and symbol for symbol in symbols)
            and len(set(symbols)) == len(symbols)
            and all(
                item.get("relation") == "view_equivalence" and item.get("view_type")
                and item.get("source") and item.get("state") in {"pre", "value"}
                for item in pairs
            )
        )
        result["expected_paired_inputs"] = sorted(expected_inputs)
        result["paired_inputs"] = sorted(paired_inputs)
        if (
            set(paired_inputs) != expected_inputs
            or len(paired_inputs) != len(expected_inputs) or not independent
        ):
            result["reason"] = "The declared source View inputs are not all independently paired"
            return result
    proved = (
        any(item["status"] == "unsat" and item["role"] == "baseline" for item in evidence)
        or any(
            proof.get("status") == "verified" and proof.get("verified_goals", 0) > 0
            and proof.get("problem_id") == report.get("problem_id")
            for proof in report.get("proofs", [])
        )
    )
    if expected == "deterministic":
        matched = status == "completed" and verdict == "deterministic" and proved and "sat" not in statuses and not source_confirmed
        result.update(
            matched=matched, semantic_coverage=matched,
            outcome="proved_deterministic" if matched else "failure",
            reason="The original contract must prove uniqueness, not merely fail to produce a counterexample",
        )
        return result
    if expected != "nondeterministic_or_unknown":
        raise ValueError(f"Unknown expectation: {expected}")
    if status == "completed" and verdict == "nondeterministic" and source_confirmed and not proved:
        result.update(
            matched=True, semantic_coverage=True, outcome="source_confirmed_nondeterministic",
            reason="Concrete inputs and both distinct contract-admitted outputs have an original-source verifier certificate; raw SMT status is unchanged",
        )
    elif status == "completed" and verdict == "nondeterministic" and "sat" in statuses and not proved:
        result.update(
            matched=True, semantic_coverage=True, outcome="confirmed_nondeterministic",
            reason="A confirmed SAT query witnesses contract non-uniqueness",
        )
    elif status == "completed" and verdict == "inconclusive" and "unknown" in statuses and not proved and "sat" not in statuses:
        result.update(
            matched=True, outcome="inconclusive",
            reason="UNKNOWN is an allowed bounded-search limitation, not evidence of nondeterminism",
        )
    else:
        result["reason"] = "An underconstrained contract must not be blessed as deterministic or treated as SAT without evidence"
    return result


def run_case(
    case: dict, analysis: str, run_dir: Path, *, toolchain: ToolchainConfig | None = None,
    abstract_require_concrete: bool | None = None,
) -> dict:
    source = contained_path(FIXTURE_ROOT, case["fixture"])
    before = sha256(source.read_bytes())
    mode = "abstract-direct" if analysis == "abstract" and abstract_require_concrete is False else analysis
    output = (run_dir / case["id"] / mode).resolve()
    if output.is_relative_to(FIXTURE_ROOT) or FIXTURE_ROOT.is_relative_to(output):
        raise ValueError("Evidence must not overlap fixtures")
    output.mkdir(parents=True, exist_ok=True)
    record = {
        "id": case["id"], "repo": case["repo"], "analysis": analysis,
        "fixture": case["fixture"], "fixture_sha256": before,
        "workflow": mode,
    }
    try:
        if before != case["fixture_sha256"]:
            raise ValueError("Fixture differs from its sealed manifest")
        if case["mode"] != "exec":
            config = case_config(case, analysis, output, ToolchainConfig())
            summary, code = analyze(config, backend=VerusBackend(config), discover_only=True)
            discovered = summary.get("targets", [])
            excluded = not any(
                target["name"] == case["function"]
                and target["line"] == case["selected"]["span"]["start_line"]
                for target in discovered
            )
            matched = code == 0 and excluded
            record.update(
                matched=matched,
                semantic_coverage=False, outcome="not_applicable" if matched else "failure",
                status="not_applicable", verdict="not_evaluated", solver_statuses=[],
                reason=f"Original {case['mode']} function excluded from executable discovery; not compiled or proved",
                api_exit_code=code,
            )
        else:
            config = case_config(
                case, analysis, output, toolchain or verifier_from_environment(),
                abstract_require_concrete=abstract_require_concrete,
            )
            summary, code = analyze(
                config, backend=VerusBackend(config),
                driver=reference_driver(case) if has_reference(case) else None,
            )
            reports = summary.get("results", [])
            if len(reports) != 1:
                raise ValueError(f"Expected one selected report, got {len(reports)}: {summary.get('diagnostics')}")
            report = reports[0]
            if report["target"]["name"] != case["function"]:
                raise ValueError("API analyzed a different target")
            if report.get("analysis_kind") != f"{analysis}_determinism":
                raise ValueError("API reported a different analysis kind")
            record.update(evaluate_report(case, analysis, report, code))
            if has_reference(case):
                record["proof_candidate_source"] = "offline_reference"
                record["live_model_called"] = False
        record["summary_path"] = str(Path(summary["run_dir"]) / "summary.json")
    except Exception as error:
        transcript = output / "runner-error.txt"
        transcript.write_text(traceback.format_exc(), encoding="utf-8")
        record.update(
            matched=False, semantic_coverage=False, outcome="failure",
            status="runner_error", reason=f"{type(error).__name__}: {error}",
            error_path=str(transcript),
        )
    finally:
        if sha256(source.read_bytes()) != before:
            record.update(
                matched=False, semantic_coverage=False, outcome="failure",
                status="fixture_modified", reason="Analysis changed the sealed fixture",
            )
    (output / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def summarize(records: list[dict]) -> dict:
    grouped = defaultdict(Counter)
    for record in records:
        grouped[record["repo"]][f"{record['analysis']}:{record['outcome']}"] += 1
    return {
        "cases": records,
        "matched": sum(record["matched"] for record in records),
        "total": len(records),
        "semantic_coverage": sum(record["semantic_coverage"] for record in records),
        "per_repo": {repo: dict(counts) for repo, counts in sorted(grouped.items())},
        "exit_code": 0 if records and all(record["matched"] for record in records) else 1,
        "note": "Matched expectations are not the same as proved semantic coverage. Read per-repository gaps.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="Explicit evidence directory (outside fixtures)")
    parser.add_argument("--analysis", choices=("concrete", "abstract", "both"), default="concrete")
    parser.add_argument(
        "--direct-abstract", action="store_true",
        help="Explicitly bypass the concrete prerequisite for isolated paired-backend probes",
    )
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()
    if args.direct_abstract and args.analysis != "abstract":
        parser.error("--direct-abstract requires --analysis abstract")
    manifest = load_manifest()
    cases = [
        case for case in manifest["cases"]
        if (not args.repo or case["repo"] in args.repo)
        and (not args.case or case["id"] in args.case)
    ]
    if not cases or set(args.repo) - {case["repo"] for case in manifest["cases"]} or set(args.case) - {case["id"] for case in manifest["cases"]}:
        parser.error("Unknown or empty repository/case selection")
    run_dir = args.run_dir.resolve()
    if run_dir.is_relative_to(FIXTURE_ROOT) or FIXTURE_ROOT.is_relative_to(run_dir):
        parser.error("Evidence must not overlap the fixture tree")
    toolchain = verifier_from_environment() if any(case["mode"] == "exec" for case in cases) else ToolchainConfig()
    records = []
    modes = ("concrete", "abstract") if args.analysis == "both" else (args.analysis,)
    for case in cases:
        for analysis in modes:
            record = run_case(
                case, analysis, run_dir, toolchain=toolchain,
                abstract_require_concrete=False if args.direct_abstract else None,
            )
            records.append(record)
            print(f"{case['id']} [{analysis}]: {record['outcome']} - {record.get('reason', '')}", flush=True)
            (run_dir / "corpus-summary.json").write_text(
                json.dumps(summarize(records), indent=2) + "\n", encoding="utf-8",
            )
    summary = summarize(records)
    print(f"{summary['matched']}/{summary['total']} expectations matched; "
          f"{summary['semantic_coverage']} decisive semantic results. Evidence: {run_dir}")
    return summary["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
