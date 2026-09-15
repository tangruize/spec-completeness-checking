from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic
from uuid import uuid4

from specdet.domain.models import (
    Diagnostic, JsonObject, SCHEMA_VERSION, StageOutcome, as_object, digest,
)
from specdet.domain.proposals import (
    AdoptionRecord, GenerationRequest, RawResponse, ValidationRecord,
)
from specdet.ports.assistance import AssistanceSession, Provider
from specdet.providers.errors import ProviderError

from .budget import AssistanceBudget
from .prompts import ProposalParseError, build_prompt, parse_proposal, validate_request
from .records import AttemptRecords, write_record
from .settings import AssistanceSettings

_ACCEPTED = frozenset({"accepted", "accepted_for_check"})


@dataclass
class AssistanceTelemetry:
    mode: str
    requests: int = 0
    adopted: int = 0
    declined: int = 0
    provider_errors: int = 0
    parse_errors: int = 0
    rejected: int = 0
    repeated_candidates: int = 0
    attempts: list[JsonObject] = field(default_factory=list)
    events: list[JsonObject] = field(default_factory=list)
    budget: JsonObject = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> JsonObject:
        return as_object(self)


class _AssistedRun:
    def __init__(
        self, session: AssistanceSession, provider: Provider | None,
        settings: AssistanceSettings, budget: AssistanceBudget,
    ):
        self.session = session
        self.provider = provider
        self.settings = settings
        self.budget = budget
        self.run_dir = Path(session.run_dir)
        self.id = uuid4().hex
        self.telemetry = AssistanceTelemetry(mode=settings.mode)

    def persist(self) -> None:
        self.telemetry.budget = self.budget.snapshot()
        write_record(
            self.run_dir / "generation" / "sessions" / self.id / "telemetry.json",
            "assistance_telemetry", self.telemetry,
        )
        write_record(
            self.run_dir / "generation" / "telemetry.json",
            "assistance_telemetry", self.telemetry,
        )

    def decline(
        self, outcome: StageOutcome, reason: str, *, request_id: str = "",
        last_error: str = "",
    ) -> None:
        self.session.decline_assistance(outcome, reason)
        self.telemetry.declined += 1
        self.telemetry.events.append({
            "stage": outcome.stage.value,
            "status": "declined",
            "reason": reason,
            "request_id": request_id,
            "last_error": last_error,
        })
        self.persist()

    def finish_attempt(
        self, records: AttemptRecords, attempt: JsonObject, started: float,
    ) -> None:
        attempt["duration_ms"] = round((monotonic() - started) * 1000, 3)
        records.telemetry(attempt)
        self.persist()

    @staticmethod
    def rejection(
        request: GenerationRequest, proposal_id: str, code: str, message: str,
    ) -> ValidationRecord:
        return ValidationRecord(
            proposal_id, "rejected", (Diagnostic(request.stage, code, message),),
            validator_version="assistance-envelope-v1",
        )

    @staticmethod
    def record_decision(
        records: AttemptRecords, validation: ValidationRecord, status: str, reason: str,
    ) -> None:
        records.validation(validation)
        records.adoption(AdoptionRecord(
            validation.proposal_id, digest(validation), status, reason,
        ))

    def assist(self, outcome: StageOutcome) -> None:
        if not self.settings.allows(outcome.stage):
            reason = "assistance_off" if self.settings.mode == "off" else "stage_excluded"
            self.decline(outcome, reason)
            return
        if self.provider is None:
            self.decline(outcome, "provider_not_configured")
            return
        transport_mode = getattr(self.provider, "mode", self.settings.mode)
        if transport_mode != self.settings.mode:
            self.decline(outcome, "provider_mode_mismatch")
            return
        base = self.session.assistance_request(outcome)
        if base is None:
            self.decline(outcome, "request_unavailable")
            return
        try:
            validate_request(base)
            if base.stage != outcome.stage:
                raise ValueError("Generation request stage differs from the suspended stage")
            base = deepcopy(base)
        except (ValueError, TypeError) as error:
            self.decline(outcome, "invalid_request", last_error=str(error))
            return

        feedback: tuple[str, ...] = ()
        last_error = ""
        last_request_id = ""
        while True:
            request = replace(base, feedback=(*base.feedback, *feedback))
            ordinal, reason = self.budget.reserve(request)
            if ordinal is None:
                self.decline(
                    outcome, reason, request_id=last_request_id, last_error=last_error,
                )
                return
            last_request_id = request.id
            started = monotonic()
            records = AttemptRecords(self.run_dir, request, f"{ordinal:06d}-{self.id}")
            records.start(build_prompt(request))
            attempt: JsonObject = {
                "request_id": request.id,
                "stage": request.stage.value,
                "target_id": request.target_id,
                "ordinal": ordinal,
                "mode": self.settings.mode,
                "status": "requested",
            }
            self.telemetry.requests += 1
            self.telemetry.attempts.append(attempt)
            records.telemetry(attempt)
            self.persist()
            try:
                # Frozen dataclasses contain mutable context objects; isolate the transport.
                response = self.provider.generate(deepcopy(request))
            except ProviderError as error:
                records.provider_error(error.to_dict())
                self.telemetry.provider_errors += 1
                attempt.update({"status": "provider_error", "error": error.to_dict()})
                last_error = f"{error.code}: {error.message}"
                self.finish_attempt(records, attempt, started)
                if not error.retryable:
                    self.decline(
                        outcome, error.code, request_id=request.id, last_error=last_error,
                    )
                    return
                feedback += (f"Previous provider attempt failed: {last_error}",)
                continue
            if not isinstance(response, RawResponse):
                raise TypeError("Provider.generate must return a RawResponse")
            records.response(response)
            try:
                proposal = parse_proposal(request, response)
            except ProposalParseError as error:
                validation = self.rejection(request, "", error.code, error.message)
                self.record_decision(records, validation, "declined", error.code)
                self.telemetry.parse_errors += 1
                attempt.update({
                    "status": "parse_error",
                    "error": {"code": error.code, "message": error.message},
                })
                last_error = f"{error.code}: {error.message}"
                feedback += (f"Previous response was rejected: {last_error}",)
                self.finish_attempt(records, attempt, started)
                continue
            records.proposal(proposal)
            attempt["proposal_id"] = proposal.id
            repeated = not self.budget.claim_candidate(request, proposal)
            if repeated:
                validation = self.rejection(
                    request, proposal.id, "repeated_candidate",
                    "The same candidate content was already attempted for this problem and stage",
                )
                self.telemetry.repeated_candidates += 1
            else:
                validation = self.session.validate_proposal(request, proposal)
                if not isinstance(validation, ValidationRecord):
                    raise TypeError("Session validator must return a ValidationRecord")
                if validation.proposal_id != proposal.id:
                    validation = self.rejection(
                        request, proposal.id, "validation_mismatch",
                        "ValidationRecord does not identify the current proposal",
                    )
            attempt["validation_status"] = validation.status
            if validation.status in _ACCEPTED:
                self.record_decision(records, validation, "pending", "awaiting_session_adoption")
                attempt["status"] = "validated"
                self.finish_attempt(records, attempt, started)
                self.session.adopt_proposal(request, proposal, validation)
                records.adoption(AdoptionRecord(
                    proposal.id, digest(validation), "adopted", validation.status,
                ))
                self.telemetry.adopted += 1
                attempt["status"] = "adopted"
                self.finish_attempt(records, attempt, started)
                return
            self.record_decision(records, validation, "declined", validation.status)
            self.telemetry.rejected += 1
            last_error = (
                f"Validation status {validation.status}: "
                + "; ".join(f"{d.code}: {d.message}" for d in validation.diagnostics)
            )
            feedback += (f"Previous candidate was not adopted. {last_error}",)
            attempt["status"] = "repeated_candidate" if repeated else "rejected"
            self.finish_attempt(records, attempt, started)
            if repeated:
                self.decline(
                    outcome, "repeated_candidate", request_id=request.id, last_error=last_error,
                )
                return

    def run(self) -> AssistanceTelemetry:
        while not self.session.finished:
            outcome = self.session.advance()
            if outcome.status == "needs_assistance":
                self.assist(outcome)
        self.persist()
        return self.telemetry


def run_assisted(
    session: AssistanceSession, provider: Provider | None, settings: AssistanceSettings,
    budget: AssistanceBudget | None = None,
) -> AssistanceTelemetry:
    """Drive explicit stage gaps only; providers must enforce their transport timeout."""
    if budget is None:
        budget = AssistanceBudget(settings)
    elif budget.settings.budget_limits != settings.budget_limits:
        raise ValueError("A shared assistance budget must use the same limits as its sessions")
    return _AssistedRun(session, provider, settings, budget).run()


def run_unassisted(session: AssistanceSession) -> AssistanceTelemetry:
    return run_assisted(session, None, AssistanceSettings())
