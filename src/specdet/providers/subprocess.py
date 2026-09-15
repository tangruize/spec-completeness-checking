from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Mapping, Sequence

from specdet.assistance.prompts import build_prompt
from specdet.domain.proposals import GenerationRequest, RawResponse

from ._transport import (
    executable_path, isolated_workspace, response_metadata, run_process,
    transport_environment, validate_environment, validate_timeout,
)
from .errors import ProviderError


class TextSubprocessProvider:
    """Trusted text-client command: prompt on stdin, candidate JSON text on stdout.

    The caller must explicitly attest that the configured executable is text-only.
    No shell is evaluated and no tool interface is supplied. This is a transport
    contract, not an OS sandbox for arbitrary executables.
    """

    mode = "live"

    def __init__(
        self, command: Sequence[str], *, text_only: bool = False, model: str = "",
        timeout_seconds: float = 300, work_root: Path | str | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        if text_only is not True:
            raise ProviderError(
                "text_only_required", "A subprocess provider requires explicit text_only=true",
            )
        if (
            not isinstance(command, (tuple, list)) or not command
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)
        ):
            raise ValueError("Text provider command must be a nonempty argument sequence")
        if Path(command[0]).name in {"copilot", "copilot.exe"}:
            raise ProviderError("unsafe_provider_command", "Use CopilotProvider for Copilot isolation")
        forbidden = {"--allow-all", "--allow-all-tools", "--allow-all-paths", "--yolo"}
        if any(arg.split("=", 1)[0] in forbidden for arg in command[1:]):
            raise ProviderError("unsafe_provider_command", "Unrestricted agent flags are forbidden")
        validate_timeout(timeout_seconds)
        if not isinstance(model, str) or "\0" in model:
            raise ValueError("Provider model must be a NUL-free string")
        self.command = (executable_path(command[0]), *command[1:])
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.work_root = (
            Path.cwd() / "provider-work" if work_root is None else Path(work_root)
        ).resolve()
        self.environment = dict(environment or {})
        validate_environment(self.environment)

    def generate(self, request: GenerationRequest) -> RawResponse:
        prompt = build_prompt(request)
        started = monotonic()
        with isolated_workspace(self.work_root) as workspace:
            environment = transport_environment(workspace, self.environment)
            remaining = self.timeout_seconds - (monotonic() - started)
            if remaining <= 0:
                raise ProviderError(
                    "provider_timeout", "Provider setup exhausted the request timeout",
                    retryable=True,
                )
            result = run_process(
                self.command, workspace=workspace, environment=environment,
                timeout=remaining, prompt=prompt,
            )
            metadata = response_metadata(result, (monotonic() - started) * 1000)
            metadata.update({"text_only_contract": True, "configuration_isolated": True})
            return RawResponse(request.id, result.stdout, "subprocess", self.model, metadata)
