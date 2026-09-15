from __future__ import annotations

from threading import Lock

from specdet.domain.models import JsonObject, Stage, digest
from specdet.domain.proposals import GenerationRequest, Proposal

from .settings import AssistanceSettings


class AssistanceBudget:
    """Share one instance across a batch; stage limits are per target (or run scope)."""

    def __init__(self, settings: AssistanceSettings | None = None):
        self.settings = settings if settings is not None else AssistanceSettings()
        self._requests = 0
        self._targets: dict[str, int] = {}
        self._stages: dict[tuple[str, Stage], int] = {}
        self._candidates: set[str] = set()
        self._lock = Lock()

    @property
    def requests(self) -> int:
        with self._lock:
            return self._requests

    def reserve(self, request: GenerationRequest) -> tuple[int | None, str]:
        """Charge every transport call, including retries and targetless requests."""
        with self._lock:
            stage_key = (request.target_id, request.stage)
            if self._requests >= self.settings.max_requests_per_run:
                return None, "run_budget_exhausted"
            if (
                request.target_id
                and self._targets.get(request.target_id, 0)
                >= self.settings.max_requests_per_target
            ):
                return None, "target_budget_exhausted"
            if self._stages.get(stage_key, 0) >= self.settings.max_rounds_per_stage:
                return None, "stage_budget_exhausted"
            self._requests += 1
            self._stages[stage_key] = self._stages.get(stage_key, 0) + 1
            if request.target_id:
                self._targets[request.target_id] = self._targets.get(request.target_id, 0) + 1
            return self._requests, ""

    def claim_candidate(self, request: GenerationRequest, proposal: Proposal) -> bool:
        # The transport ID changes with feedback; candidate identity must not.
        key = digest({
            "target_id": request.target_id,
            "stage": proposal.stage,
            "language": proposal.language,
            "kind": proposal.kind,
            "base_source_digest": proposal.base_source_digest,
            "base_problem_id": proposal.base_problem_id,
            "input_context": request.context,
            "payload": proposal.payload,
        })
        with self._lock:
            if key in self._candidates:
                return False
            self._candidates.add(key)
            return True

    def snapshot(self) -> JsonObject:
        with self._lock:
            stages: list[JsonObject] = [
                {"target_id": target, "stage": stage.value, "requests": count}
                for (target, stage), count in sorted(
                    self._stages.items(), key=lambda item: (item[0][0], item[0][1].value),
                )
            ]
            return {
                "requests": self._requests,
                "requests_per_target": dict(sorted(self._targets.items())),
                "requests_per_stage": stages,
                "distinct_candidates": len(self._candidates),
                "max_rounds_per_stage": self.settings.max_rounds_per_stage,
                "max_requests_per_target": self.settings.max_requests_per_target,
                "max_requests_per_run": self.settings.max_requests_per_run,
            }
