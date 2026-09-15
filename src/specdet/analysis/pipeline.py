from __future__ import annotations

import fnmatch
from pathlib import Path

from specdet.config import Config
from specdet.domain.models import (
    AnalysisReport, Contract, Diagnostic, JsonObject, Obligation, ObservationPlan,
    ProofCandidate, ProofCheckEvidence, SolverStatus, Stage, StageError,
    StageOutcome, TargetRef, Verdict, as_object, digest,
)
from specdet.domain.proposals import GenerationRequest, Proposal, ValidationRecord
from specdet.ports.assistance import AssistanceSession
from specdet.ports.language import LanguageBackend
from specdet.storage.artifacts import ArtifactStore
from specdet.storage.workspace import PreparedProject, prepare_project
from .verdicts import classify


def select_targets(targets: list[TargetRef], selectors: tuple[str, ...]) -> list[TargetRef]:
    if not selectors:
        return targets
    selected: dict[str, TargetRef] = {}
    for selector in selectors:
        matches = []
        for target in targets:
            labels = {
                target.name, target.qualified_name, target.id, target.file,
                f"{target.file}:{target.name}",
                f"{target.file}:{target.name}@{target.line}",
                f"{target.qualified_name}@{target.line}",
            }
            if any(label and fnmatch.fnmatchcase(label, selector) for label in labels):
                matches.append(target)
        if not matches:
            raise StageError(Diagnostic(
                Stage.DISCOVER, "target_not_found", f"No target matches {selector!r}",
            ))
        is_pattern = any(character in selector for character in "*?[")
        is_file = all(target.file == selector for target in matches)
        if len(matches) > 1 and not is_pattern and not is_file:
            choices = ", ".join(f"{t.file}:{t.name}@{t.line}" for t in matches)
            raise StageError(Diagnostic(
                Stage.DISCOVER, "ambiguous_target",
                f"Ambiguous target {selector!r}; specify one of: {choices}",
            ))
        selected.update((target.id, target) for target in matches)
    return list(selected.values())


class _Session:
    def __init__(self, backend: LanguageBackend, config: Config, run_dir: Path):
        self.backend = backend
        self.config = config
        self.run_dir = run_dir
        self.store = ArtifactStore(run_dir)
        self.project: PreparedProject | None = None
        self.target: TargetRef | None = None
        self.contract: Contract | None = None
        self.obligation: Obligation | None = None
        self._finished = False
        self._waiting: StageOutcome | None = None
        self._accepted_ids: list[str] = []
        self._budget_target_id = ""

    @property
    def finished(self) -> bool:
        return self._finished

    def _record(self, outcome: StageOutcome) -> StageOutcome:
        self.store.event({"event": "stage_outcome", "outcome": as_object(outcome)})
        if outcome.status == "needs_assistance":
            self._waiting = outcome
        return outcome

    def assistance_request(self, outcome: StageOutcome) -> GenerationRequest | None:
        from specdet.domain.capabilities import capability_for

        capability = capability_for(outcome.stage)
        if not capability.kinds:
            return None
        if capability.triggers and not any(
            diagnostic.code in capability.triggers for diagnostic in outcome.diagnostics
        ):
            return None
        context = self.backend.generation_context(
            outcome.stage, self.project, self.target, self.contract, self.obligation,
            outcome.diagnostics,
        )
        context["snapshot_digest"] = self.project.snapshot_digest if self.project else ""
        context["accepted_proposals"] = list(self._accepted_ids)
        context.update(self._feedback_context())
        return GenerationRequest(
            stage=outcome.stage, language=self.config.language,
            allowed_kinds=capability.kinds,
            source_digest=(
                self.contract.source_digest if self.contract
                else self.target.source_digest if self.target
                else self.project.snapshot_digest if self.project
                else digest(self.config.to_dict())
            ),
            problem_id=self.obligation.problem_id if self.obligation else "",
            context=context,
            target_id=self._budget_target_id or (self.target.id if self.target else ""),
        )

    def _feedback_context(self) -> JsonObject:
        return {}

    def validate_proposal(
        self, request: GenerationRequest, proposal: Proposal,
    ) -> ValidationRecord:
        if (
            proposal.request_id != request.id or proposal.stage != request.stage
            or proposal.language != request.language
            or proposal.kind not in request.allowed_kinds
            or proposal.base_source_digest != request.source_digest
            or proposal.base_problem_id != request.problem_id
        ):
            return ValidationRecord(
                proposal.id, "rejected",
                (Diagnostic(request.stage, "stale_proposal", "Proposal does not match its frozen request"),),
            )
        return self.backend.validate_proposal(
            request, proposal, self.project, self.target, self.contract, self.obligation,
        )

    def _adopt(self, proposal: Proposal, validation: ValidationRecord) -> Stage:
        if validation.proposal_id != proposal.id or validation.status not in {"accepted", "accepted_for_check"}:
            raise ValueError("Cannot adopt an unvalidated or unapproved proposal")
        if proposal.id in self._accepted_ids:
            raise ValueError("The same proposal has already been adopted")
        stage = self.backend.adopt_proposal(
            proposal, self.project, self.target, self.contract, self.obligation,
        )
        self._accepted_ids.append(proposal.id)
        self._waiting = None
        self.store.event({
            "event": "proposal_adopted", "proposal_id": proposal.id,
            "restart_stage": stage.value, "validation": as_object(validation),
        })
        return stage


