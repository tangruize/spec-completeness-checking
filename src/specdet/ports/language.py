from __future__ import annotations

from pathlib import Path
from typing import Protocol

from specdet.domain.models import (
    CheckEvidence, Contract, Diagnostic, JsonObject, Obligation, ObservationPlan,
    ProofCandidate, ProofCheckEvidence, SearchResult, Stage, TargetRef, CounterexampleSearchResult,
)
from specdet.domain.proposals import GenerationRequest, Proposal, ValidationRecord
from specdet.storage.workspace import PreparedProject


class LanguageBackend(Protocol):
    def toolchain_identity(self) -> JsonObject: ...

    def discover(self, project: PreparedProject) -> tuple[list[TargetRef], list[Diagnostic]]: ...

    def extract(self, project: PreparedProject, target: TargetRef) -> Contract: ...

    def observations(
        self, project: PreparedProject, contract: Contract,
        *, analysis_kind: str | None = None,
    ) -> ObservationPlan: ...

    def lower(
        self, project: PreparedProject, contract: Contract, observations: ObservationPlan,
    ) -> Obligation: ...

    def generate_proof(
        self, project: PreparedProject, contract: Contract, obligation: Obligation,
        strategy: str, feedback: tuple[Diagnostic, ...] = (),
    ) -> ProofCandidate | None: ...

    def check(
        self, project: PreparedProject, obligation: Obligation, candidate: ProofCandidate,
        attempt_dir: Path, *, baseline: bool = False,
    ) -> ProofCheckEvidence: ...

    def query(self, obligation: Obligation, baseline: ProofCheckEvidence) -> CheckEvidence: ...

    def search(
        self, obligation: Obligation, baseline: ProofCheckEvidence, artifact_dir: Path,
        *, max_rounds: int | None = None,
    ) -> SearchResult: ...

    def find_counterexample(
        self, project: PreparedProject, obligation: Obligation, artifact_dir: Path,
        *, max_candidates: int,
    ) -> CounterexampleSearchResult: ...

    def generation_context(
        self, stage: Stage, project: PreparedProject | None, target: TargetRef | None,
        contract: Contract | None, obligation: Obligation | None,
        diagnostics: tuple[Diagnostic, ...],
    ) -> JsonObject: ...

    def validate_proposal(
        self, request: GenerationRequest, proposal: Proposal,
        project: PreparedProject | None, target: TargetRef | None,
        contract: Contract | None, obligation: Obligation | None,
    ) -> ValidationRecord: ...

    def adopt_proposal(
        self, proposal: Proposal, project: PreparedProject | None,
        target: TargetRef | None, contract: Contract | None, obligation: Obligation | None,
    ) -> Stage: ...
