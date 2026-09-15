"""Fixed, source-sealed proof candidates for offline interface regression.

This exercises recorded-response adoption and actual Verus checking. It does
not test a live model's ability to invent the proof.
"""
from __future__ import annotations

import json
from pathlib import Path

from specdet.assistance.workflow import AssistanceSettings, RequestBudget, run_assisted
from specdet.domain.models import Stage
from specdet.providers.errors import ProviderError
from specdet.providers.replay import ReplayProvider
from specdet.storage.artifacts import ArtifactStore

from .provenance import TOOL_ROOT, sha256


REFERENCES = {
    "atmosphere.va-range-new": {
        "fixture_sha256": "4a32dae1ee92b2c586eebf567f5bdd7352f34c0d0453c081880b1820d0906913",
        "candidate": "tests/fixtures/proof_candidates/va_range_new.rs",
        "function": "new",
    },
}


def has_reference(case: dict) -> bool:
    return case["id"] in REFERENCES


def reference_driver(case: dict):
    reference = REFERENCES[case["id"]]
    if case["fixture_sha256"] != reference["fixture_sha256"]:
        raise ValueError("Offline proof candidate does not match the sealed source fixture")
    path = TOOL_ROOT / reference["candidate"]
    proof = path.read_text(encoding="utf-8")
    proof_digest = sha256(proof.encode())
    settings = AssistanceSettings(
        mode="replay",
        excluded_stages=tuple(stage.value for stage in Stage if stage != Stage.PROOF_GENERATION),
        max_rounds_per_stage=1, max_requests_per_target=1, max_requests_per_run=1,
    )
    budget = RequestBudget(settings)

    def drive(session):
        response_root = session.run_dir / "reference-responses"

        class FixedResponse:
            mode = "replay"

            def generate(self, request):
                target = request.context.get("target", {})
                if (
                    request.stage != Stage.PROOF_GENERATION
                    or request.source_digest != reference["fixture_sha256"]
                    or target.get("name") != reference["function"]
                    or request.context.get("analysis_kind") != "concrete_determinism"
                ):
                    raise ProviderError("replay_miss", "No fixed reference candidate matches this request")
                response = {
                    "schema_version": 1, "request_id": request.id,
                    "stage": request.stage.value, "language": request.language,
                    "kind": "proof", "base_source_digest": request.source_digest,
                    "base_problem_id": request.problem_id,
                    "payload": {"proof": proof, "helpers": ""},
                    "origin": "llm",
                }
                # Only the envelope is instantiated; candidate statements never change.
                ArtifactStore(response_root).write_json(f"{request.id}/response.json", {
                    "request_id": request.id, "text": json.dumps(response),
                    "provider": "offline-reference-fixture", "model": "",
                    "metadata": {
                        "candidate_source": reference["candidate"],
                        "candidate_sha256": proof_digest,
                        "live_model_called": False,
                    },
                })
                return ReplayProvider(response_root).generate(request)

        telemetry = run_assisted(session, FixedResponse(), settings, budget=budget)
        if hasattr(session, "report"):
            session.report.assistance.update(telemetry.to_dict())
            session.report.assistance["candidate_origin"] = "offline_reference"
            session.store.artifact("report.json", "analysis_report", session.report)

    return drive

