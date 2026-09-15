"""Explicit provider construction; importing this module starts no live transport."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .errors import ProviderError

if TYPE_CHECKING:
    from specdet.assistance.settings import AssistanceSettings
    from specdet.ports.assistance import Provider


def create_provider(
    settings: AssistanceSettings, *, run_dir: Path | None = None,
) -> Provider | None:
    if settings.mode == "off":
        return None
    if settings.mode == "replay":
        if not settings.responses:
            raise ProviderError("replay_config", "Replay mode requires a responses directory")
        from .replay import ReplayProvider

        return ReplayProvider(Path(settings.responses))
    workspace = (Path.cwd() if run_dir is None else Path(run_dir)) / "provider-work"
    if settings.provider == "copilot":
        from .copilot import CopilotProvider

        return CopilotProvider(
            executable=settings.executable, model=settings.model,
            timeout_seconds=settings.request_timeout_seconds, work_root=workspace,
        )
    if settings.provider == "subprocess":
        from .subprocess import TextSubprocessProvider

        return TextSubprocessProvider(
            settings.command, text_only=settings.text_only, model=settings.model,
            timeout_seconds=settings.request_timeout_seconds, work_root=workspace,
        )
    raise ProviderError("unsupported_provider", f"Unknown live provider: {settings.provider}")


def __getattr__(name: str) -> object:
    if name == "ReplayProvider":
        from .replay import ReplayProvider

        return ReplayProvider
    if name == "CopilotProvider":
        from .copilot import CopilotProvider

        return CopilotProvider
    if name == "TextSubprocessProvider":
        from .subprocess import TextSubprocessProvider

        return TextSubprocessProvider
    raise AttributeError(name)


__all__ = [
    "ProviderError", "ReplayProvider", "CopilotProvider", "TextSubprocessProvider",
    "create_provider",
]
