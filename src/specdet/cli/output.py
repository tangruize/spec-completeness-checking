"""Bounded presentation of recorded evidence, without reclassifying a result."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys

from specdet.domain.models import JsonObject


FORMAT = "specdet.summary.v1"
_MESSAGE_LIMIT = 500
_BINDING_LIMIT = 4096
_OBSERVATION_LIMIT = 1024
_DIAGNOSTIC_LIMIT = 3
_ARTIFACT_KINDS = {"contract", "observation_plan", "obligation", "counterexample_search", "search_result"}


def _text(value: object, limit: int = _MESSAGE_LIMIT) -> str:
    text = str(value)
    marker = "... [truncated]"
    return text if len(text) <= limit else text[:limit - len(marker)] + marker


def _object(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Report object has an invalid shape")
    return value


def _diagnostics(entries: list) -> tuple[list[JsonObject], int]:
    unique = {}
    for entry in entries:
        item = _object(entry)
        if item.get("severity") == "info" and not item.get("code", "").endswith("_budget_exhausted"):
            continue
        brief = {key: item.get(key) for key in ("stage", "code", "severity")}
        brief["message"] = _text(item.get("message", ""))
        for invocation in _object(item.get("details")).get("functions", []):
            stderr = _object(invocation.get("process")).get("stderr", "")
            first_error = next(
                (line.strip() for line in stderr.splitlines() if line.lstrip().startswith(("error:", "error["))),
                None,
            )
            if first_error:
                brief["verifier_error"] = _text(first_error)
                break
        unique.setdefault((brief["stage"], brief["code"], brief["message"]), brief)
    priority = {"error": 0, "warning": 1, "info": 2}
    items = sorted(unique.values(), key=lambda item: (
        item.get("code") in {"unproved", "extraction_gap", "partial_discovery"},
        priority.get(item.get("severity"), 3),
    ))
    selected = {}
    for item in items:
        selected.setdefault((item["stage"], item["code"]), item)
    preview = list(selected.values())[:_DIAGNOSTIC_LIMIT]
    return preview, len(items) - len(preview)


def _query(query: dict) -> JsonObject:
    return {
        **{key: query.get(key) for key in ("status", "role", "artifact", "query_digest")},
        "reason": _text(query["reason"]) if query.get("reason") else query.get("reason"),
        "constraint_count": len(query.get("constraints", [])),
    }


def _evidence(result: dict) -> JsonObject | None:
    verdict, problem = result.get("verdict"), result.get("problem_id")
    witness = _object(result.get("counterexample"))
    if (
        verdict == "nondeterministic" and witness.get("status") == "verified"
        and witness.get("problem_id") == problem and witness.get("verified_goals", 0) > 0
        and witness.get("kind") == "source_verified_constructive" and witness.get("certificate_digest")
    ):
        return {key: witness.get(key) for key in (
            "kind", "status", "verified_goals", "artifact", "certificate_digest",
        )}
    if verdict == "deterministic":
        for proof in result.get("proofs", []):
            if proof.get("status") == "verified" and proof.get("verified_goals", 0) > 0 and proof.get("problem_id") == problem:
                return {
                    "kind": "verified_original_obligation", "status": "verified",
                    "verified_goals": proof["verified_goals"], "artifact": proof.get("artifact"),
                }
    desired = {"deterministic": "unsat", "nondeterministic": "sat"}.get(verdict)
    if desired is None:
        return None
    queries = [_object(result.get("baseline")), *_object(result.get("search")).get("evidence", [])]
    for query in queries:
        if query.get("problem_id") != problem or query.get("status") != desired:
            continue
        if query.get("role") not in ({"baseline"} if desired == "unsat" else {"baseline", "refinement"}):
            continue
        return {"kind": "solver_query", **_query(query)}
    return None


def _result(result: dict) -> JsonObject:
    coverage = _object(result.get("coverage"))
    brief_coverage = {
        key: _text(value) if isinstance(value, str) else value
        for key, value in coverage.items() if not isinstance(value, (dict, list))
    }
    for key, limit in (("ignored_dimensions", 8), ("input_observations", 4)):
        if key in coverage:
            preview = []
            for item in coverage[key][:limit]:
                if isinstance(item, str):
                    preview.append(_text(item))
                else:
                    encoded = json.dumps(item, ensure_ascii=False)
                    preview.append(item if len(encoded) <= _OBSERVATION_LIMIT else {
                        "preview": _text(encoded, _OBSERVATION_LIMIT), "preview_truncated": True,
                    })
            brief_coverage[key] = preview
            brief_coverage[key + "_omitted"] = max(0, len(coverage[key]) - limit)
    diagnostics, omitted = _diagnostics(result.get("diagnostics", []))
    search = _object(result.get("search"))
    witness = _object(result.get("counterexample"))
    compact_witness = None
    if witness:
        compact_witness = {key: witness.get(key) for key in (
            "kind", "status", "verified_goals", "artifact", "certificate_digest", "bindings_digest",
        )}
        values = {key: witness.get(key) for key in ("bindings", "type_arguments")}
        truncated = len(json.dumps(values, ensure_ascii=False)) > _BINDING_LIMIT
        compact_witness.update(values if not truncated else {"bindings": None, "type_arguments": None})
        compact_witness["values_omitted"] = truncated
    prerequisite = _object(result.get("concrete_result"))
    artifact_dir = result.get("artifact_dir")
    artifacts = {}
    for item in result.get("artifacts", []):
        if item.get("kind") in _ARTIFACT_KINDS:
            path = item.get("path")
            artifacts[item["kind"]] = {
                "path": str(Path(artifact_dir) / path) if artifact_dir and path else path,
                "digest": item.get("digest"),
            }
    return {
        "target": result.get("target"), "status": result.get("status"),
        "verdict": result.get("verdict"), "problem_id": result.get("problem_id"),
        "analysis_kind": result.get("analysis_kind"), "duration_ms": result.get("duration_ms"),
        "artifact_dir": artifact_dir,
        "baseline": _query(_object(result.get("baseline"))) if result.get("baseline") else None,
        "decisive_evidence": _evidence(result), "counterexample": compact_witness,
        "coverage": brief_coverage, "resources": result.get("resources", {}),
        "concrete_prerequisite": {
            key: prerequisite.get(key) for key in ("status", "verdict", "problem_id")
        } if prerequisite else None,
        "proof_status_counts": dict(Counter(proof.get("status", "unknown") for proof in result.get("proofs", []))),
        "search": {
            "rounds": search.get("rounds"), "exhausted": search.get("exhausted"),
            "status_counts": dict(Counter(query.get("status", "unknown") for query in search.get("evidence", []))),
            "refinement_status_counts": dict(Counter(
                query.get("status", "unknown") for query in search.get("evidence", [])
                if query.get("role") == "refinement"
            )),
            "confirmed_constraint_count": len(search.get("confirmed_constraints", [])),
            "candidate_constraint_count": len(search.get("candidate_constraints", [])),
        } if search else None,
        "diagnostics": diagnostics, "diagnostics_omitted": omitted,
        "artifacts": artifacts,
    }


def compact_summary(summary: JsonObject) -> JsonObject:
    """Project only stored facts; omitted detail remains in the original artifacts."""
    diagnostics, omitted = _diagnostics(summary.get("diagnostics", []))
    run_dir = summary.get("run_dir")
    results = summary.get("results", [])
    return {
        "format": FORMAT, "schema_version": 1,
        "run_id": summary.get("run_id"), "run_dir": run_dir,
        "status": summary.get("status"), "exit_code": summary.get("exit_code"),
        "duration_ms": summary.get("duration_ms"),
        "counts": summary.get("counts", {}),
        "discovery_coverage": summary.get("discovery_coverage"),
        "targets": summary.get("targets", []) if not results else [],
        "results": [_result(_object(result)) for result in results],
        "diagnostics": diagnostics, "diagnostics_omitted": omitted,
        "full_report": str(Path(run_dir) / "summary.json") if run_dir else None,
    }


def print_summary(summary: JsonObject, *, json_output: bool = False, compact: bool = False) -> None:
    if json_output or compact:
        print(json.dumps(compact_summary(summary) if compact else summary, ensure_ascii=False, indent=2))
        return
    brief = compact_summary(summary)
    for result in brief["results"]:
        target = _object(result["target"])
        label = f"{target.get('file')}:{target.get('name')}@{target.get('line')}"
        print(f"{str(result['verdict']):18} {label} [{result['status']}]")
        if result["analysis_kind"] == "abstract_determinism":
            print("  analysis: abstract_determinism (input View equivalence)")
        baseline = result["baseline"]
        if baseline:
            reason = f" ({_text(baseline['reason'], 200)})" if baseline["reason"] else ""
            print(f"  baseline: {baseline['status']}{reason}")
        if evidence := result["decisive_evidence"]:
            print(f"  evidence: {evidence['kind']} ({evidence['status']})")
            if evidence.get("artifact"):
                print(f"  evidence artifact: {evidence['artifact']}")
        witness = result["counterexample"]
        if witness and witness["status"] == "verified":
            if witness["values_omitted"]:
                print("  witness values: large; see certificate")
            else:
                print("  witness: " + json.dumps(witness["bindings"], ensure_ascii=False, sort_keys=True))
                if witness.get("type_arguments"):
                    print("  instantiation: " + json.dumps(witness["type_arguments"], sort_keys=True))
        coverage = result["coverage"]
        if policy := coverage.get("observation_policy"):
            print(f"  observation: {policy}")
        if ignored := coverage.get("ignored_dimensions"):
            suffix = f"; +{coverage['ignored_dimensions_omitted']} more" if coverage.get("ignored_dimensions_omitted") else ""
            print("  ignored: " + "; ".join(map(str, ignored)) + suffix)
        if coverage.get("trusted_translation") is False:
            print("  boundary: translation to the analyzed model is not confirmed")
        boundaries = [
            f"{key}={coverage[key]}"
            for key in ("global_or_unmentioned_heap", "pre_feasibility", "contract_feasibility", "domain_preservation")
            if key in coverage
        ]
        if boundaries:
            print("  boundary: " + ", ".join(boundaries))
        if prerequisite := result["concrete_prerequisite"]:
            print(f"  concrete prerequisite: {prerequisite['verdict']} [{prerequisite['status']}]")
        resources = result["resources"]
        budgets = [
            f"{name} {resources[key]['used']}/{resources[key]['limit']}"
            + (" (disabled)" if resources[key]["limit"] == 0 else "")
            for key, name in (("proof_attempts", "proofs"), ("search_rounds", "search rounds"),
                              ("counterexample_candidates", "witness candidates"))
            if key in resources
        ]
        if budgets:
            print("  budget (target total): " + ", ".join(budgets))
        if search := result["search"]:
            counts = ", ".join(f"{status}={count}" for status, count in sorted(search["status_counts"].items()))
            print(f"  search queries: {counts}; exhausted={search['exhausted']}")
            if search["refinement_status_counts"].get("unsat"):
                print("  search boundary: local UNSAT does not prove global determinism")
        if result["duration_ms"] is not None:
            print(f"  elapsed (target): {result['duration_ms'] / 1000:.3f}s")
        if result["verdict"] not in {"deterministic", "nondeterministic"} or result["status"] == "failed":
            for diagnostic in result["diagnostics"]:
                print(f"  diagnostic {diagnostic['stage']}/{diagnostic['code']}: {diagnostic['message']}")
                if diagnostic.get("verifier_error"):
                    print(f"    {diagnostic['verifier_error']}")
            if result["diagnostics_omitted"]:
                print(f"  +{result['diagnostics_omitted']} further diagnostics in full report")
            if result["artifact_dir"]:
                print(f"  detail: {Path(result['artifact_dir']) / 'report.json'}")
    print(f"Run: {brief['run_dir']}")
    if brief["counts"]:
        print(json.dumps(brief["counts"], sort_keys=True))
    for diagnostic in brief["diagnostics"]:
        print(f"{diagnostic['code']}: {diagnostic['message']}", file=sys.stderr)
    if brief["diagnostics_omitted"]:
        print(f"+{brief['diagnostics_omitted']} further project diagnostics in full report", file=sys.stderr)
    if brief["duration_ms"] is not None:
        print(f"Elapsed (whole run): {brief['duration_ms'] / 1000:.3f}s")
    else:
        print("Elapsed (whole run): unavailable in recorded report")
