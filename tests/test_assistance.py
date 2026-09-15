from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from specdet.assistance import (
    AssistanceBudget, AssistanceSettings, run_assisted, run_unassisted,
)
from specdet.assistance.records import atomic_json
from specdet.domain.capabilities import (
    DEFAULT_CAPABILITIES, assistance_route, capability_for, is_assistance_trigger,
)
from specdet.domain.models import (
    Diagnostic, Stage, StageOutcome, Verdict, canonical_json, digest,
)
from specdet.domain.proposals import GenerationRequest, Proposal, RawResponse, ValidationRecord
from specdet.providers import ProviderError, ReplayProvider


def request_for(stage: Stage, target_id: str = "target", context: dict | None = None):
    early = stage in {Stage.CONFIG, Stage.DISCOVER, Stage.PREPARE}
    return GenerationRequest(
        stage=stage, language="verus", allowed_kinds=capability_for(stage).kinds,
        source_digest="frozen-source", problem_id="" if early else "frozen-problem",
        context={"source": "fn example() {}", **(context or {})},
        target_id="" if early else target_id,
    )


def candidate_response(request: GenerationRequest, *, payload: dict | None = None):
    proposal = Proposal(
        request_id=request.id, stage=request.stage, language=request.language,
        kind=request.allowed_kinds[0], base_source_digest=request.source_digest,
        base_problem_id=request.problem_id,
        payload=payload if payload is not None else {"text": "candidate"},
    )
    return RawResponse(request.id, canonical_json(proposal), "fixture", "test-model")


class FakeProvider:
    mode = "live"

    def __init__(self, generate=None):
        self.requests = []
        self.respond = generate or candidate_response

    def generate(self, request):
        self.requests.append(request)
        return self.respond(request)


class FakeSession:
    def __init__(self, run_dir, stages, *, target_id="target", statuses=()):
        self.run_dir = run_dir
        self.stages = list(stages)
        self.target_id = target_id
        self.statuses = iter(statuses)
        self.index = 0
        self.pending = None
        self.request_count = 0
        self.validated = []
        self.adopted = []
        self.declined = []
        self.advanced = []
        self.proof_checks = 0
        self.verdict = Verdict.INCONCLUSIVE

    @property
    def finished(self):
        return self.index == len(self.stages) and self.pending is None

    def advance(self):
        if self.pending is not None:
            raise AssertionError("Controller did not resolve the suspended stage")
        item = self.stages[self.index]
        self.index += 1
        outcome = item if isinstance(item, StageOutcome) else StageOutcome(
            item, "needs_assistance",
            (Diagnostic(item, capability_for(item).triggers[0], "mechanical stage gap"),),
        )
        self.advanced.append(outcome.stage)
        if outcome.status == "needs_assistance":
            self.pending = outcome
        if outcome.stage is Stage.PROOF_CHECKING:
            self.proof_checks += 1
        return outcome

    def make_request(self, outcome):
        return request_for(
            outcome.stage, self.target_id, {"stage_input_digest": outcome.input_digest},
        )

    def assistance_request(self, outcome):
        self.request_count += 1
        return self.make_request(outcome)

    def validate_proposal(self, request, proposal):
        self.validated.append(proposal)
        default = "accepted_for_check" if proposal.kind == "proof" else "accepted"
        status = next(self.statuses, default)
        return ValidationRecord(proposal.id, status)

    def adopt_proposal(self, request, proposal, validation):
        self.adopted.append((proposal, validation))
        self.pending = None

    def decline_assistance(self, outcome, reason):
        self.declined.append((outcome.stage, reason))
        self.pending = None


class ArtifactCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / f".assistance-work-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def settings(self, **overrides):
        values = {
            "mode": "live", "max_rounds_per_stage": 2,
            "max_requests_per_target": 20, "max_requests_per_run": 40,
        }
        values.update(overrides)
        return AssistanceSettings(**values)

    def data(self, path):
        envelope = json.loads(path.read_text())
        self.assertEqual(envelope["schema_version"], 1)
        return envelope["data"]


