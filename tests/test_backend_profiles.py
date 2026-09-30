from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from specdet.adapters.verus.backend import VerusBackend
from specdet.adapters.verus.execution import ProcessResult, VerusExecutor
from specdet.config import BuildConfig, Config, Limits, ToolchainConfig
from specdet.domain.models import Stage, StageError, canonical_json, digest, text_digest
from specdet.storage.workspace import PreparedProject


class ProfileExecutor:
    def __init__(self):
        self.calls = []

    def verify(self, source, project_root, artifact_dir, function, module="", *, all_functions=False):
        self.calls.append((source, project_root, function, module))
        (artifact_dir / "logs").mkdir(parents=True)
        return ProcessResult(("configured-verifier",), 0, "1 verified, 0 errors", "", 1)


class BackendProfileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / f"backend-profiles-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def prepare(self, sources, build):
        root = self.root / "project"
        root.mkdir()
        for relative, source in sources.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        files = {name: text_digest(source) for name, source in sources.items()}
        project = PreparedProject(root, root, digest(files), files)
        config = Config(root, self.root / "out", build=build)
        return VerusBackend(config), project

    def test_cargo_uses_manifest_crate_name_module_and_default_features(self):
        backend, project = self.prepare({
            "Cargo.toml": (
                '[package]\nname="example-package"\nversion="0.1.0"\n'
                '[lib]\nname="actual_library"\n'
                '[features]\ndefault=["enabled"]\nenabled=[]\n'
            ),
            "src/lib.rs": "pub mod model;\n",
            "src/model.rs": (
                'use vstd::prelude::*;\nverus! {\n#[cfg(feature="enabled")]\n'
                "pub fn f(x:bool)->(r:bool) ensures r==x, {x}\n}\n"
            ),
        }, BuildConfig(
            adapter="verus.cargo", package="example-package",
            injection_file="src/model.rs", extra_args=("--lib",),
        ))
        executor = ProfileExecutor()
        backend._executor = executor
        target = backend.discover(project)[0][0]
        self.assertEqual(target.module, "model")
        contract = backend.extract(project, target)
        self.assertEqual(set(contract.native["active_features"]), {"default", "enabled"})
        obligation = backend.lower(project, contract, backend.observations(project, contract))
        candidate = backend.generate_proof(project, contract, obligation, "baseline")
        checked = backend.check(project, obligation, candidate, self.root / "check", baseline=True)
        self.assertEqual(checked.status, "verified")
        self.assertEqual(checked.native["crate"], "actual_library")
        self.assertTrue(checked.native["crate_entrypoint"].endswith("src/lib.rs"))
        self.assertTrue(str(executor.calls[0][0]).endswith("src/model.rs"))
        self.assertEqual(executor.calls[0][3], "model")
        self.assertEqual(project.files["src/model.rs"], text_digest(project.source_path("src/model.rs").read_text()))

    def test_cargo_workspace_resolves_only_explicit_package(self):
        backend, project = self.prepare({
            "Cargo.toml": '[workspace]\nmembers=["crates/engine"]\n',
            "crates/engine/Cargo.toml": '[package]\nname="engine"\nversion="0.1.0"\n',
            "crates/engine/src/lib.rs": "pub mod module;\n",
            "crates/engine/src/module.rs": "verus! { pub fn f()->(r:u8) ensures r==1, {1} }\n",
        }, BuildConfig(
            adapter="verus.cargo", package="engine", injection_file="crates/engine/src/module.rs",
        ))
        context = backend._cargo_context(project)
        self.assertEqual(context["entrypoint"], "crates/engine/src/lib.rs")
        targets, diagnostics = backend.discover(project)
        self.assertEqual([target.name for target in targets], ["f"])
        self.assertEqual(targets[0].module, "module")
        self.assertFalse(diagnostics)

    def test_ambiguous_cargo_roots_need_explicit_target_selection(self):
        backend, _ = self.prepare({
            "Cargo.toml": '[package]\nname="several"\nversion="0.1.0"\n',
            "src/lib.rs": "",
            "src/main.rs": "fn main() {}",
        }, BuildConfig(adapter="verus.cargo", injection_file="src/lib.rs"))
        with self.assertRaises(StageError) as error:
            backend.toolchain_identity()
        self.assertEqual(error.exception.diagnostic.code, "profile_gap")
        self.assertIsNone(backend._executor)

    def test_native_feature_config_matches_actual_executor_flags(self):
        backend, project = self.prepare({
            "source.rs": (
                'verus! { #[cfg(feature="automatic")] pub fn f()->(r:u8) ensures r==0, {0} }\n'
            ),
        }, BuildConfig(features=("automatic",), extra_args=("--cfg", 'feature="manual"')))
        self.assertEqual(backend.discover(project)[0][0].name, "f")
        with patch("specdet.adapters.verus.backend.VerusExecutor") as factory:
            self.assertIsNone(backend._executor)
            backend.executor
        configured = factory.call_args.args[0]
        self.assertIn('feature="automatic"', configured.build.extra_args)
        self.assertEqual(configured.build.extra_args.count('feature="manual"'), 1)
        self.assertEqual(backend.config.build.extra_args, ("--cfg", 'feature="manual"'))

    def test_generation_context_never_includes_toolchain_environment(self):
        source = self.root / "project"
        source.mkdir()
        (source / "source.rs").write_text("fn main() {}")
        backend = VerusBackend(Config(
            source, self.root / "out",
            toolchain=ToolchainConfig(environment={"API_TOKEN": "SECRET_FOR_TEST"}),
        ))
        for stage in Stage:
            context = backend.generation_context(stage, None, None, None, None, ())
            self.assertNotIn("SECRET_FOR_TEST", canonical_json(context))
            self.assertNotIn("API_TOKEN", canonical_json(context))
            self.assertIn("source.rs", context["project_files"])

    def test_cargo_focused_verification_uses_focus_subcommand(self):
        source = self.root / "project"
        source.mkdir()
        target = source / "source.rs"
        target.write_text("fn main() {}")
        verifier = self.root / "verus"
        verifier.write_text("#!/bin/sh\n")
        verifier.chmod(0o755)
        executor = VerusExecutor(Config(
            source,
            self.root / "out",
            build=BuildConfig(adapter="verus.cargo", package="example"),
            toolchain=ToolchainConfig(executable=str(verifier)),
        ))
        result = ProcessResult(("cargo",), 0, "1 verified, 0 errors", "", 1)
        with patch(
            "specdet.adapters.verus.execution.run_process", return_value=result
        ) as run:
            executor.verify(
                target, source, self.root / "focused", "target", module="module"
            )
            self.assertEqual(run.call_args.args[0][:3], ["cargo", "verus", "focus"])
            executor.verify(
                target, source, self.root / "whole", "target", all_functions=True
            )
            self.assertEqual(run.call_args.args[0][:3], ["cargo", "verus", "verify"])

    def test_solver_seed_is_checked_before_resolving_a_toolchain(self):
        source = self.root / "project"
        source.mkdir()
        for seed in (-1, 2**32, True):
            backend = VerusBackend(Config(source, self.root / "out", limits=Limits(seed=seed)))
            with self.assertRaises(StageError) as error:
                backend.toolchain_identity()
            self.assertEqual(error.exception.diagnostic.code, "invalid_solver_seed")
            self.assertIsNone(backend._executor)


if __name__ == "__main__":
    unittest.main()
