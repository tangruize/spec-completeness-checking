"""Generate ignored Nanvix inputs from a read-only checkout, then record verifier evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from specdet.api import analyze
from specdet.config import ToolchainConfig, load_config

from seal import HERE, check, prepare


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verus", type=Path, required=True)
    parser.add_argument("--rust-toolchain", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True, help="Read-only, hash-pinned Nanvix checkout")
    parser.add_argument("--target", action="append")
    args = parser.parse_args()
    checker = HERE.parents[2]
    source = args.source_root.resolve()
    output = args.out.resolve()
    if output.is_relative_to(source):
        parser.error("--out must not write evidence into the Nanvix source repository")
    if output.is_relative_to(checker):
        ignored = subprocess.run(
            ["git", "-C", str(checker), "check-ignore", "--quiet", "--", str(output / "snapshot.json")],
            check=False,
        )
        if ignored.returncode != 0:
            parser.error("--out inside the checker checkout must be gitignored; use this case's results/ directory")
    preparation_started = time.monotonic()
    manifest = prepare(source)
    preparation_elapsed = time.monotonic() - preparation_started
    config = load_config(HERE / "specdet.toml")
    config = replace(
        config,
        output_dir=output,
        toolchain=ToolchainConfig(
            executable=str(args.verus.resolve()), rust_toolchain=args.rust_toolchain,
        ),
        selectors=tuple(args.target) if args.target else config.selectors,
    )
    def checker_hashes() -> dict[str, str]:
        return {
            path.relative_to(checker).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((checker / "src/specdet").rglob("*.py"))
        }

    checker_before = checker_hashes()
    started = time.monotonic()
    summary, code = analyze(config)
    elapsed = time.monotonic() - started
    check(args.source_root)
    checker_after = checker_hashes()
    record = {
        "classification": manifest["classification"],
        "source_revision": manifest["source_revision"],
        "source_hashes": manifest["source_hashes"],
        "fixture_hashes": manifest["fixture_hashes"],
        "sealed_sources_unchanged": True,
        "original_sources_rechecked": args.source_root is not None,
        "source_material_policy": "Generated inputs and their source-bearing provenance remain local and gitignored",
        "preparation_elapsed_seconds": preparation_elapsed,
        "checker_revision": subprocess.check_output(
            ["git", "-C", str(checker), "rev-parse", "HEAD"], text=True,
        ).strip(),
        "checker_source_hashes": checker_before,
        "checker_source_hashes_after": checker_after,
        "checker_sources_unchanged": checker_before == checker_after,
        "verifier": str(args.verus.resolve()),
        "verifier_sha256": hashlib.sha256(args.verus.read_bytes()).hexdigest(),
        "rust_toolchain": args.rust_toolchain,
        "elapsed_seconds": elapsed,
        "exit_code": code,
        "run_dir": summary["run_dir"],
        "results": [{
            "target": result["target"]["name"],
            "status": result["status"],
            "verdict": result["verdict"],
            "problem_id": result.get("problem_id"),
            "baseline_status": (result.get("baseline") or {}).get("status"),
            "coverage": result.get("coverage", {}),
            "proofs": [{
                key: proof.get(key)
                for key in ("status", "verified_goals", "problem_id", "duration_ms", "artifact")
            } for proof in result.get("proofs", [])],
            "counterexample": result.get("counterexample"),
            "diagnostics": result.get("diagnostics", []),
        } for result in summary.get("results", [])],
    }
    (Path(summary["run_dir"]) / "measured-result.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
