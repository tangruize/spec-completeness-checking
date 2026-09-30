from __future__ import annotations

import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Callable

from specdet.analysis.pipeline import AnalysisSession, ProjectSession, run_mechanical
from specdet.config import Config
from specdet.domain.models import JsonObject, as_object
from specdet.ports.assistance import AssistanceSession
from specdet.ports.language import LanguageBackend
from specdet.storage.artifacts import ArtifactStore

SessionDriver = Callable[[AssistanceSession], None]


def create_backend(config: Config) -> LanguageBackend:
    if config.language == "verus":
        from specdet.adapters.verus.backend import VerusBackend

        return VerusBackend(config)
    raise ValueError(f"No installed backend for language {config.language!r}")


def result_exit_code(results: list[JsonObject]) -> int:
    if not results or any(row.get("status") in {"failed", "unsupported"} for row in results):
        return 2
    if any(row.get("verdict") == "nondeterministic" for row in results):
        return 1
    if any(
        row.get("verdict") != "deterministic" and row.get("status") != "not_applicable"
        for row in results
    ):
        return 3
    return 0


def analyze(
    config: Config, *, backend: LanguageBackend | None = None,
    driver: SessionDriver | None = None, discover_only: bool = False, parent_run: str = "",
) -> tuple[JsonObject, int]:
    """Analyze a project, optionally driven by an explicit assistance controller."""
    started = time.monotonic()
    config.validate()
    selected_backend = backend if backend is not None else create_backend(config)
    drive = driver if driver is not None else run_mechanical
    if driver is None and config.assistance.get("mode", "off") != "off":
        raise ValueError("Pass an explicit assistance driver when assistance is enabled")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]
    run_dir = config.output_dir / run_id
    store = ArtifactStore(run_dir)
    store.artifact("effective-config.json", "effective_config", config.to_dict())
    store.artifact("manifest.json", "run_manifest", {
        "run_id": run_id, "status": "running", "parent_run": parent_run,
    })
    completed = False
    results: list[JsonObject] = []
    summary: JsonObject = {"schema_version": 1, "run_id": run_id, "run_dir": str(run_dir)}
    try:
        project = ProjectSession(selected_backend, config, run_dir, require_toolchain=not discover_only)
        drive(project)
        summary["diagnostics"] = [as_object(item) for item in project.diagnostics]
        summary["discovery_coverage"] = (
            "partial" if any(item.code == "partial_discovery" for item in project.diagnostics)
            else "source-visible"
        )
        summary["targets"] = [as_object(item) for item in project.targets]
        if project.failed or project.project is None:
            summary.update({"results": [], "status": "failed", "counts": {}})
            code = 2
        elif discover_only:
            summary.update({"results": [], "status": "completed", "counts": {"targets": len(project.targets)}})
            code = 0
        else:
            for target in project.targets:
                session = AnalysisSession(
                    selected_backend, config, project.project, target,
                    run_dir / "targets" / target.id, run_id,
                )
                drive(session)
                results.append(session.report.to_dict())
                store.artifact("partial-summary.json", "run_summary", {
                    **summary, "results": results, "status": "running",
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                })
            summary.update({
                "results": results, "status": "completed",
                "counts": dict(Counter(str(row["verdict"]) for row in results)),
            })
            code = result_exit_code(results)
        summary["exit_code"] = code
        summary["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        store.artifact("summary.json", "run_summary", summary)
        store.artifact("manifest.json", "run_manifest", {
            "run_id": run_id, "status": summary["status"], "parent_run": parent_run,
            "exit_code": code,
            "duration_ms": summary["duration_ms"],
        })
        completed = True
        return summary, code
    finally:
        if not completed:
            store.artifact("manifest.json", "run_manifest", {
                "run_id": run_id,
                "status": "interrupted" if isinstance(sys.exception(), KeyboardInterrupt) else "failed",
                "parent_run": parent_run, "completed_targets": len(results),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            })
