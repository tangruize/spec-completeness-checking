#!/usr/bin/env python3
"""Bounded, offline specdet runner; reports source-model evidence only."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
CHECKER = ROOT.parents[2]
LOG_LIMIT = 1024 * 1024


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def checker_identity() -> dict:
    files = {
        str(path.relative_to(CHECKER)): sha256(path)
        for path in sorted((CHECKER / "src").rglob("*.py"))
    }
    return {
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=CHECKER, text=True
        ).strip(),
        "source_sha256": files,
        "digest": hashlib.sha256(
            json.dumps(files, sort_keys=True).encode()
        ).hexdigest(),
    }


def run_bounded(command: list[str], directory: Path, timeout: int, env: dict) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    written = 0
    discarded = 0
    timed_out = False
    killed = False
    process = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    with (directory / "command.log").open("wb") as log:
        while selector.get_map():
            elapsed = time.monotonic() - started
            if elapsed >= timeout and not timed_out:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if elapsed >= timeout + 5 and not killed:
                killed = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for key, _ in selector.select(timeout=0.2):
                block = os.read(key.fd, 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                accepted = min(len(block), LOG_LIMIT - written)
                log.write(block[:accepted])
                written += accepted
                discarded += len(block) - accepted
        process.stdout.close()
        selector.close()
        returncode = process.wait()
    return {
        "command": command,
        "returncode": returncode,
        "timeout_seconds": timeout,
        "timed_out": timed_out,
        "wall_seconds": round(time.monotonic() - started, 6),
        "log": str((directory / "command.log").relative_to(ROOT)),
        "log_bytes_retained": written,
        "log_bytes_discarded": discarded,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verus", required=True, type=Path)
    parser.add_argument("--profile", type=Path, default=Path("specdet.toml"))
    parser.add_argument("--label", default="initial")
    parser.add_argument("--wall-seconds", type=int, default=240)
    parser.add_argument(
        "--out", type=Path, default=Path("runs"),
        help="Raw-artifact base directory within this example's ignored runs/",
    )
    args = parser.parse_args()
    if not args.label or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in args.label):
        parser.error("--label must use lowercase letters, digits, hyphens, or underscores")
    if args.wall_seconds <= 0:
        parser.error("--wall-seconds must be positive")
    output_root = (ROOT / args.out).resolve()
    if not output_root.is_relative_to((ROOT / "runs").resolve()):
        parser.error("--out must be inside this example's ignored runs/ directory")
    profile = (ROOT / args.profile).resolve()
    if not profile.is_relative_to(ROOT):
        parser.error("--profile must remain in this example")
    seal = json.loads((ROOT / "initial-seal.json").read_text())
    for relative, expected in seal["sealed_files"].items():
        if sha256(ROOT / relative) != expected:
            parser.error(f"sealed original changed: {relative}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = output_root / f"{args.label}-{timestamp}"
    work = run_dir / "work"
    work.mkdir(parents=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(CHECKER / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TMPDIR"] = str(work)
    command = [
        sys.executable, "-c",
        "from specdet.cli.main import main; raise SystemExit(main())",
        "analyze", "--config", str(profile), "--verus", str(args.verus.resolve()),
        "--out", str(run_dir / "checker"), "--json",
    ]
    before = checker_identity()
    process = run_bounded(command, run_dir, args.wall_seconds, env)
    after = checker_identity()
    reports = sorted((run_dir / "checker").glob("*/summary.json"))
    report_complete = bool(reports)
    if not reports:
        reports = sorted((run_dir / "checker").glob("*/partial-summary.json"))
    payload = None
    if len(reports) == 1:
        envelope = json.loads(reports[0].read_text())
        payload = envelope["payload"]
        if not isinstance(payload["results"], list):
            raise ValueError("summary.json .payload.results is not a list")
    elif len(reports) > 1:
        raise ValueError("a single invocation unexpectedly produced multiple summaries")

    results = [] if payload is None else payload["results"]
    measured = {
        "schema_version": 1,
        "evidence_scope": "abstract model contract, not original Rust verification",
        "label": args.label,
        "started_utc": timestamp,
        "run_directory": str(run_dir.relative_to(ROOT)),
        "profile": str(profile.relative_to(ROOT)),
        "profile_sha256": sha256(profile),
        "initial_spec_sha256": seal["sealed_files"]["spec/initial.rs"],
        "checker_before": before,
        "checker_after": after,
        "checker_changed_during_run": before["digest"] != after["digest"],
        "process": process,
        "report": None if not reports else str(reports[0].relative_to(ROOT)),
        "report_complete": report_complete,
        "counts": None if payload is None else payload.get("counts"),
        "results": results,
        "diagnostics": [] if payload is None else payload.get("diagnostics", []),
    }
    write_json(run_dir / "measured.json", measured)
    print(json.dumps({
        "measured": str((run_dir / "measured.json").relative_to(ROOT)),
        "process": process,
        "checker_changed_during_run": measured["checker_changed_during_run"],
        "results": [
            {
                "target": item["target"]["name"],
                "status": item["status"],
                "verdict": item["verdict"],
                "baseline": item.get("baseline"),
                "proofs": [
                    {key: proof.get(key) for key in (
                        "status", "verified_goals", "duration_ms", "artifact",
                    )}
                    for proof in item.get("proofs", [])
                ],
                "counterexample": item.get("counterexample"),
                "diagnostics": [
                    {key: diagnostic.get(key) for key in ("stage", "code", "message")}
                    for diagnostic in item.get("diagnostics", [])
                ],
            }
            for item in results
        ],
    }, indent=2))
    if process["timed_out"] or payload is None or not report_complete:
        return 2
    return process["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
