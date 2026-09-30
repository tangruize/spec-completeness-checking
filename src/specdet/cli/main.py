from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from specdet import __version__
from specdet.analysis.pipeline import AnalysisSession, run_mechanical
from specdet.api import analyze, create_backend
from specdet.config import BuildConfig, Config, Limits, ToolchainConfig, load_config
from specdet.domain.models import JsonObject, StageError, as_object, require_text
from specdet.storage.artifacts import ArtifactStore


def _output_options(parser: argparse.ArgumentParser) -> None:
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Print the complete recorded report")
    output.add_argument("--compact-json", action="store_true", help="Print a bounded evidence summary with artifact links")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="specdet", description="Specification determinism analysis")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("doctor", "discover", "analyze"):
        item = commands.add_parser(command)
        item.add_argument("source", nargs="?", help="Source file or project directory")
        item.add_argument("-c", "--config", type=Path)
        item.add_argument("--verus", help="Verifier executable (no project-specific default)")
        item.add_argument("--toolchain-root", type=Path)
        item.add_argument("--output-dir", "--out", type=Path)
        item.add_argument("--target", action="append", default=[])
        item.add_argument("--policy", choices=["verus-observable-v1", "verus-strict-v1"])
        item.add_argument("--kind", choices=["concrete_determinism", "abstract_determinism"])
        item.add_argument("--abstract-input", action="append", default=[])
        item.add_argument("--no-concrete-prerequisite", action="store_true")
        item.add_argument("--max-rounds", type=int)
        item.add_argument("--counterexample-candidates", type=int)
        item.add_argument("--solver-timeout-ms", type=int)
        item.add_argument("--timeout", type=int)
        item.add_argument("--offline", action="store_true")
        if command == "doctor":
            item.add_argument("--json", action="store_true")
        else:
            _output_options(item)
        item.add_argument("--llm-fallback", choices=["off", "live", "replay"])
        item.add_argument("--provider")
        item.add_argument("--model")
        item.add_argument("--responses", type=Path)
    report = commands.add_parser("report")
    report.add_argument("--run", type=Path, required=True)
    _output_options(report)
    replay = commands.add_parser("replay")
    replay.add_argument("--run", type=Path, required=True)
    replay.add_argument("--reexecute", action="store_true")
    replay.add_argument("--config", type=Path)
    replay.add_argument("--verus")
    replay.add_argument("--out", type=Path)
    _output_options(replay)
    assist = commands.add_parser("assist")
    assist.add_argument("--run", type=Path, required=True)
    assist.add_argument("--task", required=True)
    assist.add_argument("--mode", choices=["live", "replay"], required=True)
    assist.add_argument("--provider", default="copilot")
    assist.add_argument("--model")
    assist.add_argument("--responses", type=Path)
    assist.add_argument("--config", type=Path)
    assist.add_argument("--verus")
    assist.add_argument("--out", type=Path)
    _output_options(assist)
    adopt = commands.add_parser("adopt")
    adopt.add_argument("--proposal", type=Path, required=True)
    adopt.add_argument("--validation", type=Path, required=True)
    adopt.add_argument("--out", type=Path, required=True)
    return parser


def _config(args: argparse.Namespace) -> Config:
    if not args.config and not args.source and args.command != "doctor":
        default = Path.cwd() / "specdet.toml"
        if default.is_file():
            args.config = default
        else:
            raise ValueError("Provide a source/project path or --config; no specdet.toml found")
    if args.config:
        config = load_config(args.config)
    else:
        source = Path(args.source or ".").expanduser().resolve()
        root = source.parent if source.is_file() else source
        config = Config(
            project_root=root, output_dir=(Path.cwd() / "specdet-results").resolve(),
            include=(source.name,) if source.is_file() else ("**/*.rs",),
        )
    if args.source and args.config:
        source = Path(args.source).expanduser().resolve()
        if not source.is_relative_to(config.project_root):
            raise ValueError("Explicit source is outside configured project root")
        if source.is_file():
            config = replace(config, include=(str(source.relative_to(config.project_root)),))
    if args.verus:
        config = replace(config, toolchain=replace(
            config.toolchain, executable=str(Path(args.verus).expanduser().resolve())
        ))
    if args.toolchain_root:
        config = replace(config, toolchain=replace(
            config.toolchain, root=str(args.toolchain_root.expanduser().resolve()), executable=""
        ))
    if args.output_dir:
        config = replace(config, output_dir=args.output_dir.expanduser().resolve())
    if args.target:
        config = replace(config, selectors=tuple(args.target))
    if args.policy:
        config = replace(config, observation_policy=args.policy)
    if args.kind:
        config = replace(config, analysis_kind=args.kind)
    if args.abstract_input:
        config = replace(config, abstract_inputs=tuple(args.abstract_input))
    if args.no_concrete_prerequisite:
        config = replace(config, abstract_require_concrete=False)
    if args.counterexample_candidates is not None:
        if args.counterexample_candidates < 0:
            raise ValueError("--counterexample-candidates must be nonnegative")
        config = replace(config, counterexample_candidates=args.counterexample_candidates)
    changes = {}
    for argument, field in (
        ("max_rounds", "max_search_rounds"),
        ("solver_timeout_ms", "solver_timeout_ms"),
        ("timeout", "verifier_timeout_seconds"),
    ):
        value = getattr(args, argument)
        if value is not None:
            if value <= 0:
                raise ValueError(f"--{argument.replace('_', '-')} must be positive")
            changes[field] = value
    if changes:
        config = replace(config, limits=replace(config.limits, **changes))
    assistance = dict(config.assistance)
    for option, key in (("llm_fallback", "mode"), ("provider", "provider"), ("model", "model")):
        if getattr(args, option) is not None:
            assistance[key] = getattr(args, option)
    if args.responses:
        assistance["responses"] = str(args.responses.expanduser().resolve())
    config = replace(config, assistance=assistance, offline=config.offline or args.offline)
    config.validate()
    return config


