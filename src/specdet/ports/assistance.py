from __future__ import annotations

from pathlib import Path
from typing import Protocol

from specdet.domain.models import StageOutcome
from specdet.domain.proposals import (
    GenerationRequest, Proposal, RawResponse, ValidationRecord,
)


class Provider(Protocol):
    def generate(self, request: GenerationRequest) -> RawResponse: ...


class AssistanceSession(Protocol):
    run_dir: Path

    @property
    def finished(self) -> bool: ...

    def advance(self) -> StageOutcome: ...

    def assistance_request(self, outcome: StageOutcome) -> GenerationRequest | None: ...

    def validate_proposal(
        self, request: GenerationRequest, proposal: Proposal,
    ) -> ValidationRecord: ...

    def adopt_proposal(
        self, request: GenerationRequest, proposal: Proposal, validation: ValidationRecord,
    ) -> None: ...

    def decline_assistance(self, outcome: StageOutcome, reason: str) -> None: ...