class CapabilityAndSettingsTests(unittest.TestCase):
    def test_parent_config_mapping_is_supported(self):
        mapping = {
            "mode": "replay",
            "stage_policy": "all_applicable",
            "excluded_stages": ["config", "report"],
            "max_rounds_per_stage": 2,
            "max_requests_per_target": 3,
            "max_requests_per_run": 30,
            "request_timeout_seconds": 300,
            "provider": "copilot",
            "model": "recorded-model",
            "responses": "recorded/generation",
        }
        settings = AssistanceSettings.from_dict(mapping)
        self.assertEqual(settings.excluded_stages, ("config", "report"))
        self.assertEqual(settings.to_dict(), {
            **mapping, "executable": "copilot", "command": [], "text_only": False,
        })

    def test_every_stage_has_an_explicit_capability(self):
        self.assertEqual(set(DEFAULT_CAPABILITIES), set(Stage))
        for stage in Stage:
            with self.subTest(stage=stage):
                capability = capability_for(stage.value)
                self.assertEqual(capability.stage, stage)
                self.assertTrue(capability.kinds)
                self.assertTrue(capability.triggers)
                self.assertTrue(capability.reason)
        self.assertEqual(capability_for(Stage.PROOF_CHECKING).kinds, ("diagnostic",))
        self.assertEqual(capability_for(Stage.QUERY).kinds, ("diagnostic", "search_plan"))
        self.assertEqual(capability_for(Stage.PROOF_GENERATION).kinds, ("proof",))

    def test_only_explicit_gap_codes_route(self):
        self.assertTrue(is_assistance_trigger(Stage.EXTRACT, "type_gap"))
        self.assertTrue(is_assistance_trigger(Stage.DISCOVER, "partial_discovery"))
        self.assertEqual(
            assistance_route(Stage.DISCOVER, "partial_discovery"), Stage.DISCOVER,
        )
        self.assertFalse(is_assistance_trigger(Stage.EXTRACT, "permission_denied"))
        self.assertFalse(is_assistance_trigger(Stage.CONFIG, "executable_not_found"))
        self.assertEqual(assistance_route(Stage.PROOF_CHECKING, "unproved"), Stage.PROOF_GENERATION)
        self.assertIsNone(assistance_route(Stage.QUERY, "arbitrary_exception"))
        for stage in Stage:
            for code in (
                "no_contract", "unsupported_mutable_return", "snapshot_modified",
                "verifier_modified_input", "goal_mismatch", "goal_changed", "not_baseline",
            ):
                with self.subTest(stage=stage, code=code):
                    self.assertFalse(is_assistance_trigger(stage, code))
                    self.assertIsNone(assistance_route(stage, code))

    def test_defaults_and_strict_setting_validation(self):
        settings = AssistanceSettings()
        self.assertEqual(settings.mode, "off")
        self.assertEqual(settings.budget_limits, (2, 3, 30))
        self.assertEqual(settings.request_timeout_seconds, 300)
        for options in (
            {"mode": "auto"}, {"mode": []}, {"stage_policy": "any_exception"},
            {"excluded_stages": ("nonexistent",)}, {"excluded_stages": "extract"},
            {"max_rounds_per_stage": 0}, {"max_requests_per_target": -1},
            {"max_requests_per_run": True}, {"max_requests_per_run": 1.5},
            {"request_timeout_seconds": 0}, {"request_timeout_seconds": True},
            {"request_timeout_seconds": float("nan")},
            {"request_timeout_seconds": float("inf")}, {"text_only": 1},
            {"command": "shell command"}, {"command": ("",)},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                AssistanceSettings(**options)
        with self.assertRaises(ValueError):
            AssistanceSettings.from_dict({"invented_option": True})
        parsed = AssistanceSettings.from_dict({
            "mode": "live", "excluded_stages": ["extract"], "command": ["text-client"],
        })
        self.assertEqual(parsed.excluded_stages, ("extract",))
        self.assertFalse(parsed.allows(Stage.EXTRACT))
        self.assertTrue(parsed.allows(Stage.PREPARE))


class ControllerTests(ArtifactCase):
    def test_actual_fallback_at_every_applicable_stage(self):
        stages = list(Stage)
        session = FakeSession(self.root, stages)
        provider = FakeProvider()
        telemetry = run_assisted(session, provider, self.settings())
        self.assertTrue(session.finished)
        self.assertEqual([r.stage for r in provider.requests], stages)
        self.assertEqual([p.stage for p, _ in session.adopted], stages)
        self.assertEqual(telemetry.requests, len(stages))
        self.assertEqual(telemetry.adopted, len(stages))
        self.assertEqual(telemetry.declined, 0)
        self.assertTrue(all(p.origin == "llm" for p, _ in session.adopted))
        self.assertEqual(session.verdict, Verdict.INCONCLUSIVE)

    def test_explicit_stage_specific_gap_does_not_require_a_generic_code(self):
        outcome = StageOutcome(
            Stage.PREPARE, "needs_assistance",
            (Diagnostic(Stage.PREPARE, "verus_profile_variant_gap", "supported fallback"),),
        )
        session = FakeSession(self.root, [outcome])
        telemetry = run_assisted(session, FakeProvider(), self.settings())
        self.assertEqual(telemetry.adopted, 1)

    def test_all_non_assistance_outcomes_continue_without_provider(self):
        session = FakeSession(self.root, [
            StageOutcome(Stage.CONFIG, "configured"),
            StageOutcome(Stage.EXTRACT, "failed"),
            StageOutcome(Stage.PROOF_GENERATION, "generated"),
            StageOutcome(Stage.PROOF_CHECKING, "unproved"),
            StageOutcome(Stage.QUERY, "unknown"),
            StageOutcome(Stage.REPORT, "completed"),
        ])
        provider = Mock()
        provider.generate.side_effect = AssertionError("No explicit gap")
        telemetry = run_assisted(session, provider, self.settings())
        self.assertTrue(session.finished)
        self.assertEqual(telemetry.requests, 0)
        self.assertEqual(session.request_count, 0)
        provider.generate.assert_not_called()

    def test_off_mode_does_not_construct_requests_or_call_provider(self):
        session = FakeSession(self.root, list(Stage))
        provider = Mock()
        telemetry = run_assisted(session, provider, AssistanceSettings())
        self.assertEqual(telemetry.requests, 0)
        self.assertEqual(telemetry.declined, len(Stage))
        self.assertEqual(session.request_count, 0)
        self.assertTrue(all(reason == "assistance_off" for _, reason in session.declined))
        provider.generate.assert_not_called()
        other = FakeSession(self.root / "unassisted", [Stage.CONFIG])
        self.assertEqual(run_unassisted(other).requests, 0)

    def test_excluded_stages_and_absent_provider_decline_explicitly(self):
        session = FakeSession(self.root, [Stage.CONFIG, Stage.EXTRACT, Stage.REPORT])
        provider = FakeProvider()
        telemetry = run_assisted(
            session, provider, self.settings(excluded_stages=("config", "extract")),
        )
        self.assertEqual([r.stage for r in provider.requests], [Stage.REPORT])
        self.assertEqual(telemetry.declined, 2)
        absent = FakeSession(self.root / "absent", [Stage.PREPARE])
        telemetry = run_assisted(absent, None, self.settings())
        self.assertEqual(telemetry.requests, 0)
        self.assertEqual(absent.declined[0][1], "provider_not_configured")

    def test_unavailable_or_out_of_stage_request_does_not_consume_budget(self):
        for case in ("missing", "wrong_stage", "wrong_kind"):
            with self.subTest(case=case):
                session = FakeSession(self.root / case, [Stage.EXTRACT])
                if case == "missing":
                    session.assistance_request = lambda outcome: None
                elif case == "wrong_stage":
                    session.assistance_request = lambda outcome: request_for(Stage.PROOF_GENERATION)
                else:
                    session.assistance_request = lambda outcome: replace(
                        request_for(Stage.EXTRACT), allowed_kinds=("proof",),
                    )
                provider = FakeProvider()
                telemetry = run_assisted(session, provider, self.settings())
                self.assertEqual(telemetry.requests, 0)
                self.assertFalse(provider.requests)
                self.assertTrue(session.finished)

    def test_proof_generation_never_becomes_proof_verification(self):
        session = FakeSession(self.root, [
            Stage.PROOF_GENERATION,
            StageOutcome(Stage.PROOF_CHECKING, "unproved"),
            StageOutcome(Stage.QUERY, "unknown"),
            StageOutcome(Stage.REPORT, "completed"),
        ])
        provider = FakeProvider(lambda r: candidate_response(r, payload={"proof": "assert(true);"}))
        telemetry = run_assisted(session, provider, self.settings())
        proposal, validation = session.adopted[0]
        self.assertEqual(proposal.kind, "proof")
        self.assertEqual(validation.status, "accepted_for_check")
        self.assertEqual(session.proof_checks, 1)
        self.assertEqual(session.verdict, Verdict.INCONCLUSIVE)
        self.assertEqual(telemetry.adopted, 1)
        self.assertNotIn("verified", telemetry.to_dict())
        adoption = self.data(self.root / "proposals" / proposal.id / "adoption.json")
        self.assertEqual(adoption["status"], "adopted")
        self.assertEqual(adoption["reason"], "accepted_for_check")

    def test_mechanical_proof_failure_can_return_to_generation(self):
        session = FakeSession(self.root, [
            Stage.PROOF_GENERATION,
            StageOutcome(Stage.PROOF_CHECKING, "unproved"),
            StageOutcome(Stage.PROOF_GENERATION, "needs_assistance", input_digest="failed-proof-1"),
            StageOutcome(Stage.PROOF_CHECKING, "unproved"),
        ])
        provider = FakeProvider()
        provider.respond = lambda r: candidate_response(r, payload={
            "proof": f"candidate_{len(provider.requests)}",
        })
        telemetry = run_assisted(session, provider, self.settings())
        self.assertEqual(telemetry.requests, 2)
        self.assertEqual(session.proof_checks, 2)
        self.assertNotEqual(provider.requests[0].id, provider.requests[1].id)
        self.assertEqual(session.verdict, Verdict.INCONCLUSIVE)

    def test_feedback_changes_hash_and_is_persisted_with_every_record(self):
        provider = FakeProvider()
        provider.respond = lambda r: (
            RawResponse(r.id, "not JSON", "fixture")
            if len(provider.requests) == 1 else candidate_response(r)
        )
        session = FakeSession(self.root, [Stage.EXTRACT])
        session.validate_proposal = Mock(wraps=session.validate_proposal)
        session.adopt_proposal = Mock(wraps=session.adopt_proposal)
        telemetry = run_assisted(session, provider, self.settings())
        first, second = provider.requests
        self.assertNotEqual(first.id, second.id)
        self.assertTrue(second.feedback)
        session.validate_proposal.assert_called_once()
        session.adopt_proposal.assert_called_once()
        self.assertEqual(session.validate_proposal.call_args.args[0], second)
        self.assertEqual(session.adopt_proposal.call_args.args[0], second)
        self.assertEqual(session.validate_proposal.call_args.args[1].request_id, second.id)
        self.assertEqual(telemetry.parse_errors, 1)
        self.assertEqual(telemetry.adopted, 1)
        for request in provider.requests:
            directory = self.root / "generation" / request.id
            data = self.data(directory / "request.json")
            self.assertEqual(data["request_id"], digest(data["request"]))
            self.assertEqual(data["request"], request.to_dict())
            self.assertIn(request.id, data["prompt"])
            self.data(directory / "response.json")
            self.data(directory / "validation.json")
            self.data(directory / "adoption.json")
            self.data(directory / "telemetry.json")
            self.assertEqual(len(list((directory / "attempts").iterdir())), 1)
        proposal, validation = session.adopted[0]
        proposal_dir = self.root / "proposals" / proposal.id
        self.assertEqual(self.data(proposal_dir / "proposal.json"), proposal.to_dict())
        adoption = self.data(proposal_dir / "adoption.json")
        self.assertEqual(adoption["validation_digest"], digest(validation))
        self.assertEqual(adoption["status"], "adopted")
        self.assertFalse(list(self.root.rglob("*.pending")))

    def test_no_automatic_acceptance_of_unverified_or_approval_statuses(self):
        for status in ("needs_approval", "unverified", "rejected", "verified", "unknown"):
            with self.subTest(status=status):
                session = FakeSession(self.root / status, [Stage.OBSERVATIONS], statuses=(status,))
                telemetry = run_assisted(
                    session, FakeProvider(), self.settings(max_rounds_per_stage=1),
                )
                self.assertFalse(session.adopted)
                self.assertEqual(telemetry.rejected, 1)
                self.assertEqual(session.declined[0][1], "stage_budget_exhausted")

    def test_validator_identity_must_match_candidate(self):
        session = FakeSession(self.root, [Stage.LOWER])
        session.validate_proposal = lambda request, proposal: ValidationRecord(
            "../wrong-proposal", "accepted",
        )
        telemetry = run_assisted(session, FakeProvider(), self.settings(max_rounds_per_stage=1))
        self.assertEqual(telemetry.adopted, 0)
        validation = self.data(
            self.root / "generation" / telemetry.attempts[0]["request_id"] / "validation.json",
        )
        self.assertEqual(validation["diagnostics"][0]["code"], "validation_mismatch")

    def test_same_candidate_is_detected_despite_new_feedback_request_id(self):
        session = FakeSession(self.root, [Stage.EXTRACT], statuses=("rejected", "rejected"))
        provider = FakeProvider()
        telemetry = run_assisted(session, provider, self.settings(max_rounds_per_stage=5))
        self.assertEqual(telemetry.requests, 2)
        self.assertEqual(telemetry.repeated_candidates, 1)
        self.assertNotEqual(provider.requests[0].id, provider.requests[1].id)
        self.assertEqual(len(session.validated), 1)
        self.assertEqual(session.declined[0][1], "repeated_candidate")

    def test_expected_transport_retry_is_charged_and_recorded(self):
        provider = FakeProvider()

        def generate(request):
            if len(provider.requests) == 1:
                raise ProviderError("provider_timeout", "transport timed out", retryable=True)
            return candidate_response(request)

        provider.respond = generate
        session = FakeSession(self.root, [Stage.PREPARE])
        settings = self.settings()
        budget = AssistanceBudget(settings)
        telemetry = run_assisted(session, provider, settings, budget)
        self.assertEqual(budget.requests, 2)
        self.assertEqual(telemetry.provider_errors, 1)
        self.assertEqual(telemetry.adopted, 1)
        error = self.data(
            self.root / "generation" / provider.requests[0].id / "telemetry.json",
        )
        self.assertEqual(error["error"]["code"], "provider_timeout")

    def test_retryable_failure_exhausts_and_never_becomes_success(self):
        def fail(request):
            raise ProviderError("provider_timeout", "timeout", retryable=True)

        session = FakeSession(self.root, [Stage.CONFIG])
        telemetry = run_assisted(session, FakeProvider(fail), self.settings())
        self.assertEqual(telemetry.requests, 2)
        self.assertEqual(telemetry.provider_errors, 2)
        self.assertFalse(session.adopted)
        self.assertEqual(session.declined[0][1], "stage_budget_exhausted")

    def test_arbitrary_provider_and_mechanical_exceptions_are_not_fallbacks(self):
        session = FakeSession(self.root / "provider", [Stage.EXTRACT])
        provider = FakeProvider(Mock(side_effect=RuntimeError("provider bug")))
        with self.assertRaisesRegex(RuntimeError, "provider bug"):
            run_assisted(session, provider, self.settings())
        self.assertEqual(len(provider.requests), 1)
        session = FakeSession(self.root / "mechanical", [Stage.EXTRACT])
        session.advance = Mock(side_effect=OSError("disk failed"))
        provider = FakeProvider()
        with self.assertRaisesRegex(OSError, "disk failed"):
            run_assisted(session, provider, self.settings())
        self.assertFalse(provider.requests)
        session = FakeSession(self.root / "validator", [Stage.EXTRACT])
        session.validate_proposal = Mock(side_effect=RuntimeError("validator bug"))
        with self.assertRaisesRegex(RuntimeError, "validator bug"):
            run_assisted(session, FakeProvider(), self.settings())
        self.assertFalse(session.adopted)

    def test_failed_adoption_is_recorded_as_pending_not_adopted(self):
        session = FakeSession(self.root, [Stage.EXTRACT])
        session.adopt_proposal = Mock(side_effect=RuntimeError("adoption failed"))
        with self.assertRaisesRegex(RuntimeError, "adoption failed"):
            run_assisted(session, FakeProvider(), self.settings())
        proposal = session.validated[0]
        data = self.data(self.root / "proposals" / proposal.id / "adoption.json")
        self.assertEqual(data["status"], "pending")

    def test_provider_cannot_mutate_the_controller_request(self):
        def mutate(request):
            original = candidate_response(request)
            request.context["injected"] = True
            return original

        session = FakeSession(self.root, [Stage.EXTRACT])
        telemetry = run_assisted(session, FakeProvider(mutate), self.settings())
        self.assertEqual(telemetry.adopted, 1)
        data = self.data(
            self.root / "generation" / telemetry.attempts[0]["request_id"] / "request.json",
        )
        self.assertNotIn("injected", data["request"]["context"])

    def test_replay_mode_refuses_a_known_live_transport(self):
        session = FakeSession(self.root, [Stage.EXTRACT])
        provider = FakeProvider()
        telemetry = run_assisted(session, provider, self.settings(mode="replay"))
        self.assertEqual(telemetry.requests, 0)
        self.assertFalse(provider.requests)
        self.assertEqual(session.declined[0][1], "provider_mode_mismatch")

    def test_captured_responses_replay_directly_but_still_require_validation(self):
        first = FakeSession(self.root / "capture", [Stage.EXTRACT])
        run_assisted(first, FakeProvider(), self.settings())
        replay = ReplayProvider(first.run_dir / "generation")
        second = FakeSession(self.root / "replay", [Stage.EXTRACT], statuses=("unverified",))
        telemetry = run_assisted(
            second, replay, self.settings(mode="replay", max_rounds_per_stage=1),
        )
        self.assertEqual(telemetry.requests, 1)
        self.assertEqual(len(second.validated), 1)
        self.assertFalse(second.adopted)

    def test_captured_transport_retry_replays_with_identical_feedback_ids(self):
        provider = FakeProvider()

        def generate(request):
            if len(provider.requests) == 1:
                raise ProviderError(
                    "provider_timeout", "recorded timeout", retryable=True,
                    metadata={"stdout": "partial", "stderr": ""},
                )
            return candidate_response(request)

        provider.respond = generate
        first = FakeSession(self.root / "capture", [Stage.EXTRACT])
        run_assisted(first, provider, self.settings())
        second = FakeSession(self.root / "replay", [Stage.EXTRACT])
        result = run_assisted(
            second, ReplayProvider(first.run_dir / "generation"), self.settings(mode="replay"),
        )
        self.assertEqual(result.requests, 2)
        self.assertEqual(result.provider_errors, 1)
        self.assertEqual(result.adopted, 1)
        self.assertEqual(
            [attempt["request_id"] for attempt in result.attempts],
            [request.id for request in provider.requests],
        )

    def test_no_live_modules_are_loaded_by_default_assistance_import(self):
        project = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(project / "src"))
        code = (
            "import sys; from specdet.assistance import AssistanceSettings; "
            "from specdet.providers import create_provider; "
            "assert create_provider(AssistanceSettings()) is None; "
            "assert 'specdet.providers.copilot' not in sys.modules; "
            "assert 'specdet.providers.subprocess' not in sys.modules; "
            "assert not any(x.startswith(('copilot_sdk', 'openai', 'anthropic')) for x in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=project, env=env,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class SharedBudgetTests(ArtifactCase):
    def test_cli_workflow_names_reuse_the_same_controller_and_budget(self):
        from specdet.assistance.workflow import RequestBudget, run_assisted as workflow

        self.assertIs(RequestBudget, AssistanceBudget)
        self.assertIs(workflow, run_assisted)

    def test_batch_run_and_stage_budgets_include_targetless_early_stages(self):
        settings = self.settings(
            max_rounds_per_stage=1, max_requests_per_target=2, max_requests_per_run=3,
        )
        budget = AssistanceBudget(settings)
        provider = FakeProvider()
        first = FakeSession(self.root / "one", [Stage.CONFIG, Stage.EXTRACT], target_id="one")
        run_assisted(first, provider, settings, budget)
        second = FakeSession(
            self.root / "two", [Stage.CONFIG, Stage.PROOF_GENERATION, Stage.SEARCH],
            target_id="two",
        )
        telemetry = run_assisted(second, provider, settings, budget)
        self.assertEqual(budget.requests, 3)
        self.assertEqual(telemetry.requests, 1)
        self.assertEqual(
            second.declined,
            [(Stage.CONFIG, "stage_budget_exhausted"), (Stage.SEARCH, "run_budget_exhausted")],
        )
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["requests_per_target"], {"one": 1, "two": 1})
        early = [r for r in snapshot["requests_per_stage"] if r["stage"] == "config"]
        self.assertEqual(early, [{"target_id": "", "stage": "config", "requests": 1}])

    def test_target_budget_spans_stages_and_sessions(self):
        settings = self.settings(max_requests_per_target=2)
        budget = AssistanceBudget(settings)
        provider = FakeProvider()
        first = FakeSession(self.root / "first", [Stage.EXTRACT, Stage.LOWER], target_id="same")
        run_assisted(first, provider, settings, budget)
        second = FakeSession(self.root / "second", [Stage.SEARCH], target_id="same")
        run_assisted(second, provider, settings, budget)
        third = FakeSession(self.root / "third", [Stage.SEARCH], target_id="other")
        run_assisted(third, provider, settings, budget)
        self.assertEqual(second.declined, [(Stage.SEARCH, "target_budget_exhausted")])
        self.assertEqual(len(third.adopted), 1)
        self.assertEqual(budget.requests, 3)

    def test_replay_misses_consume_shared_budget_and_never_fall_back_live(self):
        settings = self.settings(mode="replay", max_requests_per_run=1)
        budget = AssistanceBudget(settings)
        provider = ReplayProvider(self.root / "empty-replay")
        first = FakeSession(self.root / "first", [Stage.DISCOVER])
        result = run_assisted(first, provider, settings, budget)
        self.assertEqual(result.requests, 1)
        self.assertEqual(first.declined, [(Stage.DISCOVER, "replay_miss")])
        second = FakeSession(self.root / "second", [Stage.PREPARE])
        result = run_assisted(second, provider, settings, budget)
        self.assertEqual(result.requests, 0)
        self.assertEqual(second.declined, [(Stage.PREPARE, "run_budget_exhausted")])
        self.assertEqual(budget.requests, 1)

    def test_all_targetless_stage_failures_are_charged(self):
        settings = self.settings(max_requests_per_run=3)
        budget = AssistanceBudget(settings)
        session = FakeSession(self.root, [Stage.CONFIG, Stage.DISCOVER, Stage.PREPARE, Stage.EXTRACT])
        telemetry = run_assisted(session, FakeProvider(), settings, budget)
        self.assertEqual(telemetry.requests, 3)
        self.assertEqual(budget.snapshot()["requests_per_target"], {})
        self.assertEqual(session.declined, [(Stage.EXTRACT, "run_budget_exhausted")])

    def test_shared_budget_cannot_be_silently_widened(self):
        budget = AssistanceBudget(self.settings(max_requests_per_run=1))
        session = FakeSession(self.root, [Stage.EXTRACT])
        with self.assertRaisesRegex(ValueError, "same limits"):
            run_assisted(session, FakeProvider(), self.settings(max_requests_per_run=10), budget)


class AtomicRecordTests(ArtifactCase):
    def test_attempt_history_survives_reusing_a_request_and_proposal_id(self):
        settings = self.settings()
        budget = AssistanceBudget(settings)
        first = FakeSession(self.root, [Stage.EXTRACT])
        run_assisted(first, FakeProvider(), settings, budget)
        second = FakeSession(self.root, [Stage.EXTRACT])
        run_assisted(second, FakeProvider(), settings, budget)
        proposal = first.adopted[0][0]
        directory = self.root / "proposals" / proposal.id / "attempts"
        decisions = [self.data(path / "adoption.json")["status"] for path in directory.iterdir()]
        self.assertCountEqual(decisions, ["adopted", "declined"])
        request_history = self.root / "generation" / proposal.request_id / "attempts"
        self.assertEqual(len(list(request_history.iterdir())), 2)

    def test_invalid_value_does_not_destroy_previous_record(self):
        path = self.root / "record.json"
        atomic_json(path, {"version": 1})
        with self.assertRaises(ValueError):
            atomic_json(path, {"version": float("nan")})
        self.assertEqual(json.loads(path.read_text()), {"version": 1})
        self.assertFalse(list(self.root.rglob("*.pending")))


if __name__ == "__main__":
    unittest.main()
