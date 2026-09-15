"""Optional outer orchestration; mechanical analysis never needs this package."""

from .budget import AssistanceBudget
from .controller import AssistanceTelemetry, run_assisted, run_unassisted
from .prompts import ProposalParseError, build_prompt, parse_proposal
from .settings import AssistanceSettings

RequestBudget = AssistanceBudget

__all__ = [
    "AssistanceBudget",
    "AssistanceSettings",
    "AssistanceTelemetry",
    "ProposalParseError",
    "RequestBudget",
    "build_prompt",
    "parse_proposal",
    "run_assisted",
    "run_unassisted",
]