class ProjectSession(_Session):
    """Preflight and discovery are resumable without embedding a provider."""

    def __init__(
        self, backend: LanguageBackend, config: Config, run_dir: Path,
        *, require_toolchain: bool = True,
    ):
        super().__init__(backend, config, run_dir)
        self.stage = Stage.CONFIG
        self.require_toolchain = require_toolchain
        self.targets: list[TargetRef] = []
        self.diagnostics: list[Diagnostic] = []
        self.failed = False
        self._partial_discovery = False

    def _save_targets(self) -> None:
        self.store.artifact("targets.json", "target_manifest", {
            "targets": [as_object(target) for target in self.targets],
            "diagnostics": [as_object(item) for item in self.diagnostics],
            "coverage": "partial" if self._partial_discovery else "source-visible",
        })

    def advance(self) -> StageOutcome:
        if self.finished:
            return StageOutcome(Stage.DISCOVER, "completed")
        if self._waiting is not None:
            raise RuntimeError("Resolve or decline the pending stage before advancing")
        try:
            if self.stage == Stage.CONFIG:
                identity = self.backend.toolchain_identity() if self.require_toolchain else {"status": "not_required"}
                self.store.artifact("toolchains.json", "toolchain_identity", identity)
                self.stage = Stage.PREPARE
                return self._record(StageOutcome(Stage.CONFIG, "produced"))
            if self.stage == Stage.PREPARE:
                if self.project is None:
                    self.project = prepare_project(self.config, self.run_dir)
                self.stage = Stage.DISCOVER
                return self._record(StageOutcome(Stage.PREPARE, "produced"))
            if self.stage == Stage.DISCOVER:
                if self.project is None:
                    raise RuntimeError("Discovery requires a prepared project")
                targets, diagnostics = self.backend.discover(self.project)
                self.diagnostics.extend(diagnostics)
                if not targets:
                    raise StageError(Diagnostic(
                        Stage.DISCOVER, "discovery_gap", "No analyzable target declarations were discovered",
                    ), recoverable=True)
                self.targets = select_targets(targets, self.config.selectors)
                self._partial_discovery = any(
                    item.code == "partial_discovery" for item in diagnostics
                )
                self._save_targets()
                if self._partial_discovery:
                    return self._record(StageOutcome(
                        Stage.DISCOVER, "needs_assistance", tuple(diagnostics),
                    ))
                self._finished = True
                return self._record(StageOutcome(Stage.DISCOVER, "produced", tuple(diagnostics)))
            raise RuntimeError(f"Invalid project stage: {self.stage}")
        except StageError as error:
            self.diagnostics.append(error.diagnostic)
            outcome = StageOutcome(
                error.diagnostic.stage, "needs_assistance" if error.recoverable else "failed",
                (error.diagnostic,),
            )
            if not error.recoverable:
                self.failed = True
                self._finished = True
            return self._record(outcome)

    def adopt_proposal(
        self, request: GenerationRequest, proposal: Proposal, validation: ValidationRecord,
    ) -> None:
        stage = self._adopt(proposal, validation)
        if stage not in {Stage.CONFIG, Stage.PREPARE, Stage.DISCOVER}:
            raise ValueError(f"Cannot resume project preflight at {stage}")
        self.stage = stage

    def decline_assistance(self, outcome: StageOutcome, reason: str) -> None:
        self.diagnostics.append(Diagnostic(outcome.stage, "assistance_unavailable", reason, "info"))
        self._waiting = None
        if outcome.stage == Stage.DISCOVER and self._partial_discovery and self.targets:
            self._save_targets()
            self._finished = True
            return
        self.failed = True
        self._finished = True
        self.store.artifact("preflight-error.json", "preflight_error", {
            "diagnostics": [as_object(item) for item in self.diagnostics],
        })


