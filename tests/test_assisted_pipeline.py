from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from specdet.analysis.pipeline import AnalysisSession
from specdet.assistance.workflow import AssistanceSettings, run_assisted
from specdet.config import Config
from specdet.domain.models import SolverStatus, TargetRef, Verdict
from specdet.domain.proposals import RawResponse
from specdet.providers.replay import ReplayProvider
from specdet.storage.workspace import PreparedProject
from test_pipeline import FakeBackend


class ProofFixtureProvider:
    mode = "replay"

    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        text = json.dumps({
            "schema_version": 1, "request_id": request.id,
            "stage": request.stage.value, "language": request.language,
            "kind": "proof", "base_source_digest": request.source_digest,
            "base_problem_id": request.problem_id,
            "payload": {"proof": "checked hint", "helpers": ""},
            "origin": "llm",
        })
        return RawResponse(request.id, text, "offline-fixture")


class AssistedPipelineTests(unittest.TestCase):
    def test_real_session_controller_and_recorded_replay_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            config = Config(project_root=source, output_dir=root / "out", language="test")
            project = PreparedProject(source, source, "snapshot", {})
            target = TargetRef("test", "f.formal", "f", 1, source_digest="source")
            first_backend = FakeBackend()
            first = AnalysisSession(first_backend, config, project, target, root / "first", "first")
            provider = ProofFixtureProvider()
            telemetry = run_assisted(first, provider, AssistanceSettings(mode="replay"))
            self.assertEqual(telemetry.adopted, 1)
            self.assertEqual(first.report.verdict, Verdict.DETERMINISTIC)
            self.assertEqual(first.report.baseline.status, SolverStatus.UNKNOWN)
            self.assertEqual(len(first_backend.checked), 2)

            second_backend = FakeBackend()
            second = AnalysisSession(second_backend, config, project, target, root / "second", "second")
            replay = ReplayProvider(root / "first" / "generation")
            replayed = run_assisted(second, replay, AssistanceSettings(mode="replay"))
            self.assertEqual(replayed.adopted, 1)
            self.assertEqual(replayed.provider_errors, 0)
            self.assertEqual(second.report.verdict, Verdict.DETERMINISTIC)
            self.assertEqual(second.report.baseline.status, SolverStatus.UNKNOWN)
            self.assertEqual(len(provider.requests), 1)


if __name__ == "__main__":
    unittest.main()

