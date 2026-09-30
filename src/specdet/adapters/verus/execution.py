from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from specdet.config import Config
from specdet.domain.models import Diagnostic, Stage, StageError
from specdet.runtime import RunDeadlineExceeded, bounded_timeout, check_deadline, kill_owned_process, owned_process
from specdet.storage.artifacts import ArtifactStore, file_digest


@dataclass(frozen=True)
class ProcessResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False


def _decode(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run_process(
    command: list[str], cwd: Path, timeout: float, environment: dict[str, str],
) -> ProcessResult:
    started = time.monotonic()
    timeout = bounded_timeout(timeout)
    timed_out = False
    with owned_process(command, cwd=cwd, environment=environment) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, RunDeadlineExceeded):
            kill_owned_process(process)
            stdout, stderr = process.communicate()
            timed_out = True
        returncode = -1 if timed_out else process.returncode
    return ProcessResult(
        tuple(command), returncode, _decode(stdout), _decode(stderr),
        (time.monotonic() - started) * 1000, timed_out=timed_out,
    )


class VerusExecutor:
    def __init__(self, config: Config):
        self.config = config
        self.executable = config.toolchain.resolve_executable()
        self.environment = os.environ.copy()
        self.environment.update(config.toolchain.environment)
        self.environment["PATH"] = str(self.executable.parent) + os.pathsep + self.environment.get("PATH", "")
        if config.offline:
            self.environment["CARGO_NET_OFFLINE"] = "true"
        if config.toolchain.rust_toolchain:
            if not shutil.which("rustup"):
                raise FileNotFoundError("rustup is required for the configured rust_toolchain")
            command = ["rustup", "run", config.toolchain.rust_toolchain, "rustc", "--print", "sysroot"]
            proc = run_process(command, config.project_root, 30, self.environment)
            check_deadline()
            if proc.returncode != 0:
                raise RuntimeError(f"Cannot resolve Rust sysroot: {proc.stderr}")
            library = str(Path(proc.stdout.strip()) / "lib")
            self.environment["LD_LIBRARY_PATH"] = library + os.pathsep + self.environment.get("LD_LIBRARY_PATH", "")

    def identity(self) -> dict:
        result = run_process([str(self.executable), "--version"], self.executable.parent, 30, self.environment)
        check_deadline()
        if result.returncode != 0 or result.timed_out:
            raise StageError(Diagnostic(
                Stage.CONFIG, "toolchain_error",
                f"Cannot run Verus: {(result.stderr or result.stdout).strip()}",
            ))
        import z3

        return {
            "executable": str(self.executable),
            "executable_digest": file_digest(self.executable),
            "version": result.stdout.strip(),
            "z3_python": z3.get_version_string(),
            "rust_toolchain": self.config.toolchain.rust_toolchain,
        }

    def verify(
        self, source: Path, project_root: Path, artifact_dir: Path,
        function: str, module: str = "", *, all_functions: bool = False,
    ) -> ProcessResult:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        log_dir = artifact_dir / "logs"
        log_dir.mkdir()
        flags = ["--log-all", "--log-dir", str(log_dir), *self.config.toolchain.extra_args]
        if not all_functions:
            if module:
                flags.extend(["--verify-only-module", module])
            else:
                flags.append("--verify-root")
            flags.extend(["--verify-function", function])
        build = self.config.build
        if build.adapter == "verus.cargo":
            command = ["cargo", "verus", "verify", "--fwd-verus-args-to", "roots"]
            if build.package:
                command.extend(["-p", build.package])
            if build.features:
                command.extend(["--features", ",".join(build.features)])
            command.extend(build.extra_args)
            command.extend(["--", *flags])
        elif build.adapter in {"verus.single_file", "verus.native"}:
            command = [str(self.executable), str(source), *build.extra_args, *flags]
        else:
            raise StageError(Diagnostic(
                Stage.PREPARE, "profile_gap", f"Unknown build adapter: {build.adapter}",
            ), recoverable=True)
        before = file_digest(source)
        result = run_process(
            command, project_root, self.config.limits.verifier_timeout_seconds, self.environment
        )
        store = ArtifactStore(artifact_dir)
        store.write_text("stdout.txt", result.stdout)
        store.write_text("stderr.txt", result.stderr)
        store.artifact("process.json", "verifier_process", result)
        if file_digest(source) != before:
            raise StageError(Diagnostic(
                Stage.PROOF_CHECKING, "verifier_modified_input",
                "Verifier command modified its input; refuse to attribute results to the frozen obligation",
            ))
        return result


def classify_process(result: ProcessResult) -> tuple[str, int]:
    if result.timed_out:
        return "timeout", 0
    output = result.stdout + "\n" + result.stderr
    counts = re.findall(r"(\d+)\s+verified,\s+(\d+)\s+errors?", output)
    # Cargo may print dependency summaries before the selected root crate.
    # Dependency proofs cannot make a zero-goal target a successful check.
    verified = int(counts[-1][0]) if counts else 0
    if result.returncode == 0 and counts and verified > 0 and all(int(err) == 0 for _, err in counts):
        return "verified", verified
    if "postcondition not satisfied" in output or "assertion failed" in output.lower():
        return "unproved", verified
    if result.returncode != 0:
        return "compile_error", verified
    return "error", verified
