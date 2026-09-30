"""Bounded, source-preserving native checks of the real mimalloc commit mask."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from specdet.adapters.verus.execution import VerusExecutor, classify_process, run_process
from specdet.api import analyze
from specdet.config import BuildConfig, Config, Limits, ToolchainConfig


FUNCTIONS = ("clear", "set", "create", "next_run")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "--no-pager", "-C", str(root), *arguments],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verus", type=Path, required=True)
    parser.add_argument("--rust-toolchain", default="1.98.1")
    parser.add_argument("--z3", type=Path, required=True)
    parser.add_argument("--libc", type=Path, required=True)
    parser.add_argument("--function", choices=FUNCTIONS, action="append")
    parser.add_argument("--source-seal", type=Path)
    parser.add_argument("--verify-implementation", action="store_true")
    parser.add_argument("--verifier-timeout", type=int, default=30)
    parser.add_argument("--solver-timeout-ms", type=int, default=1000)
    args = parser.parse_args()
    root = args.source_root.resolve()
    source = root / "src"
    output = args.out.resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        parser.error("Evidence output must not overlap the target repository")
    executable, libc, z3 = args.verus.resolve(), args.libc.resolve(), args.z3.resolve()
    with executable.open("rb") as stream:
        is_elf = stream.read(4) == b"\x7fELF"
    if not is_elf:
        parser.error("--verus must be a native ELF, not a source-rewriting wrapper")
    functions = tuple(dict.fromkeys(args.function or FUNCTIONS))
    inputs = {
        path.relative_to(source).as_posix(): sha256(path)
        for path in sorted(source.rglob("*.rs"))
    }
    if not inputs or not (source / "lib.rs").is_file():
        parser.error("--source-root must contain the original src/lib.rs")
    if args.source_seal:
        sealed = json.loads(args.source_seal.read_text())
        if inputs != sealed["source_files"]:
            parser.error("The source tree does not match --source-seal")
    output.mkdir(parents=True, exist_ok=False)
    checker = Path(__file__).resolve().parents[3]
    record = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runner_command": [sys.executable, *sys.argv],
        "source_root": str(root),
        "source_revision": git(root, "rev-parse", "HEAD"),
        "source_working_tree_status": git(root, "status", "--short", "--untracked-files=no", "--", "src"),
        "source_files": inputs,
        "source_transformations": [],
        "implementation_bodies_stubbed": [],
        "checker_revision": git(checker, "rev-parse", "HEAD"),
        "checker_core_files": {
            path.relative_to(checker).as_posix(): sha256(path)
            for path in sorted((checker / "src" / "specdet").rglob("*.py"))
        },
        "verifier": str(executable),
        "verifier_sha256": sha256(executable),
        "rust_toolchain": args.rust_toolchain,
        "z3": str(z3),
        "z3_sha256": sha256(z3),
        "libc": str(libc),
        "libc_sha256": sha256(libc),
        "selected_functions": functions,
        "scope": "Native src/lib.rs module context; original contracts and implementation bodies",
    }
    (output / "inputs.json").write_text(json.dumps(record, indent=2) + "\n")
    config = Config(
        project_root=source,
        output_dir=output / "checker",
        include=("commit_mask.rs",),
        selectors=tuple(f"commit_mask.rs:{name}" for name in functions),
        build=BuildConfig(
            adapter="verus.native",
            entrypoint="lib.rs",
            extra_args=("--crate-type", "lib", "--edition=2021", "--extern", f"libc={libc}"),
        ),
        toolchain=ToolchainConfig(
            executable=str(executable),
            rust_toolchain=args.rust_toolchain,
            environment={"VERUS_Z3_PATH": str(z3)},
        ),
        limits=Limits(
            verifier_timeout_seconds=args.verifier_timeout,
            solver_timeout_ms=args.solver_timeout_ms,
            max_search_rounds=4,
        ),
        proof_strategies=("baseline", "rules"),
        max_proof_attempts=2,
        counterexample_candidates=0,
        offline=True,
        assistance={"mode": "off"},
    )
    implementation = None
    if args.verify_implementation:
        executor = VerusExecutor(config)
        process = run_process(
            [
                str(executable), str(source / "lib.rs"), *config.build.extra_args,
                "--verify-module", "commit_mask", "--time", "--num-threads", "2",
            ],
            output,
            args.verifier_timeout,
            executor.environment,
        )
        status, verified = classify_process(process)
        implementation = {**asdict(process), "status": status, "verified_goals": verified}
        (output / "implementation.json").write_text(json.dumps(implementation, indent=2) + "\n")
    started = time.monotonic()
    summary, code = analyze(config)
    persisted = json.loads((Path(summary["run_dir"]) / "summary.json").read_text())["payload"]
    report = {
        "elapsed_seconds": time.monotonic() - started,
        "semantic_exit_code": code,
        "summary": str(Path(summary["run_dir"]) / "summary.json"),
        "results": persisted["results"],
        "diagnostics": persisted.get("diagnostics", []),
        "original_sources_unchanged": all(
            sha256(source / relative) == digest for relative, digest in inputs.items()
        ),
        "checker_core_unchanged": all(
            sha256(checker / relative) == digest
            for relative, digest in record["checker_core_files"].items()
        ),
        "implementation": implementation,
    }
    if not report["original_sources_unchanged"] or (
        implementation is not None and implementation["status"] != "verified"
    ):
        code = 2
    report["runner_exit_code"] = code
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        "elapsed_seconds": report["elapsed_seconds"],
        "semantic_exit_code": report["semantic_exit_code"],
        "runner_exit_code": code,
        "original_sources_unchanged": report["original_sources_unchanged"],
        "checker_core_unchanged": report["checker_core_unchanged"],
        "implementation": (
            {
                "status": implementation["status"],
                "verified_goals": implementation["verified_goals"],
                "duration_ms": implementation["duration_ms"],
            }
            if implementation is not None else None
        ),
        "summary": report["summary"],
        "results": [
            {
                "target": result["target"]["name"],
                "status": result["status"],
                "verdict": result["verdict"],
                "errors": [
                    {"code": item["code"], "message": item["message"]}
                    for item in result.get("diagnostics", [])
                    if item.get("severity", "error") == "error"
                ],
            }
            for result in persisted["results"]
        ],
    }, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