def _driver(config: Config):
    if config.assistance.get("mode", "off") == "off":
        return run_mechanical
    from specdet.assistance.workflow import AssistanceSettings, RequestBudget, run_assisted
    from specdet.providers import ProviderError, create_provider

    settings = AssistanceSettings.from_dict(config.assistance)
    try:
        provider = create_provider(settings, run_dir=config.output_dir)
    except ProviderError as error:
        raise ValueError(str(error)) from error
    budget = RequestBudget(settings)

    def drive(session):
        telemetry = run_assisted(session, provider, settings, budget=budget)
        if isinstance(session, AnalysisSession):
            session.report.assistance.update(as_object(telemetry))
            session.store.artifact("report.json", "analysis_report", session.report)

    return drive


def _summary_output(summary: JsonObject, *, json_output: bool, compact: bool = False) -> None:
    from specdet.cli.output import print_summary

    print_summary(summary, json_output=json_output, compact=compact)


def _error_output(
    args: argparse.Namespace, message: str, diagnostic: JsonObject | None = None,
    *, started: float, exit_code: int = 2,
) -> None:
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    if getattr(args, "compact_json", False):
        _summary_output({
            "status": "interrupted" if exit_code == 130 else "failed",
            "exit_code": exit_code, "results": [], "counts": {}, "duration_ms": duration_ms,
            "diagnostics": [diagnostic or {
                "stage": "cli", "code": "interrupted" if exit_code == 130 else "cli_error",
                "severity": "warning" if exit_code == 130 else "error", "message": message,
            }],
        }, json_output=False, compact=True)
    else:
        print(message, file=sys.stderr)
        print(f"Elapsed (invocation): {duration_ms / 1000:.3f}s", file=sys.stderr)


def _run(config: Config, *, discover_only: bool = False, parent_run: str = "") -> tuple[JsonObject, int]:
    return analyze(config, driver=_driver(config), discover_only=discover_only, parent_run=parent_run)


def _restore_config(run: Path, override: Path | None = None) -> Config:
    run = run.resolve()
    store = ArtifactStore(run)
    snapshot = store.read_artifact("snapshot.json", expected_kind="source_snapshot")
    files = snapshot.get("files")
    if not isinstance(files, dict):
        raise ValueError("Snapshot file manifest is missing")
    from specdet.storage.artifacts import file_digest

    workspace = run / "workspace"
    for name, expected in files.items():
        path = (workspace / name).resolve()
        if not path.is_relative_to(workspace.resolve()) or not path.is_file() or file_digest(path) != expected:
            raise ValueError(f"Snapshot no longer matches its manifest: {name}")
    if override is not None:
        return replace(load_config(override), project_root=workspace)
    data = store.read_artifact("effective-config.json", expected_kind="effective_config")
    build = as_object(data.get("build", {}))
    toolchain = as_object(data.get("toolchain", {}))
    limits = as_object(data.get("limits", {}))
    return Config(
        project_root=workspace,
        output_dir=run.parent,
        language=require_text(data, "language", default="verus"),
        include=tuple(data.get("include", ["**/*.rs"])),
        exclude=tuple(data.get("exclude", [])),
        visibility=require_text(data, "visibility", default="all"),
        selectors=tuple(data.get("selectors", [])),
        type_sources=tuple(data.get("type_sources", [])),
        build=BuildConfig(**build),
        toolchain=ToolchainConfig(**toolchain),
        limits=Limits(**limits),
        observation_policy=require_text(data, "observation_policy", default="verus-observable-v1"),
        analysis_kind=require_text(data, "analysis_kind", default="concrete_determinism"),
        abstract_inputs=tuple(data.get("abstract_inputs", [])),
        abstract_require_concrete=bool(data.get("abstract_require_concrete", True)),
        counterexample_candidates=int(data.get("counterexample_candidates", 16)),
        proof_strategies=tuple(data.get("proof_strategies", ["baseline", "rules", "accepted", "assisted"])),
        max_proof_attempts=int(data.get("max_proof_attempts", 3)),
        assistance={"mode": "off"},
        offline=bool(data.get("offline", False)),
    )


