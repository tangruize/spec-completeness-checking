"""Check a local HFS v3 checkout without publishing or rewriting its sources."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from specdet.adapters.verus.backend import VerusBackend
from specdet.adapters.verus.execution import classify_process
from specdet.adapters.verus.native.codegen.expressions import rename_free_identifiers
from specdet.adapters.verus.witness_replay import witness_goal
from specdet.config import BuildConfig, Config, Limits, ToolchainConfig
from specdet.domain.models import StageError, as_object, text_digest
from specdet.storage.artifacts import ArtifactStore, file_digest
from specdet.storage.workspace import prepare_project


def conditional_probe_code(obligation) -> tuple[str, str]:
    goal = witness_goal(obligation)
    parameters = dict(goal.parameters)
    if set(parameters) != {"pre_self_", "path", "post1_self_", "post2_self_", "r1", "r2"}:
        raise ValueError("The HFS profile requires one mutable receiver, a path, and a result")
    name = "__specdet_hfs_conditional_error_pair"
    posts = rename_free_identifiers(
        goal.posts, {"post1_self_": "pre_self_", "post2_self_": "pre_self_"},
    )
    code = (
        f"proof fn {name}(pre_self_: {parameters['pre_self_']}, path: {parameters['path']})\n"
        + "    requires " + ", ".join(goal.requires) + ",\n{\n"
        + f"    let r1: {parameters['r1']} = Err(crate::error::HfsError::NotMounted);\n"
        + f"    let r2: {parameters['r2']} = Err(crate::error::HfsError::ReadOnly);\n"
        + f"    assert({posts});\n"
        + "    assert(r1 != r2);\n}\n"
    )
    return name, code


def conditional_probe(backend, project, obligation, directory: Path) -> dict:
    name, code = conditional_probe_code(obligation)
    worktree = directory / "worktree"
    transformations = backend._copy_worktree(project, worktree, {})
    entry, injection, module = backend._build_paths(project, obligation, worktree)
    source = obligation.native["source"]
    harness = backend._inject(source, obligation.native["source_context"], code)
    injection.write_text(harness)
    frozen = {relative: file_digest(worktree / relative) for relative in project.files}
    process = backend.executor.verify(entry, worktree, directory / "verifier", name, module)
    status, verified = classify_process(process)
    changed = [
        relative for relative, expected in frozen.items()
        if not (worktree / relative).is_file() or file_digest(worktree / relative) != expected
    ]
    if changed:
        raise RuntimeError(f"Verifier modified frozen source inputs: {changed}")
    result = {
        "kind": "conditional_source_alternatives",
        "status": "verified_under_precondition" if status == "verified" else status,
        "verified_goals": verified,
        "source_contract_digest": obligation.native["contract_digest"],
        "source_digest": obligation.native["source_digest"],
        "probe_digest": text_digest(code),
        "observation": "different HfsError discriminants, not the observable policy's merged Err class",
        "pre_feasibility": "not_established",
        "nondeterminism_verdict": "not_claimed",
        "question": "For any pre-state satisfying the original requires, do two distinct errors "
                    "with unchanged receiver each satisfy all original postconditions?",
        "verifier": as_object(process),
        "transformations": [*transformations, {
            "kind": "append_conditional_source_probe",
            "file": obligation.target.file,
            "before_digest": text_digest(source),
            "after_digest": text_digest(harness),
        }],
    }
    store = ArtifactStore(directory)
    store.write_text("probe.rs", code)
    store.artifact("evidence.json", "conditional_source_alternatives", result, (obligation.id,))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verus", type=Path)
    parser.add_argument("--timeout", type=int, default=120, help="Per-verifier-call budget in seconds")
    args = parser.parse_args()
    root = args.source_root.resolve()
    output = args.out.resolve()
    checker_root = Path(__file__).resolve().parents[3]
    if output.is_relative_to(root) or output.is_relative_to(checker_root):
        parser.error("--out must be outside both the source checkout and checker repository")
    executable = args.verus or root / "toolchain/verus/verus"
    run = output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    config = Config(
        root, output, include=("libhfs/src/volume.rs",),
        exclude=(
            ".git/**", ".argus/**", ".argus_subagents/**", ".verus_agent/**",
            ".copilot/**", "research/**", "build/**", "target/**",
            "toolchain/verus/**", "toolchain/verus-src/.git/**",
            "toolchain/verus-src/target/**",
        ),
        selectors=("open_file",), offline=True, counterexample_candidates=0,
        max_proof_attempts=1, proof_strategies=("baseline",),
        build=BuildConfig(
            adapter="verus.cargo", package="libhfs", injection_file="libhfs/src/volume.rs",
            extra_args=("--no-default-features",),
        ),
        toolchain=ToolchainConfig(
            executable=str(executable),
            environment={"CARGO_TARGET_DIR": str(output / "cargo-target")},
        ),
        limits=Limits(
            verifier_timeout_seconds=args.timeout, solver_timeout_ms=1500, max_search_rounds=2,
        ),
    )
    config.validate()
    store = ArtifactStore(run)
    started = time.monotonic()
    result = {"run_dir": str(run), "scope": "native HFS crate, unchanged source contract"}
    timings = {}
    try:
        project = prepare_project(config, run)
        timings["snapshot_seconds"] = round(time.monotonic() - started, 3)
        backend = VerusBackend(config)
        store.artifact("effective-config.json", "effective_config", config.to_dict())
        store.artifact("toolchain.json", "toolchain", backend.executor.identity())
        targets, diagnostics = backend.discover(project)
        selected = [target for target in targets if target.name == "open_file"]
        if len(selected) != 1:
            raise ValueError(f"Expected exactly one open_file target, found {len(selected)}")
        store.artifact("discovery.json", "discovery", {
            "targets": [as_object(item) for item in targets],
            "diagnostics": [as_object(item) for item in diagnostics],
        })
        contract = backend.extract(project, selected[0])
        observations = backend.observations(project, contract)
        obligation = backend.lower(project, contract, observations)
        store.artifact("contract.json", "contract", as_object(contract))
        store.artifact("observations.json", "observations", as_object(observations))
        store.artifact("obligation.json", "obligation", as_object(obligation))
        result["ignored_dimensions"] = list(observations.ignored_dimensions)
        timings["prepare_and_lower_seconds"] = round(time.monotonic() - started, 3)
        phase_started = time.monotonic()
        result["conditional_probe"] = conditional_probe(
            backend, project, obligation, run / "conditional-probe",
        )
        timings["conditional_probe_seconds"] = round(time.monotonic() - phase_started, 3)
        candidate = backend.generate_proof(project, contract, obligation, "baseline")
        check = backend.check(project, obligation, candidate, run / "baseline", baseline=True)
        result["baseline_status"] = check.status
        timings["baseline_seconds"] = round(check.duration_ms / 1000, 3)
        if check.status in {"verified", "unproved", "timeout"}:
            result["query"] = as_object(backend.query(obligation, check))
        result["source_contract_digest"] = contract.id
        result["status"] = "completed"
    except StageError as error:
        result.update(status="failed", diagnostic=as_object(error.diagnostic))
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["timings"] = timings
    errors = {"failed", "compile_error", "error", "rejected"}
    if (result["status"] in errors or result.get("baseline_status") in errors
            or result.get("conditional_probe", {}).get("status") in errors):
        code = 2
    elif result.get("query", {}).get("status") == "unsat":
        code = 0
    else:
        code = 3
    result["exit_code"] = code
    store.artifact("summary.json", "hfs_evidence", result)
    print(json.dumps({
        "run_dir": str(run), "status": result["status"],
        "baseline": result.get("baseline_status"),
        "conditional_probe": result.get("conditional_probe", {}).get("status"),
        "duration_seconds": result["duration_seconds"],
        "diagnostic": result.get("diagnostic"),
        "exit_code": code,
    }, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
