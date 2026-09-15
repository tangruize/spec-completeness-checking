from __future__ import annotations

import re
from pathlib import Path
from time import monotonic
from typing import Mapping

from specdet.assistance.prompts import build_prompt
from specdet.assistance.records import atomic_json
from specdet.domain.proposals import GenerationRequest, RawResponse

from ._transport import (
    executable_path, isolated_workspace, response_metadata, run_process,
    transport_environment, validate_environment, validate_timeout,
)
from .errors import ProviderError

REQUIRED_FLAGS = (
    "--available-tools", "--disable-builtin-mcps", "--no-custom-instructions",
    "--no-ask-user", "--no-auto-update", "--no-bash-env", "--no-remote",
    "--no-remote-export", "--disallow-temp-dir", "--log-level", "--output-format",
    "--stream", "--silent", "--prompt", "--no-color",
)


class CopilotProvider:
    """Optional, tools-disabled CLI transport; authentication uses token environment.

    Configuration, hooks, instruction sources, MCP configuration and cwd are
    isolated per request. Stored interactive login configuration is not copied.
    An installed CLI without the required isolation flags is refused.
    """

    mode = "live"

    def __init__(
        self, *, executable: str = "copilot", model: str = "",
        timeout_seconds: float = 300, work_root: Path | str | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        validate_timeout(timeout_seconds)
        if not isinstance(model, str) or "\0" in model:
            raise ValueError("Provider model must be a NUL-free string")
        self.executable = executable_path(str(executable))
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.work_root = (
            Path.cwd() / "provider-work" if work_root is None else Path(work_root)
        ).resolve()
        self.environment = dict(environment or {})
        validate_environment(self.environment, copilot=True)
        self._capabilities_checked = False

    def generate(self, request: GenerationRequest) -> RawResponse:
        prompt = build_prompt(request)
        started = monotonic()
        with isolated_workspace(self.work_root) as workspace:
            environment = transport_environment(workspace, self.environment, copilot=True)
            atomic_json(Path(environment["COPILOT_HOME"]) / "config.json", {
                "disableAllHooks": True,
                "ide": {"autoConnect": False},
            })
            if not self._capabilities_checked:
                help_result = run_process(
                    [self.executable, "--help"], workspace=workspace,
                    environment=environment, timeout=min(10, self.timeout_seconds),
                )
                missing = [
                    flag for flag in REQUIRED_FLAGS
                    if re.search(
                        r"(?<![\w-])" + re.escape(flag) + r"(?=$|[\s\[=,<])",
                        help_result.stdout,
                    ) is None
                ]
                if missing:
                    raise ProviderError(
                        "tools_not_disabled",
                        "Copilot CLI cannot guarantee text-only isolation; missing flags: "
                        + ", ".join(missing)
                        + ". Configure an explicitly text-only subprocess provider instead.",
                    )
                self._capabilities_checked = True
            remaining = self.timeout_seconds - (monotonic() - started)
            if remaining <= 0:
                raise ProviderError(
                    "provider_timeout", "Provider setup exhausted the request timeout",
                    retryable=True,
                )
            argv = [
                self.executable,
                "--available-tools", "",
                "--disable-builtin-mcps",
                "--no-custom-instructions",
                "--no-ask-user",
                "--no-auto-update",
                "--no-bash-env",
                "--no-remote",
                "--no-remote-export",
                "--disallow-temp-dir",
                "--log-level", "none",
                "--no-color",
                "--output-format", "text",
                "--stream", "off",
                "--silent",
            ]
            if self.model:
                argv += ["--model", self.model]
            argv += ["--prompt", prompt]
            result = run_process(
                argv, workspace=workspace, environment=environment, timeout=remaining,
            )
            metadata = response_metadata(result, (monotonic() - started) * 1000)
            metadata.update({
                "tools_disabled": True,
                "custom_instructions_disabled": True,
                "configuration_isolated": True,
            })
            return RawResponse(request.id, result.stdout, "copilot", self.model, metadata)