def _adopt(args: argparse.Namespace) -> None:
    from specdet.domain.models import digest
    from specdet.domain.proposals import AdoptionRecord, Proposal
    from specdet.assistance.prompts import strict_json_object

    def read_record(path: Path, kind: str) -> JsonObject:
        record = strict_json_object(path.read_text(encoding="utf-8"))
        if "record_type" in record:
            if (
                set(record) != {"schema_version", "record_type", "data"}
                or type(record["schema_version"]) is not int
                or record["schema_version"] != 1
                or record["record_type"] != kind
            ):
                raise ValueError(f"Invalid {kind} record envelope")
            return as_object(record["data"])
        if record.get("kind") == kind and "payload_digest" in record:
            if (
                type(record.get("schema_version")) is not int
                or record["schema_version"] != 1
                or digest(record.get("payload")) != record["payload_digest"]
            ):
                raise ValueError(f"Invalid {kind} artifact envelope")
            return as_object(record["payload"])
        return record

    proposal_data = read_record(args.proposal, "proposal")
    validation_data = read_record(args.validation, "validation")
    proposal = Proposal.from_dict(proposal_data)
    if validation_data.get("proposal_id") != proposal.id:
        raise ValueError("Validation does not refer to this proposal")
    if validation_data.get("status") not in {"accepted", "accepted_for_check"}:
        raise ValueError("Proposal needs further validation or approval; adoption cannot bypass it")
    if args.out.exists():
        raise FileExistsError(f"Adoption record already exists: {args.out}")
    ArtifactStore(args.out.parent).artifact(
        args.out.name, "adoption",
        AdoptionRecord(proposal.id, digest(validation_data), "accepted_for_check", "explicit_user_adoption"),
    )
    print(args.out.resolve())


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "adopt":
            _adopt(args)
            return 0
        if args.command == "report" or (args.command == "replay" and not args.reexecute):
            summary = ArtifactStore(args.run).read_artifact("summary.json", expected_kind="run_summary")
            _summary_output(summary, json_output=args.json, compact=args.compact_json)
            return 0
        if args.command in {"replay", "assist"}:
            config = _restore_config(args.run, args.config)
            if args.verus:
                config = replace(config, toolchain=replace(
                    config.toolchain, executable=str(Path(args.verus).expanduser().resolve())
                ))
            if args.out:
                config = replace(config, output_dir=args.out.expanduser().resolve())
            if args.command == "assist":
                from specdet.domain.models import Stage

                task = Stage(args.task)
                settings: JsonObject = {
                    "mode": args.mode, "provider": args.provider,
                    "excluded_stages": [stage.value for stage in Stage if stage != task],
                }
                if args.model:
                    settings["model"] = args.model
                if args.responses:
                    settings["responses"] = str(args.responses.resolve())
                config = replace(config, assistance=settings)
            config.validate()
            summary, code = _run(config, parent_run=str(args.run.resolve()))
            _summary_output(summary, json_output=args.json, compact=args.compact_json)
            return code
        config = _config(args)
        if args.command == "doctor":
            identity = create_backend(config).toolchain_identity()
            print(json.dumps(identity, ensure_ascii=False, indent=2))
            return 0
        summary, code = _run(config, discover_only=args.command == "discover")
        if args.command == "discover" and not args.json and not args.compact_json:
            for item in summary.get("targets", []):
                if isinstance(item, dict):
                    print(f"{item['file']}:{item['name']}@{item['line']}")
        _summary_output(summary, json_output=args.json, compact=args.compact_json)
        return code
    except StageError as error:
        _error_output(args, f"{error.diagnostic.code}: {error.diagnostic.message}",
                      as_object(error.diagnostic), started=started)
        return 2
    except ModuleNotFoundError as error:
        _error_output(args, f"Missing dependency: {error.name}. Install the selected backend extra, e.g. pip install '.[verus]'.",
                      started=started)
        return 2
    except (ValueError, OSError) as error:
        _error_output(args, f"specdet: {error}", started=started)
        return 2
    except KeyboardInterrupt:
        _error_output(args, "specdet: interrupted; completed artifacts have been retained",
                      started=started, exit_code=130)
        return 130
