#!/usr/bin/env python3
"""Replay standalone proofs and bounded checks against the pinned Rust crate."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

from run import ROOT, run_bounded, sha256, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verus", required=True, type=Path)
    parser.add_argument("--native-only", action="store_true")
    args = parser.parse_args()
    seal = json.loads((ROOT / "initial-seal.json").read_text())
    for relative, expected in seal["sealed_files"].items():
        if sha256(ROOT / relative) != expected:
            parser.error(f"sealed original changed: {relative}")
    original = (ROOT / "spec/initial.rs").read_text()
    compatible = (ROOT / "spec/compatible.rs").read_text()
    if compatible != original.replace("seq![key]", "Seq::<u64>::empty().push(key)"):
        parser.error("compatible.rs is not the recorded encoding-only revision")

    provenance = json.loads((ROOT / "proofs/provenance.json").read_text())
    inputs = {
        "spec/compatible.rs": "compatible_spec_sha256",
        "controls/pop_return_omitted.rs": "control_spec_sha256",
        "proofs/model_determinism.rs": "determinism_proof_sha256",
        "proofs/model_satisfiability.rs": "satisfiability_proof_sha256",
        "proofs/known_omission_witness.rs": "known_omission_witness_sha256",
    }
    for relative, key in inputs.items():
        if sha256(ROOT / relative) != provenance[key]:
            parser.error(f"proof provenance mismatch: {relative}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = ROOT / "runs" / f"validation-{timestamp}"
    work = run_dir / "work"
    work.mkdir(parents=True)
    env = dict(os.environ, TMPDIR=str(work), PYTHONDONTWRITEBYTECODE="1")
    results = {}
    expected_goals = {
        "encoding_equivalence": 1,
        "model_determinism": 3,
        "model_satisfiability": 9,
        "known_omission_witness": 1,
    }
    for name, expected in expected_goals.items():
        directory = run_dir / name
        process = run_bounded(
            [
                str(args.verus.resolve()), f"proofs/{name}.rs",
                "--out-dir", str(directory),
            ],
            directory, 60, env,
        )
        text = (directory / "command.log").read_text()
        match = re.search(r"verification results:: (\d+) verified, (\d+) errors", text)
        counts = None if match is None else {
            "verified": int(match[1]), "errors": int(match[2]),
        }
        results[name] = {
            "process": process,
            "verification": counts,
            "source_sha256": sha256(ROOT / f"proofs/{name}.rs"),
            "passed": process["returncode"] == 0 and not process["timed_out"]
            and counts == {"verified": expected, "errors": 0},
        }

    if not args.native_only:
        source = json.loads((ROOT / "source.json").read_text())
        checkout = ROOT / "source-checkout"
        if not (checkout / ".git").is_dir():
            parser.error("clone the pinned upstream into source-checkout first; see README.md")
        commit = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True,
        ).strip()
        if commit != source["commit"]:
            parser.error(f"unexpected source commit: {commit}")
        for relative, expected in source["files_sha256"].items():
            if sha256(checkout / relative) != expected:
                parser.error(f"upstream source digest mismatch: {relative}")
        if subprocess.check_output(
            ["git", "-C", str(checkout), "status", "--porcelain"], text=True,
        ).strip():
            parser.error("upstream checkout is dirty")
        directory = run_dir / "source-oracle"
        process = run_bounded(
            [
                "cargo", "run", "--quiet", "--offline", "--locked",
                "--manifest-path", "oracle/Cargo.toml",
                "--target-dir", str(run_dir / "cargo-target"),
            ],
            directory, 120, env,
        )
        try:
            counts = json.loads((directory / "command.log").read_text())
        except json.JSONDecodeError:
            counts = None
        results["source_oracle"] = {
            "process": process,
            "counts": counts,
            "source_commit": commit,
            "oracle_source_sha256": sha256(ROOT / "oracle/src/main.rs"),
            "passed": process["returncode"] == 0 and not process["timed_out"]
            and isinstance(counts, dict)
            and counts.get("states") == 903 and counts.get("transitions") == 18_060,
        }

    summary = {
        "schema_version": 1,
        "started_utc": timestamp,
        "passed": all(item["passed"] for item in results.values()),
        "native_only": args.native_only,
        "verus": str(args.verus.resolve()),
        "results": results,
        "scope": "abstract-contract proofs and finite source/model comparisons, not Rust refinement verification",
    }
    write_json(run_dir / "summary.json", summary)
    print(json.dumps({
        "summary": str((run_dir / "summary.json").relative_to(ROOT)),
        "passed": summary["passed"],
        "checks": {
            name: {
                "passed": item["passed"],
                "seconds": item["process"]["wall_seconds"],
                "verification": item.get("verification"),
                "counts": item.get("counts"),
            }
            for name, item in results.items()
        },
    }, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
