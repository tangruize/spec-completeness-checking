from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from specdet.domain.models import JsonObject, Stage


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a TOML table")
    return value


def _strings(data: dict, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = data.get(key, list(default))
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError(f"{key} must be an array of strings")
    return tuple(value)


def _positive(data: dict, key: str, default: int) -> int:
    value = data.get(key, default)
    if type(value) is not int or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _text(data: dict, key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _path(base: Path, text: str) -> Path:
    path = Path(text).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


@dataclass(frozen=True)
class Limits:
    verifier_timeout_seconds: int = 120
    solver_timeout_ms: int = 10000
    max_search_rounds: int = 500
    max_container_depth: int = 1
    seed: int = 0
    run_timeout_seconds: int = 60


@dataclass(frozen=True)
class BuildConfig:
    adapter: str = "verus.single_file"
    entrypoint: str = ""
    injection_file: str = ""
    package: str = ""
    features: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()
    verify_module: str = ""


@dataclass(frozen=True)
class ToolchainConfig:
    executable: str = ""
    root: str = ""
    rust_toolchain: str = ""
    environment: dict[str, str] = field(default_factory=dict)
    extra_args: tuple[str, ...] = ()

    def resolve_executable(self) -> Path:
        configured = self.executable or (str(Path(self.root) / "verus") if self.root else "")
        found = configured or shutil.which("verus")
        if not found:
            raise FileNotFoundError(
                "Verus not found; set adapters.verus.executable/toolchain_root or --verus"
            )
        resolved = Path(found).expanduser().resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise FileNotFoundError(f"Verus is not executable: {resolved}")
        return resolved


@dataclass(frozen=True)
class Config:
    project_root: Path
    output_dir: Path
    language: str = "verus"
    include: tuple[str, ...] = ("**/*.rs",)
    exclude: tuple[str, ...] = ("target/**", ".git/**", ".venv/**", "backups/**")
    visibility: str = "all"
    selectors: tuple[str, ...] = ()
    type_sources: tuple[str, ...] = ()
    build: BuildConfig = field(default_factory=BuildConfig)
    toolchain: ToolchainConfig = field(default_factory=ToolchainConfig)
    limits: Limits = field(default_factory=Limits)
    observation_policy: str = "verus-observable-v1"
    analysis_kind: str = "concrete_determinism"
    abstract_inputs: tuple[str, ...] = ()
    abstract_require_concrete: bool = True
    counterexample_candidates: int = 16
    proof_strategies: tuple[str, ...] = ("baseline", "rules", "accepted", "assisted")
    max_proof_attempts: int = 3
    assistance: JsonObject = field(default_factory=dict)
    offline: bool = False
    source_config: str = ""

    def validate(self) -> None:
        if not self.project_root.is_dir():
            raise ValueError(f"Project root is not a directory: {self.project_root}")
        if self.output_dir == self.project_root or self.project_root.is_relative_to(self.output_dir):
            raise ValueError("Output directory must not equal or contain the project root")
        if self.visibility not in {"all", "public"}:
            raise ValueError("targets.visibility must be 'all' or 'public'")
        if self.analysis_kind not in {"concrete_determinism", "abstract_determinism"}:
            raise ValueError(f"Unsupported analysis kind: {self.analysis_kind}")
        if type(self.abstract_require_concrete) is not bool:
            raise ValueError("analysis.abstract.require_concrete must be a boolean")
        if not isinstance(self.abstract_inputs, (tuple, list)) or any(
            not isinstance(name, str) or not name for name in self.abstract_inputs
        ):
            raise ValueError("analysis.abstract.inputs must contain nonempty parameter names")
        if len(set(self.abstract_inputs)) != len(self.abstract_inputs):
            raise ValueError("analysis.abstract.inputs must not contain duplicates")
        if not self.observation_policy:
            raise ValueError("An observation policy must be selected")
        for name in ("verifier_timeout_seconds", "solver_timeout_ms", "max_search_rounds", "run_timeout_seconds"):
            value = getattr(self.limits, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.max_proof_attempts) is not int or self.max_proof_attempts <= 0:
            raise ValueError("max_proof_attempts must be a positive integer")
        if type(self.counterexample_candidates) is not int or self.counterexample_candidates < 0:
            raise ValueError("analysis.counterexamples.max_candidates must be a nonnegative integer")
        allowed_strategies = {"baseline", "rules", "accepted", "assisted"}
        if not self.proof_strategies or set(self.proof_strategies) - allowed_strategies:
            raise ValueError("Unknown or empty proof generation strategy list")
        if self.proof_strategies[0] != "baseline":
            raise ValueError("The first proof generation strategy must be baseline")
        if len(set(self.proof_strategies)) != len(self.proof_strategies):
            raise ValueError("Proof generation strategies must not repeat")
        if self.offline and self.assistance.get("mode", "off") == "live":
            raise ValueError("--offline conflicts with live assistance")
        mode = self.assistance.get("mode", "off")
        if mode not in {"off", "live", "replay"}:
            raise ValueError(f"Unknown assistance mode: {mode}")
        excluded = self.assistance.get("excluded_stages", [])
        if not isinstance(excluded, list) or any(not isinstance(item, str) for item in excluded):
            raise ValueError("assistance.excluded_stages must be an array of stage names")
        for item in excluded:
            Stage(item)
        bypass = {
            "--no-verify", "--no-lifetime", "--verify-function", "--verify-root",
            "--verify-only-module", "--verify-module", "--log-dir", "--log-all",
        }
        if any(arg.split("=", 1)[0] in bypass for arg in (*self.toolchain.extra_args, *self.build.extra_args)):
            raise ValueError("Verifier scoping, verification and artifact flags are managed by specdet")
        for relative in (self.build.entrypoint, self.build.injection_file, *self.type_sources):
            if relative and not (self.project_root / relative).resolve().is_relative_to(self.project_root):
                raise ValueError(f"Project input escapes project root: {relative}")

    def to_dict(self) -> JsonObject:
        from specdet.domain.models import as_object

        return {
            "schema_version": 1,
            "project_root": str(self.project_root),
            "output_dir": str(self.output_dir),
            "language": self.language,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "visibility": self.visibility,
            "selectors": list(self.selectors),
            "type_sources": list(self.type_sources),
            "build": as_object(self.build),
            "toolchain": as_object(self.toolchain),
            "limits": as_object(self.limits),
            "observation_policy": self.observation_policy,
            "analysis_kind": self.analysis_kind,
            "abstract_inputs": list(self.abstract_inputs),
            "abstract_require_concrete": self.abstract_require_concrete,
            "counterexample_candidates": self.counterexample_candidates,
            "proof_strategies": list(self.proof_strategies),
            "max_proof_attempts": self.max_proof_attempts,
            "assistance": self.assistance,
            "offline": self.offline,
        }


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve()
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("Configuration requires schema_version = 1")
    expected = {"schema_version", "project", "language", "targets", "build",
                "adapters", "analysis", "limits", "artifacts", "assistance", "offline"}
    if unknown := set(raw) - expected:
        raise ValueError(f"Unknown configuration sections: {', '.join(sorted(unknown))}")
    project = _table(raw, "project")
    targets = _table(raw, "targets")
    build = _table(raw, "build")
    toolchain = _table(_table(raw, "adapters"), "verus")
    analysis = _table(raw, "analysis")
    proofs = _table(analysis, "proof_generation")
    abstract = _table(analysis, "abstract")
    counterexamples = _table(analysis, "counterexamples")
    limits = _table(raw, "limits")
    environment = toolchain.get("environment", {})
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ValueError("adapters.verus.environment must contain string values")
    assistance = _table(raw, "assistance").copy()
    if "responses" in assistance:
        assistance["responses"] = str(_path(path.parent, _text(assistance, "responses")))
    executable = _text(toolchain, "executable")
    root = _text(toolchain, "toolchain_root")
    seed = limits.get("seed", 0)
    depth = limits.get("max_container_depth", 1)
    if type(seed) is not int or seed < 0 or type(depth) is not int or depth < 0:
        raise ValueError("seed and max_container_depth must be nonnegative integers")
    offline = raw.get("offline", False)
    if type(offline) is not bool:
        raise ValueError("offline must be a boolean")
    cfg = Config(
        project_root=_path(path.parent, _text(project, "root", ".")),
        output_dir=_path(path.parent, _text(_table(raw, "artifacts"), "output_dir", "specdet-results")),
        language=_text(_table(raw, "language"), "name", "verus"),
        include=_strings(targets, "include", ("**/*.rs",)),
        exclude=_strings(targets, "exclude", ("target/**", ".git/**", ".venv/**", "backups/**")),
        visibility=_text(targets, "visibility", "all"),
        selectors=_strings(targets, "select"),
        type_sources=_strings(project, "type_sources"),
        build=BuildConfig(
            adapter=_text(build, "adapter", "verus.single_file"),
            entrypoint=_text(build, "entrypoint"),
            injection_file=_text(build, "injection_file"),
            package=_text(build, "package"),
            features=_strings(build, "features"),
            extra_args=_strings(build, "extra_args"),
            verify_module=_text(build, "verify_module"),
        ),
        toolchain=ToolchainConfig(
            executable=str(_path(path.parent, executable)) if executable else "",
            root=str(_path(path.parent, root)) if root else "",
            rust_toolchain=_text(toolchain, "rust_toolchain"),
            environment=environment,
            extra_args=_strings(toolchain, "extra_args"),
        ),
        limits=Limits(
            verifier_timeout_seconds=_positive(limits, "verifier_timeout_seconds", 120),
            solver_timeout_ms=_positive(limits, "solver_timeout_ms", 10000),
            max_search_rounds=_positive(limits, "max_search_rounds", 500),
            max_container_depth=depth,
            seed=seed,
            run_timeout_seconds=_positive(limits, "run_timeout_seconds", 60),
        ),
        analysis_kind=_text(analysis, "kind", "concrete_determinism"),
        abstract_inputs=_strings(abstract, "inputs"),
        abstract_require_concrete=abstract.get("require_concrete", True),
        counterexample_candidates=counterexamples.get("max_candidates", 16),
        observation_policy=_text(analysis, "observation_policy", "verus-observable-v1"),
        proof_strategies=_strings(proofs, "strategies", ("baseline", "rules", "accepted", "assisted")),
        max_proof_attempts=_positive(proofs, "max_attempts", 3),
        assistance=assistance,
        offline=offline,
        source_config=str(path),
    )
    cfg.validate()
    return cfg
