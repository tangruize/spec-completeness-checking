from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from specdet.assistance import AssistanceSettings, build_prompt, parse_proposal
from specdet.assistance.records import atomic_json, write_record
from specdet.domain.models import Stage, as_object, canonical_json
from specdet.domain.proposals import GenerationRequest, Proposal, RawResponse
from specdet.providers import (
    CopilotProvider, ProviderError, ReplayProvider, TextSubprocessProvider, create_provider,
)
from specdet.providers.copilot import REQUIRED_FLAGS


class ProviderCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / f".provider-work-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.request = GenerationRequest(
            stage=Stage.PROOF_GENERATION, language="verus", allowed_kinds=("proof",),
            source_digest="source-digest", problem_id="problem-id",
            context={"source": "fn frozen() {}", "obligation_digest": "obligation-digest"},
            target_id="target-id",
        )
        self.proposal = Proposal(
            request_id=self.request.id, stage=self.request.stage, language=self.request.language,
            kind="proof", base_source_digest=self.request.source_digest,
            base_problem_id=self.request.problem_id, payload={"proof": "assert(true);"},
        )
        self.raw = RawResponse(
            self.request.id, canonical_json(self.proposal), "copilot", "recorded-model",
            {"transport_invocations": 1},
        )

    def process(self, stdout="", stderr="", returncode=0):
        process = MagicMock()
        process.__enter__.return_value = process
        process.__exit__.return_value = False
        process.communicate.return_value = (stdout, stderr)
        process.returncode = returncode
        process.pid = 999999
        return process

    @property
    def help_text(self):
        return "\n".join(f"  {flag}  documented option" for flag in REQUIRED_FLAGS)


