from __future__ import annotations

import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Mapping

from specdet.domain.models import JsonObject, Stage, as_object


@dataclass(frozen=True)
class AssistanceSettings:
    mode: str = "off"
    stage_policy: str = "all_applicable"
    excluded_stages: tuple[str, ...] = ()
    max_rounds_per_stage: int = 2
    max_requests_per_target: int = 3
    max_requests_per_run: int = 30
    request_timeout_seconds: float = 300
    provider: str = "copilot"
    model: str = ""
    responses: str = ""
    executable: str = "copilot"
    command: tuple[str, ...] = ()
    text_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or self.mode not in {"off", "live", "replay"}:
            raise ValueError("assistance.mode must be off, live, or replay")
        if self.stage_policy != "all_applicable":
            raise ValueError("assistance.stage_policy must be all_applicable")
        for name in ("excluded_stages", "command"):
            value = getattr(self, name)
            if not isinstance(value, (tuple, list)) or any(
                not isinstance(item, str) for item in value
            ):
                raise ValueError(f"assistance.{name} must be a sequence of strings")
            object.__setattr__(self, name, tuple(value))
        for stage in self.excluded_stages:
            Stage(stage)
        if len(set(self.excluded_stages)) != len(self.excluded_stages):
            raise ValueError("assistance.excluded_stages must not contain duplicates")
        for name in (
            "max_rounds_per_stage", "max_requests_per_target", "max_requests_per_run",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"assistance.{name} must be a positive integer")
        timeout = self.request_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("assistance.request_timeout_seconds must be positive and finite")
        if isinstance(self.responses, Path):
            object.__setattr__(self, "responses", str(self.responses))
        if isinstance(self.executable, Path):
            object.__setattr__(self, "executable", str(self.executable))
        for name in ("provider", "model", "responses", "executable"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"assistance.{name} must be a string")
        if not self.provider or not self.executable or "\0" in self.executable:
            raise ValueError("assistance.provider and executable must not be empty")
        if any(not arg or "\0" in arg for arg in self.command):
            raise ValueError("assistance.command arguments must be nonempty and NUL-free")
        if type(self.text_only) is not bool:
            raise ValueError("assistance.text_only must be a boolean")

    def allows(self, stage: Stage | str) -> bool:
        return self.mode != "off" and Stage(stage).value not in self.excluded_stages

    @property
    def budget_limits(self) -> tuple[int, int, int]:
        return (
            self.max_rounds_per_stage,
            self.max_requests_per_target,
            self.max_requests_per_run,
        )

    def to_dict(self) -> JsonObject:
        return as_object(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> AssistanceSettings:
        unknown = set(data) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown assistance settings: {', '.join(sorted(unknown))}")
        return cls(**dict(data))
