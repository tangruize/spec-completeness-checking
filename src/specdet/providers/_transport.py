from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Sequence
from uuid import uuid4

from specdet.assistance.prompts import MAX_RESPONSE_BYTES
from specdet.domain.models import JsonObject

from .errors import ProviderError

_INHERITED_ENV = frozenset({
    "PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
})
_COPILOT_AUTH_ENV = frozenset({"COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"})
_RESERVED_ENV = frozenset({
    "HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "COPILOT_HOME", "COPILOT_ALLOW_ALL", "COPILOT_CUSTOM_INSTRUCTIONS_DIRS",
    "BASH_ENV", "ENV", "NODE_OPTIONS", "LD_PRELOAD", "PYTHONPATH",
    "TMPDIR", "TMP", "TEMP", "GIT_CEILING_DIRECTORIES",
})


def validate_timeout(timeout: float) -> None:
    if (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ValueError("Provider timeout must be positive and finite")


def validate_environment(environment: Mapping[str, str], *, copilot: bool = False) -> None:
    for key, value in environment.items():
        if (
            not isinstance(key, str) or not key or "=" in key or "\0" in key
            or not isinstance(value, str) or "\0" in value
        ):
            raise ValueError("Provider environment must contain valid string pairs")
        if key in _RESERVED_ENV or (copilot and key not in _INHERITED_ENV | _COPILOT_AUTH_ENV):
            raise ProviderError(
                "unsafe_provider_environment", f"Provider environment may not override {key}",
            )


@contextmanager
def isolated_workspace(root: Path) -> Iterator[Path]:
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / f"request-{uuid4().hex}"
    workspace.mkdir(mode=0o700)
    try:
        yield workspace
    finally:
        shutil.rmtree(workspace)


def transport_environment(
    workspace: Path, extra: Mapping[str, str], *, copilot: bool = False,
) -> dict[str, str]:
    inherited = _INHERITED_ENV | (_COPILOT_AUTH_ENV if copilot else frozenset())
    environment = {key: os.environ[key] for key in inherited if key in os.environ}
    environment.update(extra)
    environment.setdefault("PATH", os.defpath)
    home = workspace / "home"
    for directory in (home, home / ".config", home / ".cache", home / ".local" / "share"):
        directory.mkdir(parents=True, exist_ok=True)
    environment.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "COPILOT_HOME": str(home / ".copilot"),
        "COPILOT_ALLOW_ALL": "false",
        "COPILOT_AUTO_UPDATE": "false",
        "NO_COLOR": "1",
        "CI": "1",
        "TMPDIR": str(workspace),
        "TMP": str(workspace),
        "TEMP": str(workspace),
        "GIT_CEILING_DIRECTORIES": str(workspace.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return environment


def executable_path(executable: str) -> str:
    if not isinstance(executable, str) or not executable or "\0" in executable:
        raise ValueError("Provider executable must be a nonempty NUL-free string")
    if os.sep in executable or (os.altsep and os.altsep in executable):
        return str(Path(executable).expanduser().resolve())
    return executable


def _output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "")[:16384]


def _kill_owned_process(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def run_process(
    argv: Sequence[str], *, workspace: Path, environment: Mapping[str, str],
    timeout: float, prompt: str | None = None,
) -> subprocess.CompletedProcess:
    """One invocation, with no retries; timeout terminates its owned process group."""
    try:
        with subprocess.Popen(
            list(argv), cwd=workspace, env=dict(environment), shell=False,
            stdin=subprocess.DEVNULL if prompt is None else subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                _kill_owned_process(process)
                try:
                    stdout, stderr = process.communicate()
                except UnicodeError:
                    stdout, stderr = error.output, error.stderr
                raise ProviderError(
                    "provider_timeout", f"Provider exceeded its {timeout:g}s transport timeout",
                    retryable=True,
                    metadata={"stdout": _output(stdout), "stderr": _output(stderr)},
                ) from error
            except UnicodeError as error:
                _kill_owned_process(process)
                process.wait()
                raise ProviderError("provider_encoding", "Provider output is not UTF-8") from error
            if process.returncode:
                raise ProviderError(
                    "provider_exit", f"Provider exited with status {process.returncode}",
                    metadata={
                        "returncode": process.returncode,
                        "stdout": _output(stdout), "stderr": _output(stderr),
                    },
                )
    except FileNotFoundError as error:
        raise ProviderError("executable_not_found", f"Provider executable not found: {argv[0]}") from error
    except PermissionError as error:
        raise ProviderError("provider_permission_denied", str(error)) from error
    except OSError as error:
        raise ProviderError("provider_transport_error", str(error)) from error
    if len(stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ProviderError("response_too_large", "Provider output exceeds the response size limit")
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def response_metadata(result: subprocess.CompletedProcess, duration_ms: float) -> JsonObject:
    return {
        "duration_ms": round(duration_ms, 3),
        "returncode": result.returncode,
        "stderr": _output(result.stderr),
        "transport_invocations": 1,
    }