class ReplayTests(ProviderCase):
    def test_captured_envelope_is_directly_replayable(self):
        directory = self.root / "generation"
        path = directory / self.request.id / "response.json"
        write_record(path, "raw_response", self.raw)
        response = ReplayProvider(directory).generate(self.request)
        self.assertEqual(response.request_id, self.request.id)
        self.assertEqual(response.text, self.raw.text)
        self.assertEqual(response.provider, "replay")
        self.assertEqual(response.model, "recorded-model")
        self.assertEqual(response.metadata["recorded_provider"], "copilot")
        self.assertEqual(parse_proposal(self.request, response), self.proposal)

    def test_flat_minimal_fixture_format(self):
        path = self.root / f"{self.request.id}.json"
        atomic_json(path, {"request_id": self.request.id, "text": self.raw.text})
        response = ReplayProvider(self.root).generate(self.request)
        self.assertEqual(response.text, self.raw.text)
        self.assertEqual(response.provider, "replay")

    def test_raw_field_format_also_works_in_nested_response_file(self):
        path = self.root / self.request.id / "response.json"
        atomic_json(path, {"schema_version": 1, **as_object(self.raw)})
        response = ReplayProvider(self.root).generate(self.request)
        self.assertEqual(response.request_id, self.request.id)

    def test_nested_record_is_preferred_without_source_hash_lookup(self):
        atomic_json(self.root / f"{self.request.id}.json", {
            "request_id": self.request.id, "text": "flat response",
        })
        write_record(self.root / self.request.id / "response.json", "raw_response", self.raw)
        provider = ReplayProvider(self.root)
        self.assertEqual(provider.generate(self.request).text, self.raw.text)
        changed = replace(self.request, context={"source": "different context"})
        self.assertEqual(changed.source_digest, self.request.source_digest)
        with self.assertRaises(ProviderError) as raised:
            provider.generate(changed)
        self.assertEqual(raised.exception.code, "replay_miss")
        self.assertFalse(raised.exception.retryable)

    def test_existing_invalid_nested_record_never_uses_flat_fallback(self):
        nested = self.root / self.request.id / "response.json"
        nested.parent.mkdir()
        nested.write_text("not JSON")
        atomic_json(self.root / f"{self.request.id}.json", as_object(self.raw))
        with self.assertRaises(ProviderError) as raised:
            ReplayProvider(self.root).generate(self.request)
        self.assertEqual(raised.exception.code, "replay_invalid")

    def test_replay_miss_never_invokes_a_subprocess(self):
        with patch("specdet.providers._transport.subprocess.Popen") as popen:
            with self.assertRaises(ProviderError) as raised:
                ReplayProvider(self.root).generate(self.request)
        self.assertEqual(raised.exception.code, "replay_miss")
        popen.assert_not_called()

    def test_recorded_transport_failure_is_a_typed_error_not_a_candidate(self):
        write_record(
            self.root / self.request.id / "response.json", "provider_error",
            {
                "request_id": self.request.id, "code": "provider_timeout",
                "message": "recorded timeout", "retryable": True,
                "metadata": {"stdout": "partial", "stderr": ""},
            },
        )
        with patch("specdet.providers._transport.subprocess.Popen") as popen:
            with self.assertRaises(ProviderError) as raised:
                ReplayProvider(self.root).generate(self.request)
        self.assertEqual(raised.exception.code, "provider_timeout")
        self.assertEqual(raised.exception.message, "recorded timeout")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.metadata["recorded_metadata"]["stdout"], "partial")
        popen.assert_not_called()

    def test_identity_schema_and_raw_field_types_are_checked(self):
        cases = (
            {"request_id": "wrong", "text": self.raw.text},
            {"schema_version": 2, **as_object(self.raw)},
            {"schema_version": True, **as_object(self.raw)},
            {"request_id": self.request.id, "text": 123},
            {"request_id": self.request.id, "text": self.raw.text, "metadata": []},
            {"request_id": self.request.id, "text": self.raw.text, "origin": "trusted"},
            {"schema_version": 1, "record_type": "proposal", "data": as_object(self.raw)},
            {"schema_version": 1, "record_type": "raw_response", "data": []},
            {"schema_version": 1, "record_type": "raw_response", "data": as_object(self.raw), "extra": 1},
        )
        for index, data in enumerate(cases):
            directory = self.root / str(index)
            atomic_json(directory / f"{self.request.id}.json", data)
            with self.subTest(index=index), self.assertRaises(ProviderError) as raised:
                ReplayProvider(directory).generate(self.request)
            self.assertIn(raised.exception.code, {"replay_invalid", "replay_request_mismatch"})

    def test_duplicate_json_keys_and_non_finite_record_metadata_are_rejected(self):
        path = self.root / f"{self.request.id}.json"
        for text in (
            '{"request_id":"a","request_id":"b","text":""}',
            '{"request_id":"a","text":"","metadata":{"tokens":NaN}}',
        ):
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ProviderError) as raised:
                ReplayProvider(self.root).generate(self.request)
            self.assertEqual(raised.exception.code, "replay_invalid")

    def test_pinned_records_cannot_escape_via_a_symlink(self):
        directory = self.root / "pinned"
        record = directory / self.request.id / "response.json"
        record.parent.mkdir(parents=True)
        outside = self.root / "outside.json"
        atomic_json(outside, as_object(self.raw))
        record.symlink_to(outside)
        with self.assertRaises(ProviderError) as raised:
            ReplayProvider(directory).generate(self.request)
        self.assertEqual(raised.exception.code, "replay_path_escape")