class AnalysisSession(_Session):
    def __init__(
        self, backend: LanguageBackend, config: Config, project: PreparedProject,
        target: TargetRef, run_dir: Path, run_id: str,
    ):
        super().__init__(backend, config, run_dir)
        self.project = project
        self.target = target
        self._budget_target_id = target.id
        self._requested_kind = config.analysis_kind
        self._phase_kind = (
            "concrete_determinism"
            if config.analysis_kind == "abstract_determinism" and config.abstract_require_concrete
            else config.analysis_kind
        )
        self.stage = Stage.EXTRACT
        self.report = AnalysisReport(target=target, run_id=run_id, analysis_kind=self._phase_kind)
        self.observation_plan: ObservationPlan | None = None
        self._pending_proof: ProofCandidate | None = None
        self._baseline_check: ProofCheckEvidence | None = None
        self._proof_cursor = 0
        self._proof_count = 0
        self._phase_proof_count = 0
        self._total_proof_limit = config.max_proof_attempts * (
            2 if config.analysis_kind == "abstract_determinism" and config.abstract_require_concrete else 1
        )
        self._force_accepted = False
        self._search_rounds = 0
        self._search_done = False
        self._witness_attempts = 0
        self._after_witness = Stage.PROOF_GENERATION
        self._explanation_requested = False
        self._failed = False
        self._not_applicable = False
        self._verdict_ready = False
        self._revisions = 0

    def _feedback_context(self) -> JsonObject:
        def diagnostic_text(message: str) -> str:
            message = message.replace(str(self.run_dir), "<target-run>")
            if self.project is not None:
                message = message.replace(str(self.project.root), "<project>")
            return message

        return {
            "analysis_kind": self._phase_kind,
            "requested_analysis_kind": self._requested_kind,
            "concrete_prerequisite": {
                "problem_id": self.report.concrete_result.get("problem_id"),
                "verdict": self.report.concrete_result.get("verdict"),
            } if self.report.concrete_result else {"status": "not_run"},
            "proof_feedback": [
                {
                    "candidate_id": proof.candidate_id,
                    "status": proof.status,
                    "diagnostics": [
                        {"code": item.code, "message": diagnostic_text(item.message)}
                        for item in proof.diagnostics
                    ],
                }
                for proof in self.report.proofs
            ],
            "baseline_status": self.report.baseline.status.value if self.report.baseline else "not_run",
            "search_feedback": {
                "confirmed_constraints": list(self.report.search.confirmed_constraints),
                "candidate_constraints": list(self.report.search.candidate_constraints),
            } if self.report.search else {},
            "counterexample": as_object(self.report.counterexample) if self.report.counterexample else None,
        }

    def _artifact(self, name: str, kind: str, data: object) -> None:
        ref = self.store.artifact(
            f"revisions/{self._revisions}/{name}.json", kind, data,
            (self.obligation.id,) if self.obligation else (),
        )
        self.report.artifacts.append(ref)

    def _gap(self, stage: Stage, code: str, message: str) -> StageOutcome:
        return self._record(StageOutcome(
            stage, "needs_assistance", (Diagnostic(stage, code, message, "warning"),),
        ))

    def advance(self) -> StageOutcome:
        if self.finished:
            return StageOutcome(Stage.REPORT, "completed")
        if self._waiting is not None:
            raise RuntimeError("Resolve or decline the pending stage before advancing")
        if self.project is None or self.target is None:
            raise RuntimeError("Analysis session requires a frozen project and target")
        stage = self.stage
        try:
            if stage == Stage.CONFIG:
                self._artifact("toolchains", "toolchain_identity", self.backend.toolchain_identity())
                self.stage = Stage.PREPARE
            elif stage == Stage.PREPARE:
                # Accepted preparation is represented by backend-owned overlays.
                # The original snapshot remains the input to each new attempt.
                self.stage = Stage.DISCOVER
            elif stage == Stage.DISCOVER:
                targets, diagnostics = self.backend.discover(self.project)
                self.report.diagnostics.extend(diagnostics)
                matches = [
                    candidate for candidate in targets
                    if candidate.file == self.target.file and candidate.name == self.target.name
                    and (
                        not self.target.qualified_name
                        or candidate.qualified_name == self.target.qualified_name
                    )
                ]
                if len(matches) != 1:
                    raise StageError(Diagnostic(
                        Stage.DISCOVER, "ambiguous_target",
                        "The original declaration cannot be uniquely reidentified after preparation",
                    ))
                previous = self.target
                self.target = matches[0]
                self.report.target = self.target
                self.report.annotations.append({
                    "kind": "target_reidentified",
                    "previous": as_object(previous), "current": as_object(self.target),
                })
                self.stage = Stage.EXTRACT
            elif stage == Stage.EXTRACT:
                self.contract = self.backend.extract(self.project, self.target)
                self.report.diagnostics.extend(self.contract.diagnostics)
                self._artifact("contract", "contract", self.contract)
                self.stage = Stage.OBSERVATIONS
            elif stage == Stage.OBSERVATIONS:
                if self.contract is None:
                    raise RuntimeError("Observations require a contract")
                self.observation_plan = self.backend.observations(
                    self.project, self.contract, analysis_kind=self._phase_kind,
                )
                self._artifact("observation-plan", "observation_plan", self.observation_plan)
                self.report.coverage["ignored_dimensions"] = list(self.observation_plan.ignored_dimensions)
                self.report.coverage.update(self.observation_plan.coverage)
                self.report.coverage["input_observations"] = list(self.observation_plan.inputs)
                self.stage = Stage.LOWER
            elif stage == Stage.LOWER:
                if self.contract is None or self.observation_plan is None:
                    raise RuntimeError("Lowering requires a contract and observation plan")
                self.obligation = self.backend.lower(self.project, self.contract, self.observation_plan)
                self.report.problem_id = self.obligation.problem_id
                self.report.coverage["trusted_translation"] = (
                    self.contract.trusted_translation and self.obligation.trusted_translation
                )
                self.report.coverage["feasibility"] = "not_run"
                self._artifact("obligation", "obligation", self.obligation)
                self.stage = Stage.PROOF_GENERATION
            elif stage == Stage.PROOF_GENERATION:
                return self._generate_proof()
            elif stage == Stage.PROOF_CHECKING:
                return self._check_proof()
            elif stage == Stage.QUERY:
                if self.obligation is None or self._baseline_check is None:
                    raise RuntimeError("Querying requires baseline compilation")
                self.report.baseline = self.backend.query(self.obligation, self._baseline_check)
                self._artifact("baseline", "check_evidence", self.report.baseline)
                if self.report.baseline.status == SolverStatus.UNSAT:
                    self.stage = Stage.REPORT
                elif self.report.baseline.status == SolverStatus.SAT:
                    self._after_witness = Stage.SEARCH
                    self.stage = Stage.COUNTEREXAMPLE
                elif self._baseline_check.status == "verified":
                    self.stage = Stage.REPORT
                else:
                    self._after_witness = Stage.PROOF_GENERATION
                    self.stage = Stage.COUNTEREXAMPLE
            elif stage == Stage.COUNTEREXAMPLE:
                return self._counterexample()
            elif stage == Stage.SEARCH:
                return self._search()
            elif stage == Stage.REPORT:
                return self._finish()
            else:
                raise RuntimeError(f"Invalid analysis stage: {stage}")
            return self._record(StageOutcome(stage, "produced"))
        except StageError as error:
            self.report.diagnostics.append(error.diagnostic)
            if error.diagnostic.code == "no_abstract_inputs" and self._phase_kind == "abstract_determinism":
                self._not_applicable = True
                self.stage = Stage.REPORT
                return self._record(StageOutcome(error.diagnostic.stage, "skipped", (error.diagnostic,)))
            if error.recoverable:
                return self._record(StageOutcome(
                    error.diagnostic.stage, "needs_assistance", (error.diagnostic,),
                ))
            self._failed = True
            self.stage = Stage.REPORT
            return self._record(StageOutcome(error.diagnostic.stage, "failed", (error.diagnostic,)))

    def _generate_proof(self) -> StageOutcome:
        if self.contract is None or self.obligation is None or self.project is None:
            raise RuntimeError("Proof generation requires a frozen obligation")
        if (
            self._phase_proof_count >= self.config.max_proof_attempts
            or self._proof_count >= self._total_proof_limit
        ):
            self.report.diagnostics.append(Diagnostic(
                Stage.PROOF_GENERATION, "proof_budget_exhausted", "Proof attempt budget exhausted", "info",
            ))
            self.stage = Stage.SEARCH
            return self._record(StageOutcome(Stage.PROOF_GENERATION, "skipped"))
        forced = self._force_accepted
        if forced:
            strategy = "accepted"
            self._force_accepted = False
        elif self._proof_cursor < len(self.config.proof_strategies):
            strategy = self.config.proof_strategies[self._proof_cursor]
            self._proof_cursor += 1
        else:
            self.stage = Stage.SEARCH
            return self._record(StageOutcome(Stage.PROOF_GENERATION, "skipped"))
        feedback = tuple(
            diagnostic for proof in self.report.proofs for diagnostic in proof.diagnostics
        )
        candidate = self.backend.generate_proof(
            self.project, self.contract, self.obligation, strategy, feedback,
        )
        self.store.artifact(
            f"proof-generation/{self._proof_count}-{strategy}-{len(self._accepted_ids)}.json",
            "proof_generation", {
                "strategy": strategy,
                "status": "generated" if candidate else "no_candidate",
                "candidate": as_object(candidate) if candidate else None,
            },
        )
        if candidate is None:
            if strategy == "baseline":
                raise RuntimeError("A backend must provide its baseline proof candidate")
            if strategy == "accepted" and forced:
                return self._gap(Stage.PROOF_GENERATION, "no_candidate", "Accepted proof could not be materialized")
            if strategy == "assisted":
                return self._gap(
                    Stage.PROOF_GENERATION, "no_candidate",
                    "Mechanical proof strategies did not close the frozen determinism obligation",
                )
            return self._record(StageOutcome(Stage.PROOF_GENERATION, "skipped"))
        if candidate.problem_id != self.obligation.problem_id or candidate.obligation_digest != self.obligation.id:
            raise StageError(Diagnostic(
                Stage.PROOF_GENERATION, "goal_drift", "Proof candidate targets a different frozen obligation",
            ))
        self._pending_proof = candidate
        self._artifact(f"proof-candidate-{self._proof_count}", "proof_candidate", candidate)
        self.stage = Stage.PROOF_CHECKING
        return self._record(StageOutcome(Stage.PROOF_GENERATION, "produced"))

    def _check_proof(self) -> StageOutcome:
        if self.project is None or self.obligation is None or self._pending_proof is None:
            raise RuntimeError("Proof checking requires a candidate")
        candidate = self._pending_proof
        is_baseline = candidate.origin == "baseline"
        directory = self.run_dir / "attempts" / f"{self._proof_count:03d}-{candidate.id[:12]}"
        self._proof_count += 1
        self._phase_proof_count += 1
        checked = self.backend.check(
            self.project, self.obligation, candidate, directory, baseline=is_baseline,
        )
        self.report.proofs.append(checked)
        self.report.diagnostics.extend(checked.diagnostics)
        self._artifact(f"proof-check-{self._proof_count}", "proof_check", checked)
        self._pending_proof = None
        if is_baseline:
            self._baseline_check = checked
        if checked.status == "compile_error":
            return self._gap(
                Stage.LOWER, "lowering_gap",
                "Generated harness did not compile; preserve the goal when proposing a correction",
            )
        if checked.status == "verified":
            self.stage = Stage.QUERY if is_baseline else Stage.REPORT
        elif is_baseline:
            self.stage = Stage.QUERY
        else:
            self.stage = Stage.PROOF_GENERATION
        return self._record(StageOutcome(Stage.PROOF_CHECKING, "produced"))

    def _search(self) -> StageOutcome:
        if self._baseline_check is None or self.obligation is None:
            self.stage = Stage.REPORT
            return self._record(StageOutcome(Stage.SEARCH, "skipped"))
        if self._search_rounds >= self.config.limits.max_search_rounds:
            self.report.diagnostics.append(Diagnostic(
                Stage.SEARCH, "search_budget_exhausted", "Total target search budget exhausted", "info",
            ))
            self.stage = Stage.REPORT
            return self._record(StageOutcome(Stage.SEARCH, "skipped"))
        result = self.backend.search(
            self.obligation, self._baseline_check,
            self.run_dir / "search" / f"{self._search_rounds:06d}-{len(self._accepted_ids)}",
            max_rounds=self.config.limits.max_search_rounds - self._search_rounds,
        )
        self._search_rounds += result.rounds
        if self.report.search is not None:
            from dataclasses import replace

            previous = self.report.search
            result = replace(
                result, evidence=previous.evidence + result.evidence,
                confirmed_constraints=result.confirmed_constraints or previous.confirmed_constraints,
                rounds=self._search_rounds,
                diagnostics=previous.diagnostics + result.diagnostics,
            )
        self.report.search = result
        self.report.diagnostics.extend(result.diagnostics)
        self._artifact("search", "search_result", result)
        self._search_done = True
        self.stage = Stage.REPORT
        if (
            not result.confirmed_constraints
            and self._search_rounds < self.config.limits.max_search_rounds
            and not any(e.role == "baseline" and e.status == SolverStatus.UNSAT for e in result.evidence)
        ):
            return self._gap(
                Stage.SEARCH, "search_stalled",
                "Search did not produce a confirmed concrete counterexample slice",
            )
        return self._record(StageOutcome(Stage.SEARCH, "produced"))

    def _counterexample(self) -> StageOutcome:
        if self.project is None or self.obligation is None:
            raise RuntimeError("Counterexample search requires the frozen source problem")
        remaining = self.config.counterexample_candidates - self._witness_attempts
        if remaining <= 0:
            self.stage = self._after_witness
            return self._record(StageOutcome(Stage.COUNTEREXAMPLE, "skipped"))
        result = self.backend.find_counterexample(
            self.project, self.obligation,
            self.run_dir / "counterexamples" / f"{self._witness_attempts:04d}-{len(self._accepted_ids)}",
            max_candidates=remaining,
        )
        self._witness_attempts += result.attempts
        self.report.diagnostics.extend(result.diagnostics)
        self._artifact("counterexample-search", "counterexample_search", result)
        if result.witness is not None:
            if (
                result.witness.problem_id != self.obligation.problem_id
                or result.witness.obligation_digest != self.obligation.id
                or result.witness.status != "verified"
                or result.witness.verified_goals <= 0
            ):
                raise StageError(Diagnostic(
                    Stage.COUNTEREXAMPLE, "witness_identity_mismatch",
                    "Source counterexample is not verified for the current frozen obligation",
                ))
            self.report.counterexample = result.witness
            self._verdict_ready = False
            self.stage = Stage.REPORT
            return self._record(StageOutcome(Stage.COUNTEREXAMPLE, "produced"))
        self.stage = self._after_witness
        if self._witness_attempts < self.config.counterexample_candidates:
            return self._gap(
                Stage.COUNTEREXAMPLE, "counterexample_gap",
                "The mechanical constructor catalog did not produce a source-verified witness",
            )
        return self._record(StageOutcome(
            Stage.COUNTEREXAMPLE, "unsupported" if result.diagnostics else "exhausted",
            result.diagnostics,
        ))

    def _finish(self) -> StageOutcome:
        trusted = (
            (self.contract is None or self.contract.trusted_translation)
            and (self.obligation is None or self.obligation.trusted_translation)
        )
        if not self._verdict_ready:
            self.report.verdict = classify(self.report, trusted_translation=trusted)
            self._verdict_ready = True
        if self._phase_kind == "concrete_determinism" and self._requested_kind == "abstract_determinism":
            return self._finish_prerequisite()
        if (
            not self._failed and not self._explanation_requested
            and self.report.verdict in {Verdict.INCONCLUSIVE, Verdict.NONDETERMINISTIC}
        ):
            self._explanation_requested = True
            return self._gap(
                Stage.REPORT, "explanation_gap",
                "An evidence-linked explanation may help interpret this result",
            )
        self.report.status = (
            "not_applicable" if self._not_applicable
            else "failed" if self._failed else "completed"
        )
        if self._failed and self.report.verdict == Verdict.NOT_EVALUATED:
            unsupported = {"no_contract", "unsupported_return", "unsupported_syntax", "unsupported_capability"}
            if any(item.code in unsupported for item in self.report.diagnostics):
                self.report.status = "unsupported"
        self.report.assistance["accepted_proposals"] = list(self._accepted_ids)
        self.store.artifact("report.json", "analysis_report", self.report)
        self._finished = True
        return self._record(StageOutcome(Stage.REPORT, "produced"))

    def _finish_prerequisite(self) -> StageOutcome:
        self.report.status = "failed" if self._failed else "completed"
        self.report.assistance["accepted_proposals"] = list(self._accepted_ids)
        concrete = self.report.to_dict()
        self.store.artifact("concrete-report.json", "analysis_report", concrete)
        target, run_id = self.report.target, self.report.run_id
        self.report = AnalysisReport(
            target=target, run_id=run_id, analysis_kind="abstract_determinism",
            concrete_result=concrete,
        )
        if concrete["status"] != "completed" or concrete["verdict"] != Verdict.DETERMINISTIC.value:
            self.report.status = "skipped"
            self.report.diagnostics.append(Diagnostic(
                Stage.LOWER, "concrete_prerequisite_unproved",
                "Abstract checking requires a proved concrete-input determinism result",
                "warning", {"concrete_report": "concrete-report.json"},
            ))
            self.store.artifact("report.json", "analysis_report", self.report)
            self._finished = True
            return self._record(StageOutcome(Stage.LOWER, "skipped", tuple(self.report.diagnostics)))
        self._phase_kind = "abstract_determinism"
        self._revisions += 1
        self.observation_plan = None
        self.obligation = None
        self._baseline_check = None
        self._pending_proof = None
        self._proof_cursor = 0
        self._phase_proof_count = 0
        self._search_done = False
        self._explanation_requested = False
        self._failed = False
        self._not_applicable = False
        self._verdict_ready = False
        self.stage = Stage.OBSERVATIONS
        return self._record(StageOutcome(Stage.REPORT, "produced"))

    def adopt_proposal(
        self, request: GenerationRequest, proposal: Proposal, validation: ValidationRecord,
    ) -> None:
        restart = self._adopt(proposal, validation)
        if proposal.kind in {"diagnostic", "explanation"}:
            self.report.annotations.append({
                "kind": proposal.kind, "proposal_id": proposal.id,
                "content": proposal.payload, "status": "generated_annotation",
            })
        if restart in {
            Stage.CONFIG, Stage.DISCOVER, Stage.PREPARE,
            Stage.EXTRACT, Stage.OBSERVATIONS, Stage.LOWER,
        }:
            self.store.artifact(f"revisions/{self._revisions}/superseded-report.json", "analysis_report", self.report)
            self._revisions += 1
            self.report.annotations.append({
                "kind": "problem_revision", "previous_problem_id": self.report.problem_id,
                "proposal_id": proposal.id,
            })
            self.report.baseline = None
            self.report.proofs = []
            self.report.search = None
            self.report.counterexample = None
            self.report.problem_id = ""
            self.obligation = None
            self._baseline_check = None
            self._pending_proof = None
            self._proof_cursor = 0
            self._verdict_ready = False
            self._not_applicable = False
            if (
                self._requested_kind == "abstract_determinism"
                and self.config.abstract_require_concrete
                and self._phase_kind == "abstract_determinism"
            ):
                self._phase_kind = "concrete_determinism"
                self.report.analysis_kind = self._phase_kind
                self.report.concrete_result = None
                self._phase_proof_count = 0
                if restart == Stage.LOWER:
                    restart = Stage.OBSERVATIONS
                    self.observation_plan = None
            if restart in {Stage.CONFIG, Stage.DISCOVER, Stage.PREPARE, Stage.EXTRACT}:
                self.contract = None
                self.observation_plan = None
            elif restart == Stage.OBSERVATIONS:
                self.observation_plan = None
        elif restart == Stage.PROOF_GENERATION:
            self._force_accepted = True
            self._verdict_ready = False
        elif restart == Stage.SEARCH:
            self._verdict_ready = False
        elif restart == Stage.COUNTEREXAMPLE:
            self._verdict_ready = False
        self.stage = restart

    def decline_assistance(self, outcome: StageOutcome, reason: str) -> None:
        self._waiting = None
        self.report.diagnostics.append(Diagnostic(outcome.stage, "assistance_unavailable", reason, "info"))
        if outcome.stage in {Stage.PROOF_GENERATION, Stage.PROOF_CHECKING}:
            self.stage = Stage.SEARCH if not self._search_done else Stage.REPORT
        elif outcome.stage == Stage.COUNTEREXAMPLE:
            self.stage = self._after_witness
        elif outcome.stage in {Stage.SEARCH, Stage.REPORT, Stage.QUERY}:
            self.stage = Stage.REPORT
        else:
            self._failed = True
            self.stage = Stage.REPORT


def run_mechanical(session: AssistanceSession) -> None:
    while not session.finished:
        outcome = session.advance()
        if outcome.status == "needs_assistance":
            session.decline_assistance(outcome, "Live and replay assistance are disabled")