class ProviderConstructionTests(ProviderCase):
    def test_off_and_replay_do_not_initialize_live_providers(self):
        with patch("specdet.providers.copilot.CopilotProvider") as copilot:
            with patch("specdet.providers.subprocess.TextSubprocessProvider") as text:
                self.assertIsNone(create_provider(AssistanceSettings(), run_dir=self.root))
                replay = create_provider(
                    AssistanceSettings(mode="replay", responses=str(self.root)),
                    run_dir=self.root,
                )
                self.assertIsInstance(replay, ReplayProvider)
        copilot.assert_not_called()
        text.assert_not_called()

    def test_live_provider_initialization_is_lazy_and_timeout_is_configured(self):
        with patch("specdet.providers._transport.subprocess.Popen") as popen:
            provider = create_provider(
                AssistanceSettings(mode="live", model="chosen", request_timeout_seconds=7),
                run_dir=self.root,
            )
        self.assertIsInstance(provider, CopilotProvider)
        self.assertEqual(provider.model, "chosen")
        self.assertEqual(provider.timeout_seconds, 7)
        self.assertEqual(provider.work_root, self.root / "provider-work")
        popen.assert_not_called()
        self.assertFalse((self.root / "provider-work").exists())

    def test_invalid_provider_config_fails_instead_of_defaulting_to_live(self):
        for settings in (
            AssistanceSettings(mode="replay"),
            AssistanceSettings(mode="live", provider="not-a-provider"),
            AssistanceSettings(mode="live", provider="subprocess", command=("text-client",)),
        ):
            with self.subTest(settings=settings), self.assertRaises(ProviderError):
                create_provider(settings, run_dir=self.root)

    def test_text_only_subprocess_can_be_selected_explicitly(self):
        settings = AssistanceSettings(
            mode="live", provider="subprocess", text_only=True,
            command=("text-client", "--json"), request_timeout_seconds=11,
        )
        provider = create_provider(settings, run_dir=self.root)
        self.assertIsInstance(provider, TextSubprocessProvider)
        self.assertEqual(provider.command, ("text-client", "--json"))
        self.assertEqual(provider.timeout_seconds, 11)


class CopilotIsolationTests(ProviderCase):
    def test_only_prompt_is_supplied_and_tools_hooks_and_context_are_disabled(self):
        calls = []
        home_paths = []

        def launch(argv, **kwargs):
            calls.append((argv, kwargs))
            cwd = kwargs["cwd"]
            self.assertTrue(cwd.is_dir())
            self.assertTrue(cwd.is_relative_to(self.root / "work"))
            self.assertNotEqual(cwd, self.root)
            env = kwargs["env"]
            home_paths.append(Path(env["HOME"]))
            self.assertTrue(Path(env["HOME"]).is_relative_to(cwd))
            self.assertEqual(env["COPILOT_ALLOW_ALL"], "false")
            self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "test-only-token")
            self.assertEqual(env["TMPDIR"], str(cwd))
            for key in ("UNRELATED_SECRET", "NODE_OPTIONS", "BASH_ENV", "COPILOT_CUSTOM_INSTRUCTIONS_DIRS"):
                self.assertNotIn(key, env)
            config = json.loads((Path(env["COPILOT_HOME"]) / "config.json").read_text())
            self.assertTrue(config["disableAllHooks"])
            self.assertFalse(config["ide"]["autoConnect"])
            self.assertFalse(kwargs["shell"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            return self.process(self.help_text if "--help" in argv else self.raw.text)

        inherited = {
            "COPILOT_ALLOW_ALL": "true",
            "COPILOT_GITHUB_TOKEN": "test-only-token",
            "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "/unrelated-instructions",
            "UNRELATED_SECRET": "must-not-inherit",
            "NODE_OPTIONS": "--require untrusted-module",
            "BASH_ENV": "/unrelated-shell-init",
        }
        provider = CopilotProvider(
            executable="copilot", model="chosen-model", timeout_seconds=9,
            work_root=self.root / "work",
        )
        with patch.dict(os.environ, inherited):
            with patch("specdet.providers._transport.subprocess.Popen", side_effect=launch):
                response = provider.generate(self.request)
        self.assertEqual(response.text, self.raw.text)
        self.assertEqual(response.provider, "copilot")
        self.assertTrue(response.metadata["tools_disabled"])
        self.assertEqual(len(calls), 2)
        argv = calls[1][0]
        self.assertEqual(argv[argv.index("--available-tools") + 1], "")
        self.assertEqual(argv[argv.index("--prompt") + 1], build_prompt(self.request))
        self.assertEqual(argv[argv.index("--model") + 1], "chosen-model")
        self.assertEqual(argv[argv.index("--output-format") + 1], "text")
        for forbidden in ("--allow-all", "--allow-all-tools", "--allow-all-paths", "--yolo"):
            self.assertNotIn(forbidden, argv)
        for flag in REQUIRED_FLAGS:
            self.assertIn(flag, argv)
        self.assertFalse(any(path.exists() for path in home_paths))
        self.assertEqual(list((self.root / "work").iterdir()), [])

    def test_every_missing_isolation_flag_prevents_generation(self):
        for omitted in REQUIRED_FLAGS:
            provider = CopilotProvider(work_root=self.root / omitted)
            help_text = " ".join(flag for flag in REQUIRED_FLAGS if flag != omitted)
            with self.subTest(flag=omitted):
                with patch(
                    "specdet.providers._transport.subprocess.Popen",
                    return_value=self.process(help_text),
                ) as popen:
                    with self.assertRaises(ProviderError) as raised:
                        provider.generate(self.request)
                self.assertEqual(raised.exception.code, "tools_not_disabled")
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(popen.call_args.args[0], ["copilot", "--help"])

    def test_help_capabilities_are_checked_once_but_generation_is_not_retried(self):
        processes = [
            self.process(self.help_text), self.process(self.raw.text), self.process(self.raw.text),
        ]
        provider = CopilotProvider(work_root=self.root)
        with patch(
            "specdet.providers._transport.subprocess.Popen", side_effect=processes,
        ) as popen:
            provider.generate(self.request)
            provider.generate(self.request)
        self.assertEqual(popen.call_count, 3)
        generation = [call for call in popen.call_args_list if "--prompt" in call.args[0]]
        self.assertEqual(len(generation), 2)

    def test_missing_executable_and_permissions_are_typed_errors(self):
        for error, code in (
            (FileNotFoundError("missing"), "executable_not_found"),
            (PermissionError("denied"), "provider_permission_denied"),
            (OSError("bad executable"), "provider_transport_error"),
        ):
            with self.subTest(code=code):
                provider = CopilotProvider(work_root=self.root / code)
                with patch("specdet.providers._transport.subprocess.Popen", side_effect=error) as popen:
                    with self.assertRaises(ProviderError) as raised:
                        provider.generate(self.request)
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(list(provider.work_root.iterdir()), [])

    def test_nonzero_exit_preserves_raw_diagnostics_and_never_retries(self):
        processes = [
            self.process(self.help_text), self.process("partial response", "transport failed", 7),
        ]
        provider = CopilotProvider(work_root=self.root)
        with patch("specdet.providers._transport.subprocess.Popen", side_effect=processes) as popen:
            with self.assertRaises(ProviderError) as raised:
                provider.generate(self.request)
        self.assertEqual(raised.exception.code, "provider_exit")
        self.assertEqual(raised.exception.metadata["returncode"], 7)
        self.assertEqual(raised.exception.metadata["stdout"], "partial response")
        self.assertEqual(raised.exception.metadata["stderr"], "transport failed")
        self.assertEqual(popen.call_count, 2)

    def test_timeout_kills_only_its_owned_process_group_and_is_visible_to_controller(self):
        timed_out = self.process()
        timed_out.communicate.side_effect = [
            subprocess.TimeoutExpired(["copilot"], 1, output="partial"),
            ("partial", "timeout details"),
        ]
        provider = CopilotProvider(work_root=self.root, timeout_seconds=1)
        with patch(
            "specdet.providers._transport.subprocess.Popen",
            side_effect=[self.process(self.help_text), timed_out],
        ) as popen:
            with patch("specdet.providers._transport.os.killpg") as killpg:
                with self.assertRaises(ProviderError) as raised:
                    provider.generate(self.request)
        self.assertEqual(raised.exception.code, "provider_timeout")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.metadata["stdout"], "partial")
        self.assertEqual(popen.call_count, 2)
        if os.name == "posix":
            killpg.assert_called_once_with(timed_out.pid, signal.SIGKILL)
        timeout = timed_out.communicate.call_args_list[0].kwargs["timeout"]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 1)
        self.assertFalse(list(self.root.iterdir()))

    def test_unsafe_environment_overrides_are_refused(self):
        for environment in (
            {"COPILOT_ALLOW_ALL": "true"}, {"HOME": "/elsewhere"},
            {"COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "/elsewhere"},
            {"COPILOT_PROVIDER_BASE_URL": "https://unselected-provider.invalid"},
            {"NODE_OPTIONS": "--require external"},
        ):
            with self.subTest(environment=environment), self.assertRaises(ProviderError):
                CopilotProvider(work_root=self.root, environment=environment)


class TextSubprocessTests(ProviderCase):
    def test_text_client_uses_stdin_argv_and_isolated_workspace(self):
        process = self.process(self.raw.text)
        provider = TextSubprocessProvider(
            ("text-client", "--format", "json"), text_only=True,
            model="client-model", work_root=self.root / "work",
            timeout_seconds=5, environment={"TEXT_CLIENT_TOKEN": "test-token"},
        )
        with patch("specdet.providers._transport.subprocess.Popen", return_value=process) as popen:
            response = provider.generate(self.request)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["text-client", "--format", "json"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(kwargs["env"]["TEXT_CLIENT_TOKEN"], "test-token")
        self.assertTrue(kwargs["cwd"].is_relative_to(self.root / "work"))
        self.assertFalse(kwargs["cwd"].exists())
        process.communicate.assert_called_once()
        self.assertEqual(process.communicate.call_args.kwargs["input"], build_prompt(self.request))
        self.assertGreater(process.communicate.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(process.communicate.call_args.kwargs["timeout"], 5)
        self.assertEqual(response.provider, "subprocess")
        self.assertEqual(response.model, "client-model")
        self.assertTrue(response.metadata["text_only_contract"])
        self.assertEqual(parse_proposal(self.request, response), self.proposal)

    def test_arbitrary_commands_require_text_only_attestation(self):
        with self.assertRaises(ProviderError) as raised:
            TextSubprocessProvider(("some-command",), work_root=self.root)
        self.assertEqual(raised.exception.code, "text_only_required")
        with self.assertRaises(ValueError):
            TextSubprocessProvider("text-client --json", text_only=True, work_root=self.root)

    def test_copilot_and_unrestricted_agent_flags_cannot_bypass_copilot_adapter(self):
        for command in (
            ("copilot",), ("/bin/copilot",),
            ("agent", "--allow-all-tools"), ("agent", "--allow-all-paths"),
            ("agent", "--allow-all"), ("agent", "--yolo"),
        ):
            with self.subTest(command=command), self.assertRaises(ProviderError):
                TextSubprocessProvider(command, text_only=True, work_root=self.root)

    def test_timeout_and_output_limits_have_no_hidden_retry(self):
        provider = TextSubprocessProvider(("text-client",), text_only=True, work_root=self.root)
        with patch("specdet.providers._transport.MAX_RESPONSE_BYTES", 5):
            with patch(
                "specdet.providers._transport.subprocess.Popen",
                return_value=self.process("too much output"),
            ) as popen:
                with self.assertRaises(ProviderError) as raised:
                    provider.generate(self.request)
        self.assertEqual(raised.exception.code, "response_too_large")
        self.assertEqual(popen.call_count, 1)

    def test_nonpositive_timeouts_are_rejected_without_processes(self):
        for timeout in (0, -1, True, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                TextSubprocessProvider(
                    ("text-client",), text_only=True, work_root=self.root,
                    timeout_seconds=timeout,
                )


if __name__ == "__main__":
    unittest.main()
