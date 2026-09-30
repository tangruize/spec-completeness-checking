from __future__ import annotations

import json
import fnmatch
import os
import re
import shutil
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .native.view.registry import ViewRegistry

from specdet.config import Config
from specdet.domain.models import (
    CheckEvidence, Contract, Diagnostic, JsonObject, Obligation, ObservationPlan,
    ProofCandidate, ProofCheckEvidence, SearchResult, SolverStatus, Stage,
    StageError, TargetRef, as_object, canonical_json, digest, text_digest,
)
from specdet.domain.proposals import GenerationRequest, Proposal, ValidationRecord
from specdet.storage.artifacts import ArtifactStore, file_digest
from specdet.storage.workspace import PreparedProject, input_files

from .discovery import (
    FunctionLocation, discover_project, parser, project_modules, scan_source,
    source_for_features,
)
from .execution import VerusExecutor, classify_process
from .proposal_validation import (
    KINDS, checked_edits, checked_profile, proof_structure, relative_path,
)


def _copy(value):
    return json.loads(canonical_json(value))


def _fail(stage: Stage, code: str, message: str, *, recoverable: bool = False,
          details: JsonObject | None = None):
    raise StageError(Diagnostic(stage, code, message, details=details or {}), recoverable=recoverable)


def _object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


class VerusBackend:
    def __init__(self, config: Config):
        self.config = config
        self._request_config_digest = digest(config.to_dict())
        self._executor: VerusExecutor | None = None
        self._identity: JsonObject | None = None
        self._overlays: dict[str, dict[str, str]] = {}
        self._transformations: dict[str, list[JsonObject]] = {}
        self._untrusted_snapshots: set[str] = set()
        self._locations: dict[tuple[str, str, str], list[FunctionLocation]] = {}
        self._target_selections: dict[str, tuple[TargetRef, ...]] = {}
        self._target_aliases: dict[str, str] = {}
        self._effective_targets: dict[str, TargetRef] = {}
        self._scoped_targets: dict[tuple[str, str], TargetRef] = {}
        self._accepted: dict[tuple[str, str], JsonObject] = {}
        self._validated: dict[str, ValidationRecord] = {}
        self._validation_bases: dict[str, JsonObject] = {}
        self._contexts: dict[tuple[str, str], object] = {}
        self._baselines: dict[tuple[str, str], CheckEvidence] = {}
        self._executed_plans: set[tuple[tuple[str, str], str]] = set()
        self._frozen: dict[str, str] = {}
        self._known_contracts: set[str] = set()
        self._profile_trace: list[JsonObject] = []
        self._resolved_types: dict[tuple[str, bool], JsonObject] = {}
        self._view_registries: dict[tuple[str, bool, str], ViewRegistry] = {}

    @property
    def executor(self) -> VerusExecutor:
        if self._executor is None:
            try:
                configured = self.config
                if configured.build.adapter != "verus.cargo" and configured.build.features:
                    arguments = list(configured.build.extra_args)
                    present = self._flag_features(configured)
                    for feature in configured.build.features:
                        if feature not in present:
                            arguments.extend(["--cfg", "feature=" + json.dumps(feature, ensure_ascii=False)])
                    configured = replace(configured, build=replace(configured.build, extra_args=tuple(arguments)))
                self._executor = VerusExecutor(configured)
            except (FileNotFoundError, RuntimeError, OSError) as error:
                _fail(Stage.CONFIG, "toolchain_error", str(error), recoverable=True)
        return self._executor

    def toolchain_identity(self) -> JsonObject:
        self._solver_options()
        build = self.config.build
        if build.adapter not in {"verus.single_file", "verus.native", "verus.cargo"}:
            _fail(Stage.CONFIG, "profile_gap", f"Unsupported build adapter: {build.adapter}", recoverable=True)
        if build.adapter == "verus.native" and not build.entrypoint:
            _fail(Stage.CONFIG, "profile_gap", "Native verification requires an explicit crate entrypoint", recoverable=True)
        if build.adapter == "verus.cargo" and not build.injection_file:
            _fail(Stage.CONFIG, "profile_gap", "Cargo verification requires an explicit injection_file", recoverable=True)
        if build.adapter == "verus.cargo":
            self._cargo_context(None, stage=Stage.CONFIG)
        if self._identity is None:
            self._identity = _copy(self.executor.identity())
        return _copy(self._identity)

    def _solver_options(self) -> tuple[int, int]:
        from specdet.runtime import bounded_timeout_ms

        limits = self.config.limits
        if type(limits.solver_timeout_ms) is not int or limits.solver_timeout_ms <= 0:
            _fail(Stage.CONFIG, "invalid_solver_timeout", "The Verus/Z3 solver timeout must be a positive integer")
        if type(limits.seed) is not int or not 0 <= limits.seed < 2**32:
            _fail(Stage.CONFIG, "invalid_solver_seed", "The Verus/Z3 seed must be an unsigned 32-bit integer")
        return bounded_timeout_ms(limits.solver_timeout_ms), limits.seed

    def _effective_overlays(self, project: PreparedProject, target: TargetRef | None = None) -> dict[str, str]:
        result = dict(self._overlays.get(project.snapshot_digest, {}))
        if target is not None:
            result.update(self._overlays.get(self._target_key(project, target), {}))
        return result

    @staticmethod
    def _check_project(project: PreparedProject) -> None:
        if digest(project.files) != project.snapshot_digest:
            _fail(Stage.PREPARE, "snapshot_modified", "Prepared source manifest no longer matches its snapshot digest")

    def _source(self, project: PreparedProject, relative: str, target: TargetRef | None = None) -> str:
        if relative not in project.files:
            _fail(Stage.PREPARE, "missing_source", f"Source is not in the prepared snapshot: {relative}")
        path = project.source_path(relative)
        if path.is_symlink() or file_digest(path) != project.files[relative]:
            _fail(Stage.PREPARE, "snapshot_modified", f"Prepared source changed: {relative}")
        overlays = self._effective_overlays(project, target)
        if relative in overlays:
            return overlays[relative]
        return path.read_text(encoding="utf-8")

    def _target_key(self, project: PreparedProject | None, target: TargetRef | None) -> str:
        if target is None:
            return project.snapshot_digest if project is not None else "configuration"
        if target.id in self._target_aliases:
            return self._target_aliases[target.id]
        return digest({
            "snapshot": project.snapshot_digest if project is not None else "",
            "language": target.language, "file": target.file, "name": target.name,
            "line": target.line, "qualified_name": target.qualified_name,
        })

    def _semantic_config(self, analysis_kind: str | None = None) -> JsonObject:
        return {
            "build": as_object(self.config.build),
            "type_sources": list(self.config.type_sources),
            "observation_policy": self.config.observation_policy,
            "analysis_kind": analysis_kind or self.config.analysis_kind,
            "max_container_depth": self.config.limits.max_container_depth,
            "verus_args": list(self.config.toolchain.extra_args),
        }

    @staticmethod
    def _flag_features(config: Config) -> set[str]:
        arguments = (*config.toolchain.extra_args, *config.build.extra_args)
        result: set[str] = set()
        for index, argument in enumerate(arguments):
            value = argument.removeprefix("--cfg=") if argument.startswith("--cfg=") else (
                arguments[index + 1] if argument == "--cfg" and index + 1 < len(arguments) else ""
            )
            match = re.fullmatch(r'feature\s*=\s*("(?:[^"\\]|\\.)*")', value)
            if match:
                try:
                    result.add(json.loads(match[1]))
                except ValueError:
                    _fail(Stage.CONFIG, "profile_gap", "Feature cfg value is not a supported string literal", recoverable=True)
        return result

    def _cargo_context(self, project: PreparedProject | None, target: TargetRef | None = None,
                       *, stage: Stage = Stage.DISCOVER, config: Config | None = None) -> JsonObject:
        config = config or self.config
        files = set(project.files) if project is not None else self._profile_files()
        manifests = []
        for relative in sorted(name for name in files if Path(name).name == "Cargo.toml"):
            try:
                source = self._source(project, relative, target) if project is not None else (
                    config.project_root / relative
                ).read_text(encoding="utf-8")
                manifest = tomllib.loads(source)
            except (UnicodeError, tomllib.TOMLDecodeError) as error:
                _fail(stage, "profile_gap", f"Cannot parse Cargo manifest {relative}: {error}", recoverable=True)
            package = manifest.get("package", {})
            if isinstance(package, dict) and isinstance(package.get("name"), str):
                manifests.append((relative, manifest, package["name"]))
        if config.build.package:
            manifests = [entry for entry in manifests if entry[2] == config.build.package]
        else:
            manifests = [entry for entry in manifests if entry[0] == "Cargo.toml"]
        if len(manifests) != 1:
            _fail(
                stage, "profile_gap",
                "Cargo verification needs one explicit package in the prepared workspace",
                recoverable=True,
            )
        relative, manifest, package = manifests[0]
        directory = Path(relative).parent
        candidates: list[tuple[str, str, str]] = []
        library = manifest.get("lib", {})
        if not isinstance(library, dict):
            _fail(stage, "profile_gap", "Cargo lib metadata must be a table", recoverable=True)
        library_path = (directory / str(library.get("path", "src/lib.rs"))).as_posix()
        if library_path in files:
            candidates.append((library_path, str(library.get("name", package)), "lib"))
        binaries = manifest.get("bin", [])
        if not isinstance(binaries, list) or any(not isinstance(item, dict) for item in binaries):
            _fail(stage, "profile_gap", "Cargo bin metadata must be an array of tables", recoverable=True)
        for binary in binaries:
            name = binary.get("name")
            if not isinstance(name, str):
                _fail(stage, "profile_gap", "An explicit Cargo binary needs a name", recoverable=True)
            path = binary.get("path")
            if path is None:
                conventional = directory / ("src/main.rs" if name == package else f"src/bin/{name}.rs")
                path = conventional.as_posix()
            else:
                path = (directory / str(path)).as_posix()
            if path in files:
                candidates.append((path, name, "bin"))
        main_path = (directory / "src/main.rs").as_posix()
        if main_path in files and not any(item[0] == main_path for item in candidates):
            candidates.append((main_path, package, "bin"))
        bin_directory = directory / "src/bin"
        for name in sorted(files):
            path = Path(name)
            if path.suffix == ".rs" and path.parent == bin_directory:
                if not any(item[0] == name for item in candidates):
                    candidates.append((name, path.stem, "bin"))
            elif path.name == "main.rs" and path.parent.parent == bin_directory:
                if not any(item[0] == name for item in candidates):
                    candidates.append((name, path.parent.name, "bin"))
        multiple_targets = len(candidates) > 1
        if config.build.entrypoint:
            candidates = [entry for entry in candidates if entry[0] == config.build.entrypoint]
        arguments = config.build.extra_args
        if "--lib" in arguments:
            candidates = [entry for entry in candidates if entry[2] == "lib"]
        selected_bins = []
        for index, argument in enumerate(arguments):
            if argument.startswith("--bin="):
                selected_bins.append(argument.split("=", 1)[1])
            elif argument == "--bin" and index + 1 < len(arguments):
                selected_bins.append(arguments[index + 1])
        if selected_bins:
            candidates = [entry for entry in candidates if entry[2] == "bin" and entry[1] in selected_bins]
        if (
            len(candidates) != 1
            or (multiple_targets and "--lib" not in arguments and not selected_bins)
            or any(argument in {"--all-targets", "--tests", "--examples", "--benches"} for argument in arguments)
        ):
            _fail(
                stage, "profile_gap",
                "Cargo verification needs an unambiguous manifest target; configure entrypoint and --lib/--bin as needed",
                recoverable=True,
            )
        entrypoint, name, kind = candidates[0]
        features = manifest.get("features", {})
        if not isinstance(features, dict) or any(
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
            for value in features.values()
        ):
            _fail(stage, "profile_gap", "Cargo feature definitions must be arrays of feature names", recoverable=True)
        active = set(config.build.features)
        for index, argument in enumerate(arguments):
            if argument.startswith("--features="):
                active.update(re.split(r"[,\s]+", argument.split("=", 1)[1]))
            elif argument == "--features" and index + 1 < len(arguments):
                active.update(re.split(r"[,\s]+", arguments[index + 1]))
        if "--no-default-features" not in arguments and "default" in features:
            active.add("default")
        if "--all-features" in arguments:
            active.update(features)
        pending = list(active)
        while pending:
            feature = pending.pop()
            for dependency in features.get(feature, []):
                if "/" not in dependency and not dependency.startswith("dep:") and dependency not in active:
                    active.add(dependency)
                    pending.append(dependency)
        active.discard("")
        return {
            "package": package, "manifest": relative, "entrypoint": entrypoint,
            "crate": name.replace("-", "_"), "target_kind": kind,
            "active_features": sorted(active),
        }

    def _discovery_config(self, project: PreparedProject, target: TargetRef | None = None) -> Config:
        if self.config.build.adapter != "verus.cargo":
            features = set(self.config.build.features) | self._flag_features(self.config)
            return replace(self.config, build=replace(self.config.build, features=tuple(sorted(features))))
        context = self._cargo_context(project, target)
        return replace(self.config, build=replace(
            self.config.build, entrypoint=str(context["entrypoint"]),
            features=tuple(context["active_features"]),
        ))

    def _build_context(self, project: PreparedProject, target: TargetRef) -> JsonObject:
        if self.config.build.adapter == "verus.cargo":
            return self._cargo_context(project, target, stage=Stage.LOWER)
        entrypoint = self.config.build.entrypoint or target.file
        names = []
        arguments = (*self.config.toolchain.extra_args, *self.config.build.extra_args)
        for index, argument in enumerate(arguments):
            if argument.startswith("--crate-name="):
                names.append(argument.split("=", 1)[1])
            elif argument == "--crate-name" and index + 1 < len(arguments):
                names.append(arguments[index + 1])
        if len(set(names)) > 1:
            _fail(Stage.LOWER, "profile_gap", "Conflicting explicit crate names", recoverable=True)
        crate = names[0] if names else Path(entrypoint).stem.replace("-", "_")
        return {"entrypoint": entrypoint, "crate": crate, "target_kind": "configured_source"}

    def discover(self, project: PreparedProject) -> tuple[list[TargetRef], list[Diagnostic]]:
        locations, diagnostics = self._discover_locations(project)
        return [location.target for location in locations], diagnostics

    def _remember_locations(self, project: PreparedProject, locations: list[FunctionLocation]) -> None:
        for location in locations:
            key = (project.snapshot_digest, location.target.file, location.target.source_digest)
            self._locations.setdefault(key, [])
            if location not in self._locations[key]:
                self._locations[key].append(location)

    def _discover_locations(
        self, project: PreparedProject, *, apply_selection: bool = True,
    ) -> tuple[list[FunctionLocation], list[Diagnostic]]:
        self._check_project(project)
        try:
            config = self._discovery_config(project)
            locations, diagnostics = discover_project(
                config, project, lambda relative: self._source(project, relative),
            )
            self._remember_locations(project, locations)
            files = {
                path.relative_to(project.root).as_posix()
                for path in input_files(config, project)
            }
            scopes = [
                (key, target) for (snapshot, key), target in self._scoped_targets.items()
                if snapshot == project.snapshot_digest
            ]
            for scope, target in scopes:
                if target.file not in files:
                    _fail(Stage.DISCOVER, "target_not_found", "Accepted source edits refer to a target excluded by the current profile")
                scoped_config = self._discovery_config(project, target)
                modules, module_diagnostics = project_modules(
                    scoped_config, project,
                    lambda relative: self._source(project, relative, target),
                )
                if scoped_config.build.adapter != "verus.single_file" and target.file not in modules:
                    _fail(Stage.DISCOVER, "target_not_found", "The edited target is no longer reachable from its crate entrypoint")
                found, scoped_diagnostics = scan_source(
                    self._source(project, target.file, target), target.file,
                    module=modules.get(target.file, ""), visibility=scoped_config.visibility,
                    features=scoped_config.build.features,
                    test="--test" in scoped_config.build.extra_args,
                )
                self._remember_locations(project, found)
                named = [location for location in found if location.target.name == target.name]
                qualified = [
                    location for location in named
                    if location.target.qualified_name == target.qualified_name
                ]
                matches = qualified or named
                if len(matches) != 1:
                    _fail(
                        Stage.DISCOVER, "ambiguous_target" if matches else "target_not_found",
                        "Accepted source edits must retain one uniquely identifiable target",
                        details={"target": as_object(target)},
                    )
                refreshed = matches[0]
                self._target_aliases[refreshed.target.id] = scope
                self._effective_targets[scope] = refreshed.target
                self._scoped_targets[(project.snapshot_digest, scope)] = refreshed.target
                replaced = False
                for index, original in enumerate(locations):
                    if self._target_key(project, original.target) == scope:
                        locations[index] = refreshed
                        replaced = True
                        break
                if not replaced:
                    locations.append(refreshed)
                diagnostics.extend(module_diagnostics)
                diagnostics.extend(scoped_diagnostics)
        except ImportError as error:
            _fail(Stage.DISCOVER, "backend_dependency_error", str(error))
        except UnicodeError as error:
            _fail(Stage.DISCOVER, "source_encoding_error", str(error))
        except ValueError as error:
            _fail(Stage.DISCOVER, "discovery_error", str(error))
        if apply_selection:
            selected = self._target_selections.get(project.snapshot_digest)
            if selected is not None:
                identities = {self._target_key(project, target) for target in selected}
                locations = [
                    location for location in locations
                    if self._target_key(project, location.target) in identities
                ]
        return locations, diagnostics

    def _location(self, project: PreparedProject, target: TargetRef, source: str) -> FunctionLocation:
        key = (project.snapshot_digest, target.file, text_digest(source))
        target_key = self._target_key(project, target)
        has_overlay = bool(self._overlays.get(target_key))
        if key not in self._locations:
            if has_overlay:
                config = self._discovery_config(project, target)
                modules, _ = project_modules(
                    config, project,
                    lambda relative: self._source(project, relative, target),
                )
                locations, _ = scan_source(
                    source, target.file, module=modules.get(target.file, ""),
                    visibility=config.visibility, features=config.build.features,
                    test="--test" in config.build.extra_args,
                )
                self._locations[key] = locations
            else:
                self.discover(project)
        found = [
            item for item in self._locations.get(key, [])
            if item.target.name == target.name and (has_overlay or item.target.line == target.line)
            and (not target.qualified_name or item.target.qualified_name == target.qualified_name)
        ]
        if len(found) != 1:
            _fail(
                Stage.EXTRACT, "target_ambiguous",
                "Target must identify exactly one current source declaration by file, name, and line",
                details={"target": as_object(target)},
            )
        location = found[0]
        if target.source_digest and target.source_digest != text_digest(source) and not has_overlay:
            _fail(Stage.EXTRACT, "source_mismatch", "Target refers to a different source revision")
        if location.unsupported:
            _fail(Stage.EXTRACT, "unsupported_syntax", location.unsupported, recoverable=True)
        self._target_aliases[location.target.id] = target_key
        self._effective_targets[target_key] = location.target
        return location

    def _check_frozen(self, obligation: Obligation) -> None:
        if digest(obligation.native) != obligation.problem_id:
            _fail(Stage.PROOF_CHECKING, "goal_changed", "Obligation content no longer matches its frozen problem hash")
        if obligation.native.get("format") == "specdet.verus.obligation.v1" and (
            obligation.native.get("target") != as_object(obligation.target)
            or obligation.native.get("trusted_translation") != obligation.trusted_translation
        ):
            _fail(Stage.PROOF_CHECKING, "goal_changed", "The target or translation trust differs from the frozen problem")
        known = self._frozen.get(obligation.problem_id)
        if known is not None and known != obligation.id:
            _fail(Stage.PROOF_CHECKING, "goal_changed", "The frozen target or translation metadata was changed")

    def _source_context(self, location: FunctionLocation) -> JsonObject:
        return {
            "start_byte": location.start, "end_byte": location.end,
            "insertion_byte": location.insertion, "in_verus": location.in_verus,
            "module": location.target.module, "declaration": location.declaration,
        }

    def _type_sources(self, project: PreparedProject, target: TargetRef) -> list[JsonObject]:
        names = set(self.config.type_sources)
        modules: dict[str, str] = {}
        if self.config.build.adapter in {"verus.native", "verus.cargo"}:
            modules, diagnostics = project_modules(
                self._discovery_config(project, target), project,
                lambda relative: self._source(project, relative, target),
            )
            ambiguous = [as_object(item) for item in diagnostics if item.code == "ambiguous_module"]
            if ambiguous:
                _fail(Stage.EXTRACT, "module_context_gap",
                      "A source file has multiple module identities", details={"diagnostics": ambiguous})
            names.update(modules)
        names.discard(target.file)
        return [
            {"file": name, "module": modules.get(name, ""), "source": self._source(project, name, target),
             "digest": text_digest(self._source(project, name, target))}
            for name in sorted(names)
        ]

    def _reject_mutable_return(self, source: str, location: FunctionLocation) -> None:
        data = source.encode("utf-8")
        root = parser().parse(data).root_node

        def walk(node) -> None:
            if node.type in {"function_item", "function_signature_item", "assume_specification_item"}:
                if location.start <= node.start_byte and node.end_byte <= location.end:
                    result = node.child_by_field_name("return_type")
                    if result is not None and re.search(
                        r"&\s*(?:'[A-Za-z_]\w*\s+)?mut\b", data[result.start_byte:result.end_byte].decode(),
                    ):
                        _fail(
                            Stage.EXTRACT, "unsupported_mutable_return",
                            "Mutable-reference return values are not modeled by this backend",
                        )
                    return
            for child in node.named_children:
                walk(child)

        walk(root)

    @staticmethod
    def _type_slot(function_spec: JsonObject, slot: str) -> tuple[JsonObject, str]:
        if slot == "return":
            for key in ("return_type", "ret_type"):
                if key in function_spec:
                    return function_spec, key
            raise ValueError("The native contract has no separately modeled return type")
        if not slot.startswith("param:") or not slot[6:]:
            raise ValueError("Type slots must be return or param:<exact source parameter>")
        params = function_spec.get("params")
        if not isinstance(params, list):
            raise ValueError("Native parameter list is unavailable")
        found = [item for item in params if isinstance(item, dict) and item.get("name") == slot[6:]]
        if len(found) != 1:
            raise ValueError("Type slot does not identify one source parameter")
        for key in ("type_info", "type", "ty", "typ"):
            if key in found[0]:
                return found[0], key
        raise ValueError("Parameter has no native type model")

    def extract(self, project: PreparedProject, target: TargetRef) -> Contract:
        from .native.extract import function_spec_from_dict, function_spec_to_dict
        from .native.extract.extractor import Unsupported, extract_spec

        self._check_project(project)
        source = self._source(project, target.file, target)
        location = self._location(project, target, source)
        if not location.has_contract:
            _fail(Stage.EXTRACT, "no_contract", "The executable declaration has no ensures clause")
        self._reject_mutable_return(source, location)
        sources = self._type_sources(project, target)
        active_features = self._discovery_config(project, target).build.features
        extraction_diagnostics: list[str] = []
        lookup_source, cfg_diagnostics = source_for_features(
            source, active_features, test="--test" in self.config.build.extra_args,
        )
        extraction_diagnostics.extend(f"{target.file}: {message}" for message in cfg_diagnostics)
        lookup_types = []
        for entry in sources:
            filtered, cfg_diagnostics = source_for_features(
                str(entry["source"]), active_features, test="--test" in self.config.build.extra_args,
            )
            lookup_types.append(filtered)
            extraction_diagnostics.extend(f"{entry['file']}: {message}" for message in cfg_diagnostics)
        try:
            function = extract_spec(
                lookup_source, location.extractor_name or target.name, type_sources=lookup_types,
                active_features=set(active_features), source_line=location.extractor_line or target.line,
                diagnostics=extraction_diagnostics,
            )
            if not function.ensures:
                _fail(
                    Stage.EXTRACT, "extraction_gap",
                    "The source contract was discovered but its postconditions could not be extracted",
                    recoverable=True,
                )
            serialized = function_spec_to_dict(function)
        except (Unsupported, ValueError, TypeError, KeyError, NotImplementedError) as error:
            _fail(Stage.EXTRACT, "extraction_gap", str(error), recoverable=True)
        key = self._target_key(project, target)
        accepted_semantics: list[JsonObject] = []
        trusted = not ({project.snapshot_digest, key} & self._untrusted_snapshots)
        extraction = self._accepted.get((key, "extraction"))
        if extraction is not None:
            replacement = _copy(_object(extraction["payload"], "extraction")["function_spec"])
            faithful_now = serialized == replacement and not bool(extraction["changes_model"])
            serialized = replacement
            function_spec_from_dict(serialized)
            trusted = trusted and faithful_now
            accepted_semantics.append(_copy(extraction))
        type_models = [
            record for (scope, kind), record in sorted(self._accepted.items())
            if scope == key and kind.startswith("type_model:")
        ]
        for type_model in type_models:
            serialized = _copy(serialized)
            payload = _object(type_model["payload"], "type model")
            owner, field = self._type_slot(serialized, _string(payload["slot"], "slot"))
            faithful_now = owner[field] == payload["type"] and not bool(type_model["changes_model"])
            owner[field] = _copy(payload["type"])
            function_spec_from_dict(serialized)
            trusted = trusted and faithful_now
            accepted_semantics.append(_copy(type_model))
        native: JsonObject = {
            "format": "specdet.verus.contract.v1", "source": source,
            "source_file": target.file, "function_spec": _copy(serialized),
            "source_context": self._source_context(location),
            "type_sources": sources, "snapshot_digest": project.snapshot_digest,
            "overlays": _copy(self._effective_overlays(project, target)),
            "transformations": _copy([
                *self._transformations.get(project.snapshot_digest, []),
                *self._transformations.get(key, []),
            ]),
            "accepted_semantics": accepted_semantics,
            "active_features": list(active_features),
        }
        diagnostics = tuple(
            Diagnostic(Stage.EXTRACT, "extraction_gap", message, "warning")
            for message in extraction_diagnostics
        )
        if location.declaration:
            diagnostics += (Diagnostic(
                Stage.EXTRACT, "contract_declaration",
                "This analysis concerns the declared contract, not a proof that an implementation satisfies it",
                "info",
            ),)
        if not trusted:
            diagnostics += (Diagnostic(
                Stage.EXTRACT, "unverified_translation",
                "An explicitly adopted model or source transformation has not been shown faithful to the original source",
                "warning",
            ),)
        contract = Contract(location.target, text_digest(source), native, diagnostics, trusted)
        self._known_contracts.add(contract.id)
        return contract

    def _check_contract(self, project: PreparedProject, contract: Contract) -> None:
        self._check_project(project)
        if (
            contract.native.get("snapshot_digest") != project.snapshot_digest
            or contract.source_digest != text_digest(self._source(project, contract.target.file, contract.target))
            or contract.source_digest != text_digest(_string(contract.native.get("source"), "contract source"))
        ):
            _fail(Stage.EXTRACT, "source_mismatch", "Contract is not bound to the current prepared source")
        if contract.id not in self._known_contracts:
            extracted = self.extract(project, contract.target)
            if extracted.id != contract.id:
                _fail(Stage.EXTRACT, "source_model_mismatch", "An unvalidated native model cannot replace the source contract")

    def _validate_model_proposal(
        self, proposal: Proposal, project: PreparedProject | None,
        target: TargetRef | None, contract: Contract | None, obligation: Obligation | None,
    ) -> ValidationRecord:
        from .native.extract import function_spec_from_dict, function_spec_to_dict
        from .native.extract.types import TypeInfo

        payload = proposal.payload
        if proposal.kind == "obligation":
            if set(payload) != {"native"} or not isinstance(payload["native"], dict):
                raise ValueError("Obligation proposals require the complete native obligation object")
            if obligation is None:
                return self._record_validation(
                    proposal, "unverified",
                    "A replacement obligation cannot establish its own source faithfulness",
                )
            if canonical_json(payload["native"]) != canonical_json(obligation.native):
                return self._record_validation(
                    proposal, "rejected",
                    "A proof proposal may not replace the original requires, ensures, equality, or context",
                    changes_model=True,
                )
            return self._record_validation(proposal, "accepted")
        if contract is None and project is not None and target is not None:
            try:
                contract = self.extract(project, target)
            except StageError:
                pass
        if proposal.kind == "extraction":
            if set(payload) != {"function_spec"}:
                raise ValueError("Extraction proposals require the complete native function_spec")
            supplied = _object(payload["function_spec"], "function_spec")
            decoded = function_spec_from_dict(supplied)
            if supplied != function_spec_to_dict(decoded):
                raise ValueError("function_spec must use the complete canonical native schema without ignored fields")
            if contract is None:
                return self._record_validation(
                    proposal, "unverified",
                    "The parser gap prevents confirming a candidate contract against the original source",
                    changes_model=True,
                )
            same = canonical_json(supplied) == canonical_json(contract.native["function_spec"])
        else:
            if set(payload) != {"slot", "type"}:
                raise ValueError("Type models require a source slot and a complete native type object")
            slot = _string(payload["slot"], "slot")
            supplied = _object(payload["type"], "type")
            if supplied != TypeInfo.from_dict(supplied).to_dict():
                raise ValueError("Type model must use the canonical native schema without ignored fields")
            if contract is None:
                return self._record_validation(proposal, "unverified", "No source-bound contract exists for the proposed type")
            owner, field = self._type_slot(_copy(contract.native["function_spec"]), slot)
            same = canonical_json(supplied) == canonical_json(owner[field])
        return self._record_validation(
            proposal, "accepted" if same else "needs_approval",
            "" if same else "A changed semantic model requires explicit approval and remains an untrusted translation",
            changes_model=not same,
        )

    def _context_sources(self, contract: Contract) -> dict[str, str]:
        sources = [
            {"file": contract.target.file, "source": contract.native["source"]},
            *contract.native["type_sources"],
        ]
        return {
            str(entry["file"]): source_for_features(
                str(entry["source"]), tuple(contract.native.get("active_features", [])),
                test="--test" in self.config.build.extra_args,
            )[0]
            for entry in sources
        }

    def _context_modules(self, contract: Contract) -> dict[str, str]:
        return {
            contract.target.file: str(contract.native["source_context"]["module"]),
            **{str(entry["file"]): str(entry.get("module", ""))
               for entry in contract.native["type_sources"]},
        }

    def _resolved_function(self, contract: Contract):
        from .native.extract import function_spec_from_dict, function_spec_to_dict
        from .type_context import TypeContextError, resolve_function_types

        key = contract.id, "--test" in self.config.build.extra_args
        if key in self._resolved_types:
            return function_spec_from_dict(_copy(self._resolved_types[key]))
        function = function_spec_from_dict(_copy(contract.native["function_spec"]))
        try:
            resolved = resolve_function_types(
                function, self._context_sources(contract), modules=self._context_modules(contract),
            )
        except TypeContextError as error:
            _fail(Stage.OBSERVATIONS, "type_gap", str(error), recoverable=True)
        self._resolved_types = {key: _copy(function_spec_to_dict(resolved))}
        return resolved

    def _registry(self, contract: Contract, function=None):
        from .native.extract import function_spec_to_dict
        from .native.extract.type_registry import build_registry
        from .native.view.impl_scanner import ImplScan, scan_source as scan_views
        from .native.view.registry import ViewRegistry
        from .type_context import TypeContextError, bound_views

        function = function if function is not None else self._resolved_function(contract)
        key = contract.id, "--test" in self.config.build.extra_args, digest(function_spec_to_dict(function))
        if key in self._view_registries:
            return self._view_registries[key]
        types: dict = {}
        merged = ImplScan("<prepared-project>")
        diagnostics: list[str] = []
        sources = [
            {"file": contract.target.file, "source": contract.native["source"]},
            *contract.native["type_sources"],
        ]
        for entry in sources:
            relative, source = str(entry["file"]), str(entry["source"])
            source, cfg_diagnostics = source_for_features(
                source, tuple(contract.native.get("active_features", [])),
                test="--test" in self.config.build.extra_args,
            )
            diagnostics.extend(f"{relative}: {message}" for message in cfg_diagnostics)
            index = build_registry(source, relative)
            diagnostics.extend(index.diagnostics)
            for definition in index.types.values():
                types.setdefault(definition.name, []).append(definition)
            scan = scan_views(source, relative)
            diagnostics.extend(scan.diagnostics)
            for name, definitions in scan.views.items():
                merged.views.setdefault(name, []).extend(definitions)
            for name, definitions in scan.eqs.items():
                merged.eqs.setdefault(name, []).extend(definitions)
        try:
            bindings = bound_views(
                function,
                self._context_sources(contract),
                modules=self._context_modules(contract),
            )
        except TypeContextError as error:
            _fail(Stage.OBSERVATIONS, "input_view_gap", str(error), recoverable=True)
        registry = ViewRegistry(
            types, merged, accepted_views={}, diagnostics=diagnostics, bound_views=bindings,
        )
        self._view_registries = {key: registry}
        return registry

    def observations(
        self, project: PreparedProject, contract: Contract,
        *, analysis_kind: str | None = None,
    ) -> ObservationPlan:
        from .native.codegen.equal_policy import EqualPolicy
        from .native.codegen.gen_det import _typeinfo_to_typeexpr, build_equal_expr
        from .native.extract import function_spec_from_dict
        from .native.extract.extractor import Unsupported
        from .native.extract.types import TypeKind

        self._check_contract(project, contract)
        policy_name = self._policy_for(project, contract.target)
        if policy_name not in {"verus-observable-v1", "verus-strict-v1"}:
            _fail(
                Stage.OBSERVATIONS, "observation_gap",
                f"Unsupported observation policy: {policy_name}", recoverable=True,
            )
        kind = analysis_kind or self.config.analysis_kind
        if kind not in {"concrete_determinism", "abstract_determinism"}:
            _fail(
                Stage.OBSERVATIONS, "observation_gap",
                f"Unsupported determinism analysis kind: {kind}",
                recoverable=True,
            )
        policy = EqualPolicy(
            errs_equivalent=policy_name == "verus-observable-v1",
            compare_raw_pointers=policy_name == "verus-strict-v1",
            opaque_ok=False, source=policy_name,
        )
        function = self._resolved_function(contract)
        registry = self._registry(contract, function)
        generic_names = set(re.findall(r"(?:^|[<,])\s*([A-Za-z_]\w*)", function.generics_decl))
        if function.trait_name and not function.self_type:
            generic_names.add("Self")
        defined_names = set(function.type_defs)
        ignored: list[str] = []
        known_unknowns = {"nat", "u128", "i128", "char", "f32", "f64", "str"}
        slots = [("return", function.return_type, "r1", "r2")]
        slots.extend((f"post:{p.name}", p.type, f"post1_{p.name}", f"post2_{p.name}") for p in function.params if p.is_mut_ref)

        def inspect_type(ty, path: str, active: set[int]) -> None:
            if id(ty) in active:
                return
            active = {*active, id(ty)}
            raw_pointer = bool(re.match(r"^\*\s*(?:const|mut)\b", ty.name.strip()))
            if raw_pointer:
                ignored.append(f"{path}.pointee_heap")
                if not policy.compare_raw_pointers:
                    ignored.append(f"{path}.pointer_identity")
                return
            if ty.kind in {TypeKind.STRUCT, TypeKind.UNKNOWN} and ty.spec_view is None:
                try:
                    resolved_view = registry.resolve(_typeinfo_to_typeexpr(ty))
                except (TypeError, ValueError) as error:
                    _fail(Stage.OBSERVATIONS, "observation_gap", str(error),
                          recoverable=True, details={"dimension": path})
                if resolved_view.layer in {"L2", "L3", "L4", "source-bound"}:
                    from .native.codegen.gen_det import _try_view_registry_equal

                    projected = resolved_view.view_expr("__specdet_view_probe")
                    equality = _try_view_registry_equal(
                        registry, ty, "__specdet_view_probe", "__specdet_other_probe",
                        caller_generics=" ".join([function.generics_decl, function.where_decl]),
                    )
                    if projected != "__specdet_view_probe" and equality is not None:
                        ignored.append(f"{path}.representation_beyond_view")
                        return
            if ty.kind == TypeKind.UNKNOWN:
                name = ty.name.strip()
                if name not in generic_names | defined_names | known_unknowns and ty.spec_view is None:
                    _fail(
                        Stage.OBSERVATIONS, "observation_gap",
                        f"No source-bound observation semantics were resolved for {name}",
                        recoverable=True, details={"dimension": path, "type": ty.to_dict()},
                    )
            if ty.kind == TypeKind.RESULT and policy.errs_equivalent:
                ignored.append(f"{path}.Err.payload")
            if ty.spec_view is not None:
                ignored.append(f"{path}.representation_beyond_view")
                inspect_type(ty.spec_view, path + "@", active)
                return
            for field in ty.fields:
                inspect_type(field.type, path + "." + field.name, active)
            for variant in ty.variants:
                if ty.kind == TypeKind.RESULT and variant.name == "Err" and policy.errs_equivalent:
                    continue
                if variant.inner is not None:
                    inspect_type(variant.inner, path + "." + variant.name, active)
            if not ty.variants:
                for index, arg in enumerate(ty.type_args):
                    inspect_type(arg, f"{path}[{index}]", active)

        comparisons: list[JsonObject] = []
        for path, ty, lhs, rhs in slots:
            inspect_type(ty, path, set())
            try:
                prelude: list[str] = []
                expression = build_equal_expr(
                    ty, lhs, rhs, policy, registry, prelude_collector=prelude,
                    caller_generics=" ".join([function.generics_decl, function.where_decl]),
                )
            except (Unsupported, ValueError, TypeError, NotImplementedError) as error:
                _fail(Stage.OBSERVATIONS, "observation_gap", str(error), recoverable=True)
            without_comments = re.sub(r"/\*.*?\*/", "", expression, flags=re.S).strip(" \n()")
            allowed_empty = (
                ty.kind == TypeKind.UNIT
                or (ty.kind == TypeKind.STRUCT and not ty.fields and not ty.is_opaque)
                or (not policy.compare_raw_pointers and bool(re.match(r"^\*\s*(?:const|mut)\b", ty.name)))
            )
            if without_comments == "true" and not allowed_empty:
                _fail(
                    Stage.OBSERVATIONS, "observation_gap",
                    "An unresolved output dimension would otherwise use a constant-true equality",
                    recoverable=True, details={"dimension": path, "type": ty.to_dict()},
                )
            comparisons.append({
                "dimension": path, "type": ty.to_dict(), "expression": expression,
                "view_prelude": list(dict.fromkeys(prelude)),
            })
            if "@" in expression:
                ignored.append(f"{path}.representation_beyond_view")
        native: JsonObject = {
            "format": "specdet.verus.observations.v1", "contract_digest": contract.id,
            "analysis_kind": kind,
            "equal_policy": policy.to_dict(), "comparisons": comparisons,
            "view_sources": [
                {"file": contract.target.file, "digest": contract.source_digest},
                *({"file": entry["file"], "digest": entry["digest"]} for entry in contract.native["type_sources"]),
            ],
            "accepted_views": {},
            "source_bound_views": {
                name: as_object(view) for name, view in registry.bound_views.items()
            },
            "diagnostics": list(getattr(registry, "diagnostics", [])),
            "coverage": {
                "return": "modeled", "mutable_parameter_poststates": "modeled",
                "global_or_unmentioned_heap": "not_modeled",
                "pre_feasibility": "not_run", "contract_feasibility": "not_run",
            },
        }
        input_observations: tuple[JsonObject, ...] = ()
        if kind == "abstract_determinism":
            from .abstract import InputViewError, NoAbstractInputs, resolve_inputs

            try:
                input_plan = resolve_inputs(function, registry, self._abstract_inputs_for(project, contract.target))
            except NoAbstractInputs as error:
                _fail(Stage.OBSERVATIONS, "no_abstract_inputs", str(error))
            except InputViewError as error:
                _fail(Stage.OBSERVATIONS, "input_view_gap", str(error), recoverable=True)
            native["input_relation"] = input_plan.to_dict()
            input_observations = input_plan.observations
            native["coverage"]["domain_preservation"] = "not_checked"
        return ObservationPlan(
            policy_name, _copy(native), tuple(dict.fromkeys(ignored)), kind,
            _copy(native["coverage"]), input_observations,
        )

    def lower(
        self, project: PreparedProject, contract: Contract, observations: ObservationPlan,
    ) -> Obligation:
        from .native.codegen.equal_policy import EqualPolicy
        from .native.codegen.gen_det import build_det_check_spec, render_template
        from .native.extract import function_spec_from_dict
        from .native.extract.extractor import Unsupported
        from .native.schema_search.schemas import enumerate_schemas, render_guarded_template

        if observations.native.get("contract_digest") != contract.id:
            _fail(Stage.LOWER, "observation_mismatch", "Observation plan belongs to another contract")
        if observations.id != self.observations(
            project, contract, analysis_kind=observations.analysis_kind,
        ).id:
            _fail(Stage.LOWER, "observation_mismatch", "Observation semantics changed after planning")
        source = _string(contract.native["source"], "contract source")
        if text_digest(source) != contract.source_digest:
            _fail(Stage.LOWER, "source_mismatch", "Contract source was changed after extraction")
        try:
            function = self._resolved_function(contract)
            policy = EqualPolicy.from_dict(_copy(observations.native["equal_policy"]))
            lookup_source, _ = source_for_features(
                source, tuple(contract.native.get("active_features", [])),
                test="--test" in self.config.build.extra_args,
            )
            input_pairs = ()
            if observations.analysis_kind == "abstract_determinism":
                from .native.codegen.pairing import InputPair

                input_pairs = tuple(
                    InputPair(**entry) for entry in observations.native["input_relation"]["pairs"]
                )
            prefix = "__specdet_abstract" if input_pairs else "__specdet"
            det = build_det_check_spec(
                function, check_name=f"{prefix}_{contract.target.id}",
                verus_config={
                    "max_container_depth": self.config.limits.max_container_depth,
                    "analysis_kind": observations.analysis_kind,
                },
                equal_policy=policy, view_registry=self._registry(contract, function), source=lookup_source,
                **({"input_pairs": input_pairs} if input_pairs else {}),
            )
            schemas = enumerate_schemas(det)
            template = render_template(det, [])
            guarded = det.equal_fn_def + "\n\n" + render_guarded_template(det, schemas)
        except (Unsupported, ValueError, TypeError, KeyError, NotImplementedError) as error:
            _fail(Stage.LOWER, "lowering_gap", str(error), recoverable=True)
        key = self._target_key(project, contract.target)
        semantic_proposals = _copy(contract.native.get("accepted_semantics", []))
        for kind in ("policy", "obligation"):
            if (key, kind) in self._accepted:
                semantic_proposals.append(_copy(self._accepted[(key, kind)]))
        native: JsonObject = {
            "format": "specdet.verus.obligation.v1",
            "analysis_kind": observations.analysis_kind,
            "target": as_object(contract.target), "contract_digest": contract.id,
            "function_spec": as_object(function),
            "source_function_spec": _copy(contract.native["function_spec"]),
            "source": source, "source_digest": contract.source_digest,
            "source_context": _copy(contract.native["source_context"]),
            "type_sources": _copy(contract.native["type_sources"]),
            "snapshot_digest": project.snapshot_digest, "overlays": _copy(contract.native["overlays"]),
            "transformations": _copy(contract.native["transformations"]),
            "semantic_config": self._semantic_config(observations.analysis_kind),
            "build_context": self._build_context(project, contract.target),
            "policy": observations.policy, "observations": _copy(observations.native),
            "ignored_dimensions": list(observations.ignored_dimensions),
            "input_relation": _copy(observations.native.get("input_relation", {"kind": "identical_inputs"})),
            "det_spec": det.to_dict(), "schemas": [as_object(schema) for schema in schemas],
            "template": template, "guarded_template": guarded,
            "fn_det": det.check_fn_name, "equal_fn": det.equal_fn_name,
            "opened_closed_specs": list(det.opened_closed_specs),
            "rule_reveals": self._mechanical_reveals(contract, function),
            "accepted_semantic_proposals": semantic_proposals,
            "profile_trace": _copy(self._profile_trace),
            "trusted_translation": contract.trusted_translation,
        }
        obligation = Obligation(digest(native), contract.target, _copy(native), contract.trusted_translation)
        self._frozen[obligation.problem_id] = obligation.id
        return obligation

    def _mechanical_reveals(self, contract: Contract, function) -> list[str]:
        source, _ = source_for_features(
            _string(contract.native["source"], "source"),
            tuple(contract.native.get("active_features", [])),
            test="--test" in self.config.build.extra_args,
        )
        data = source.encode()
        context = _object(contract.native["source_context"], "source context")
        target_scope: tuple[str, ...] | None = None
        definitions: list[tuple[tuple[str, ...], str]] = []

        def walk(node, modules: tuple[str, ...] = (), in_owner: bool = False) -> None:
            nonlocal target_scope
            if node.type == "mod_item":
                name, body = node.child_by_field_name("name"), node.child_by_field_name("body")
                if name is not None and body is not None:
                    part = data[name.start_byte:name.end_byte].decode()
                    for child in body.named_children:
                        walk(child, (*modules, part), False)
                return
            if node.type in {"impl_item", "trait_item"}:
                in_owner = True
            if node.type in {"function_item", "function_signature_item", "assume_specification_item"}:
                if int(context["start_byte"]) <= node.start_byte and node.end_byte <= int(context["end_byte"]):
                    target_scope = modules
                mode = next((child for child in node.named_children if child.type == "function_mode"), None)
                name = node.child_by_field_name("name")
                wrapper = node.parent if node.parent.type == "declaration_with_attrs" else node
                attributes = data[wrapper.start_byte:node.start_byte].decode()
                if (
                    not in_owner and name is not None and mode is not None
                    and data[mode.start_byte:mode.end_byte] == b"spec"
                    and node.child_by_field_name("body") is not None
                    and not re.search(r"verifier::(?:external_body|external)\b", attributes)
                ):
                    definitions.append((modules, data[name.start_byte:name.end_byte].decode()))
                return
            for child in node.named_children:
                walk(child, modules, in_owner)

        walk(parser().parse(data).root_node)
        relation = "\n".join([*function.requires, *function.ensures])
        parameters = {parameter.name for parameter in function.params}
        return sorted({
            name for scope, name in definitions
            if scope == target_scope and name not in parameters
            and re.search(rf"(?<![\w:.]){re.escape(name)}\s*\(", relation)
        })

    def _scan_candidate(self, obligation: Obligation, proof: str, helpers: str) -> tuple[str, ...]:
        from .native.proof_validation import format_violations, scan_helper_lemmas, scan_proof_block

        violations = [*scan_proof_block(proof), *scan_helper_lemmas(helpers)]
        if violations:
            raise ValueError(format_violations(violations))
        reserved = self._reserved_names(_string(obligation.native["source"], "source"))
        reserved.update(self._reserved_names(_string(obligation.native["template"], "template")))
        for entry in obligation.native.get("type_sources", []):
            reserved.update(self._reserved_names(_string(entry["source"], "type source")))
        return proof_structure(proof, helpers, reserved)

    def _reserved_names(self, source: str) -> set[str]:
        data = source.encode("utf-8")
        names: set[str] = set()

        def walk(node) -> None:
            if node.type in {"identifier", "type_identifier"}:
                names.add(data[node.start_byte:node.end_byte].decode())
            if node.type in {
                "function_item", "function_signature_item", "struct_item", "enum_item",
                "const_item", "static_item", "type_item", "mod_item", "trait_item",
            }:
                name = node.child_by_field_name("name")
                if name is not None:
                    names.add(data[name.start_byte:name.end_byte].decode())
            for child in node.named_children:
                walk(child)

        walk(parser().parse(data).root_node)
        return names

    def generate_proof(
        self, project: PreparedProject, contract: Contract, obligation: Obligation,
        strategy: str, feedback: tuple[Diagnostic, ...] = (),
    ) -> ProofCandidate | None:
        self._check_frozen(obligation)
        if strategy == "baseline":
            return ProofCandidate(obligation.problem_id, obligation.id)
        if strategy == "assisted":
            return None
        if strategy == "accepted":
            accepted = self._accepted.get((obligation.problem_id, "proof"))
            if accepted is None:
                return None
            payload = _object(accepted["payload"], "proof payload")
            return ProofCandidate(
                obligation.problem_id, obligation.id,
                _string(payload["proof"], "proof"), _string(payload["helpers"], "helpers"),
                "accepted", _string(accepted["proposal_id"], "proposal_id"),
            )
        if strategy == "rules":
            from .proof_hints import sequence_extensionality

            reveals = obligation.native.get("rule_reveals", [])
            hint = sequence_extensionality(obligation)
            if (not isinstance(reveals, list) or not reveals) and not hint:
                return None
            proof = "\n".join(f"reveal({name});" for name in reveals)
            if hint:
                proof = (proof + "\n" + hint).strip()
            return ProofCandidate(obligation.problem_id, obligation.id, proof=proof, origin="rules")
        _fail(Stage.PROOF_GENERATION, "unknown_strategy", f"Unsupported proof strategy: {strategy}")

    def _copy_worktree(self, project: PreparedProject, destination: Path, overlays: JsonObject) -> list[JsonObject]:
        self._check_project(project)
        if destination.exists() or destination.resolve().is_relative_to(project.root.resolve()):
            _fail(Stage.PROOF_CHECKING, "unsafe_attempt_directory", "Each proof attempt requires a fresh, isolated worktree")
        destination.mkdir(parents=True)
        transformations: list[JsonObject] = []
        for relative, expected in sorted(project.files.items()):
            source = project.source_path(relative)
            if source.is_symlink() or file_digest(source) != expected:
                _fail(Stage.PROOF_CHECKING, "snapshot_modified", f"Prepared source changed: {relative}")
            copied = destination / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, copied)
            copied.chmod(copied.stat().st_mode | 0o200)
            if file_digest(copied) != expected or file_digest(source) != expected:
                _fail(Stage.PROOF_CHECKING, "snapshot_modified", f"Source changed while copying: {relative}")
        for relative, text in overlays.items():
            relative_path(relative)
            if relative not in project.files or not isinstance(text, str):
                _fail(Stage.PROOF_CHECKING, "invalid_overlay", "Frozen overlay does not match the prepared project")
            path = destination / relative
            path.write_text(text, encoding="utf-8")
            transformations.append({
                "kind": "accepted_source_overlay", "file": relative,
                "before_digest": project.files[relative], "after_digest": text_digest(text),
            })
        return transformations

    def _build_paths(self, project: PreparedProject, obligation: Obligation, worktree: Path) -> tuple[Path, Path, str]:
        build = _object(obligation.native["semantic_config"], "semantic configuration")["build"]
        build = _object(build, "build configuration")
        adapter = build.get("adapter")
        injection = build.get("injection_file") or obligation.target.file
        if injection != obligation.target.file:
            _fail(
                Stage.PREPARE, "preparation_gap",
                "The injection file must be the target's source file to preserve private/module context",
                recoverable=True,
            )
        if adapter == "verus.single_file":
            entry = build.get("entrypoint") or obligation.target.file
            if entry != obligation.target.file:
                _fail(Stage.PREPARE, "profile_gap", "Single-file verification must use the target file", recoverable=True)
        elif adapter == "verus.native":
            entry = build.get("entrypoint")
            if not entry:
                _fail(Stage.PREPARE, "profile_gap", "Native verification requires an explicit crate entrypoint", recoverable=True)
        elif adapter == "verus.cargo":
            if not build.get("injection_file"):
                _fail(Stage.PREPARE, "profile_gap", "Cargo verification requires an explicit injection_file", recoverable=True)
            if "Cargo.toml" not in project.files:
                _fail(Stage.PREPARE, "profile_gap", "Cargo verification requires a root manifest", recoverable=True)
            entry = injection
        else:
            _fail(Stage.PREPARE, "profile_gap", f"Unsupported build adapter: {adapter}", recoverable=True)
        if entry not in project.files or injection not in project.files:
            _fail(Stage.PREPARE, "profile_gap", "Entrypoint or injection file is absent from the prepared snapshot", recoverable=True)
        module = obligation.target.module
        configured_module = build.get("verify_module")
        if configured_module and configured_module != module:
            _fail(
                Stage.PREPARE, "module_mismatch",
                "Configured verification module does not match the target's source module",
                recoverable=True, details={"configured": configured_module, "actual": module},
            )
        return worktree / str(entry), worktree / str(injection), module

    def _inject(self, source: str, context: JsonObject, code: str) -> str:
        insertion = context.get("insertion_byte")
        if type(insertion) is not int or not 0 <= insertion <= len(source.encode("utf-8")):
            _fail(Stage.LOWER, "invalid_source_location", "Synthetic obligation has no valid source insertion point")
        if not context.get("in_verus"):
            code = "verus! {\n" + code + "\n}"
        data = source.encode("utf-8")
        return (data[:insertion] + ("\n\n" + code + "\n").encode("utf-8") + data[insertion:]).decode("utf-8")

    def _render_harness(self, obligation: Obligation, candidate: ProofCandidate, *, guarded: bool) -> str:
        template = _string(
            obligation.native["guarded_template" if guarded else "template"], "frozen template",
        )
        prefix = "verus! {\n"
        data = (prefix + template + "\n}").encode()
        root = parser().parse(data).root_node
        if root.has_error:
            _fail(Stage.LOWER, "lowering_gap", "Native determinism template contains unsupported syntax", recoverable=True)
        goals = []

        def walk(node) -> None:
            if node.type == "function_item":
                name = node.child_by_field_name("name")
                if name is not None and data[name.start_byte:name.end_byte].decode() == obligation.native["fn_det"]:
                    goals.append(node)
            for child in node.named_children:
                walk(child)

        walk(root)
        if len(goals) != 1:
            _fail(Stage.LOWER, "ambiguous_template", "Native template must contain exactly one frozen determinism goal")
        body = goals[0].child_by_field_name("body")
        if body is None:
            _fail(Stage.LOWER, "invalid_template", "Determinism goal is missing its proof body")
        if guarded:
            if candidate.proof.strip() or candidate.helpers.strip():
                _fail(Stage.PROOF_CHECKING, "invalid_baseline", "Guarded queries cannot contain generated proof text")
            return template
        start, end = body.start_byte - len(prefix.encode()), body.end_byte - len(prefix.encode())
        encoded = template.encode()
        rendered = (encoded[:start] + ("{\n" + candidate.proof + "\n}").encode() + encoded[end:]).decode()
        if candidate.helpers.strip():
            rendered = candidate.helpers + "\n\n" + rendered
        return rendered

    def check(
        self, project: PreparedProject, obligation: Obligation, candidate: ProofCandidate,
        attempt_dir: Path, *, baseline: bool = False,
    ) -> ProofCheckEvidence:
        started = time.monotonic()
        self._check_frozen(obligation)
        if candidate.problem_id != obligation.problem_id or candidate.obligation_digest != obligation.id:
            return ProofCheckEvidence(
                obligation.problem_id, candidate.id, "rejected", diagnostics=(Diagnostic(
                    Stage.PROOF_CHECKING, "goal_mismatch", "Candidate does not refer to the exact frozen obligation",
                ),),
            )
        if baseline and (candidate.proof.strip() or candidate.helpers.strip() or candidate.origin != "baseline"):
            return ProofCheckEvidence(
                obligation.problem_id, candidate.id, "rejected", diagnostics=(Diagnostic(
                    Stage.PROOF_CHECKING, "invalid_baseline", "The query baseline must be the empty mechanical candidate",
                ),),
            )
        kind = obligation.native.get("analysis_kind", self.config.analysis_kind)
        if kind not in {"concrete_determinism", "abstract_determinism"}:
            _fail(Stage.PROOF_CHECKING, "goal_changed", "Frozen obligation has an unsupported analysis kind")
        if self._semantic_config(str(kind)) != obligation.native.get("semantic_config"):
            _fail(Stage.PROOF_CHECKING, "configuration_changed", "Build semantics changed after the obligation was frozen")
        if project.snapshot_digest != obligation.native.get("snapshot_digest"):
            _fail(Stage.PROOF_CHECKING, "snapshot_mismatch", "Obligation belongs to a different project snapshot")
        try:
            helper_names = self._scan_candidate(obligation, candidate.proof, candidate.helpers)
        except (ValueError, TypeError) as error:
            return ProofCheckEvidence(
                obligation.problem_id, candidate.id, "rejected", diagnostics=(Diagnostic(
                    Stage.PROOF_CHECKING, "unsafe_proof", str(error),
                ),),
            )
        attempt_dir = attempt_dir.resolve()
        attempt_dir.mkdir(parents=True, exist_ok=True)
        worktree = attempt_dir / "worktree"
        transformations = self._copy_worktree(
            project, worktree, _object(obligation.native.get("overlays", {}), "source overlays"),
        )
        entry, injection, module = self._build_paths(project, obligation, worktree)
        source = _string(obligation.native["source"], "frozen source")
        context = _object(obligation.native["source_context"], "source context")
        rendered = self._render_harness(obligation, candidate, guarded=baseline)
        harness = self._inject(source, context, rendered)
        before = file_digest(injection)
        injection.write_text(harness, encoding="utf-8")
        transformations.append({
            "kind": "guarded_obligation" if baseline else "original_obligation_proof",
            "file": obligation.target.file, "before_digest": before,
            "after_digest": text_digest(harness),
            "source_digest": text_digest(source), "template_digest": text_digest(rendered),
        })
        store = ArtifactStore(attempt_dir)
        store.write_text("harness.rs", harness)
        store.artifact("transformations.json", "source_transformations", transformations, (obligation.id, candidate.id))
        frozen_inputs = {
            relative: file_digest(worktree / relative)
            for relative in project.files
        }
        results: list[JsonObject] = []
        diagnostics: list[Diagnostic] = []
        total_verified = 0
        statuses: list[str] = []
        fn_det = _string(obligation.native["fn_det"], "determinism function")
        for index, function in enumerate((fn_det, *helper_names)):
            verification_dir = attempt_dir / ("verifier" if index == 0 else f"helper-{index:03d}")
            try:
                result = self.executor.verify(
                    entry, worktree, verification_dir, function, module, all_functions=False,
                )
                status, verified = classify_process(result)
            except StageError as error:
                diagnostics.append(error.diagnostic)
                status, verified, result = "error", 0, None
            except (OSError, RuntimeError) as error:
                diagnostics.append(Diagnostic(Stage.PROOF_CHECKING, "verifier_error", str(error)))
                status, verified, result = "error", 0, None
            changed = [
                relative for relative, expected in frozen_inputs.items()
                if not (worktree / relative).is_file() or file_digest(worktree / relative) != expected
            ]
            if changed:
                diagnostics.append(Diagnostic(
                    Stage.PROOF_CHECKING, "verifier_modified_input",
                    "The verifier changed frozen project inputs; its results cannot establish this obligation",
                    details={"files": changed},
                ))
                status, verified = "error", 0
            results.append({
                "function": function, "module": module, "role": "determinism" if index == 0 else "helper",
                "status": status, "verified_goals": verified, "artifact": str(verification_dir),
                "process": as_object(result) if result is not None else None,
            })
            statuses.append(status)
            total_verified += verified
            if changed or status in {"compile_error", "error"}:
                break
        if statuses and len(statuses) == 1 + len(helper_names) and all(status == "verified" for status in statuses):
            status = "verified"
        else:
            status = next((value for value in ("error", "compile_error", "timeout", "unproved") if value in statuses), "error")
        if status != "verified" and not diagnostics:
            diagnostics.append(Diagnostic(
                Stage.PROOF_CHECKING, status,
                "Verification did not prove all required synthetic goals",
                details={"functions": results},
            ))
        native: JsonObject = {
            "baseline": baseline, "obligation_digest": obligation.id,
            "harness_digest": text_digest(harness), "template_digest": text_digest(rendered),
            "harness": str(attempt_dir / "harness.rs"),
            "source": str(injection), "entrypoint": str(entry), "worktree": str(worktree),
            "logs": str(attempt_dir / "verifier" / "logs"), "fn_det": fn_det,
            "module": module,
            "crate": str(_object(obligation.native.get("build_context", {}), "build context").get("crate", entry.stem.replace("-", "_"))),
            "crate_entrypoint": str(worktree / str(_object(
                obligation.native.get("build_context", {}), "build context",
            ).get("entrypoint", entry.relative_to(worktree).as_posix()))),
            "invocations": results, "transformations": transformations,
            "det_spec": _copy(obligation.native["det_spec"]),
            "schemas_digest": digest(obligation.native["schemas"]),
        }
        evidence = ProofCheckEvidence(
            obligation.problem_id, candidate.id, status, total_verified,
            (time.monotonic() - started) * 1000, tuple(diagnostics), str(attempt_dir), native,
        )
        store.artifact("check.json", "proof_check", evidence, (obligation.id, candidate.id))
        return evidence

    def _baseline_key(self, obligation: Obligation, baseline: ProofCheckEvidence) -> tuple[str, str]:
        self._check_frozen(obligation)
        expected = ProofCandidate(obligation.problem_id, obligation.id)
        if (
            baseline.problem_id != obligation.problem_id
            or baseline.candidate_id != expected.id
            or baseline.native.get("baseline") is not True
            or baseline.native.get("obligation_digest") != obligation.id
            or baseline.native.get("schemas_digest") != digest(obligation.native["schemas"])
        ):
            _fail(Stage.QUERY, "not_baseline", "Only the original guarded, empty-proof transcript can supply counterexample queries")
        rendered = self._render_harness(obligation, expected, guarded=True)
        expected_source = self._inject(
            _string(obligation.native["source"], "source"),
            _object(obligation.native["source_context"], "source context"), rendered,
        )
        if baseline.native.get("harness_digest") != text_digest(expected_source):
            _fail(Stage.QUERY, "harness_mismatch", "Baseline transcript is not bound to the frozen source and goal")
        if baseline.status in {"rejected", "error"} or any(
            item.code == "verifier_modified_input" for item in baseline.diagnostics
        ):
            _fail(Stage.QUERY, "invalid_baseline", "Rejected or mutated proof attempts cannot be used for counterexample search")
        return obligation.id, digest(baseline)

    def _select_smt(self, obligation: Obligation, baseline: ProofCheckEvidence) -> Path | None:
        directory = Path(_string(baseline.native.get("logs"), "baseline log directory"))
        crate = _string(baseline.native.get("crate"), "baseline crate")
        module = _string(baseline.native.get("module", ""), "baseline module")
        function = _string(obligation.native["fn_det"], "frozen function")
        expected = "::".join(part for part in (crate, module, function) if part)
        matches: list[Path] = []
        for path in sorted(directory.rglob("*.smt2")) if directory.is_dir() else []:
            if path.is_symlink():
                continue
            definitions = re.findall(
                r"^\s*;;\s*Function-Def\s+([^\s]+)\s*$",
                path.read_text(encoding="utf-8"), flags=re.M,
            )
            count = definitions.count(expected)
            if count > 1:
                _fail(Stage.QUERY, "ambiguous_query", "The frozen Function-Def occurs more than once in a transcript")
            if count == 1:
                matches.append(path)
        if len(matches) > 1:
            _fail(
                Stage.QUERY, "ambiguous_query",
                "Multiple SMT transcripts identify the frozen determinism function",
                details={"function": expected, "files": [str(path) for path in matches]},
            )
        return matches[0] if matches else None

    @staticmethod
    def _solver_status(value: object) -> SolverStatus:
        text = str(value).lower()
        if text in {"sat", "unsat", "unknown"}:
            return SolverStatus(text)
        _fail(Stage.QUERY, "invalid_solver_status", f"Unrecognized solver result: {text}")

    def _schema_objects(self, obligation: Obligation):
        from .native.extract.types import DetCheckSpec
        from .native.schema_search.schemas import enumerate_schemas

        det = DetCheckSpec.from_dict(_copy(obligation.native["det_spec"]))
        schemas = enumerate_schemas(det)
        if [as_object(schema) for schema in schemas] != obligation.native["schemas"]:
            _fail(Stage.QUERY, "schemas_changed", "Reconstructed schema semantics differ from the frozen obligation")
        return det, schemas

    def _bundle(self, obligation: Obligation, baseline: ProofCheckEvidence):
        from .native.schema_search.search import build_schema_ctx

        key = self._baseline_key(obligation, baseline)
        if key in self._contexts:
            bundle = self._contexts[key]
            if file_digest(bundle["path"]) != bundle["file_digest"]:
                _fail(Stage.QUERY, "transcript_changed", "The baseline transcript changed after query construction")
            return key, bundle
        path = self._select_smt(obligation, baseline)
        if path is None:
            return key, None
        det, schemas = self._schema_objects(obligation)
        qualified_crate = "::".join(
            value for value in (str(baseline.native["crate"]), str(baseline.native.get("module", "")))
            if value
        )
        ctx = build_schema_ctx(
            path, str(obligation.native["fn_det"]), schemas, str(baseline.native["crate"]),
        )
        expected_function = f"{qualified_crate}::{obligation.native['fn_det']}"
        if ctx.target_function and ctx.target_function != expected_function:
            _fail(Stage.QUERY, "query_target_mismatch", "Schema context does not identify the frozen target")
        timeout, seed = self._solver_options()
        ctx.solver.set(timeout=timeout, random_seed=seed)
        bundle = {
            "ctx": ctx, "path": path, "file_digest": file_digest(path),
            "query_digest": text_digest(ctx.query_smt2), "det": det, "schemas": schemas,
            "role": "originalC",
        }
        self._contexts[key] = bundle
        return key, bundle

    def _solver_query(self, bundle, constraints: list[JsonObject]) -> tuple[SolverStatus, float, str, list]:
        import z3

        ctx = bundle["ctx"]
        by_id = {schema.id: schema for schema in bundle["schemas"]}
        active = {str(item["schema_id"]): _object(item["bindings"], "schema bindings") for item in constraints}
        arguments = []
        assigned: set[str] = set()
        for schema in bundle["schemas"]:
            guard = ctx.guard_consts.get(schema.guard_name)
            if guard is None:
                guard = ctx.guard_consts.get(schema.id)
            if guard is None:
                _fail(Stage.QUERY, "missing_guard", f"Compiled schema guard is missing: {schema.id}")
            if str(guard) in assigned:
                _fail(Stage.QUERY, "aliased_guard", "Multiple schemas unexpectedly share one guard symbol")
            assigned.add(str(guard))
            arguments.append(guard if schema.id in active else z3.Not(guard))
        if len(assigned) != len(ctx.guard_consts):
            _fail(Stage.QUERY, "extra_guard", "The compiled context contains unaccounted guard symbols")
        for schema_id, bindings in active.items():
            if schema_id not in by_id:
                _fail(Stage.SEARCH, "unknown_schema", "Search plan refers to an uncompiled schema")
            for name, value in bindings.items():
                if name not in ctx.k_consts:
                    _fail(Stage.SEARCH, "missing_binding", f"Compiled schema parameter is missing: {name}")
                arguments.append(ctx.k_consts[name] == value)
        timeout, seed = self._solver_options()
        ctx.solver.set(timeout=timeout, random_seed=seed)
        bundle["last_query_timeout_ms"] = timeout
        started = time.monotonic()
        status = self._solver_status(ctx.solver.check(*arguments))
        duration = (time.monotonic() - started) * 1000
        reason = ctx.solver.reason_unknown() if status == SolverStatus.UNKNOWN else ""
        return status, duration, reason, arguments

    def query(self, obligation: Obligation, baseline: ProofCheckEvidence) -> CheckEvidence:
        from .native.schema_search import MissingSchemaBinding, UnsupportedTranscript

        key = self._baseline_key(obligation, baseline)
        if key in self._baselines:
            return self._baselines[key]
        try:
            key, bundle = self._bundle(obligation, baseline)
        except (MissingSchemaBinding, UnsupportedTranscript) as error:
            evidence = CheckEvidence(
                obligation.problem_id, SolverStatus.UNSUPPORTED, "baseline",
                reason=str(error), artifact=str(baseline.native.get("logs", "")),
            )
            self._baselines[key] = evidence
            return evidence
        if bundle is None:
            evidence = CheckEvidence(
                obligation.problem_id,
                SolverStatus.NOT_RUN if baseline.status in {"compile_error", "timeout"} else SolverStatus.UNSUPPORTED,
                "baseline", reason=f"No exact target SMT query artifact was produced ({baseline.status})",
                artifact=str(baseline.native.get("logs", "")),
            )
            self._baselines[key] = evidence
            return evidence
        status, duration, reason, arguments = self._solver_query(bundle, [])
        artifact_dir = Path(baseline.artifact) / "query"
        store = ArtifactStore(artifact_dir)
        path = store.write_text(
            "originalC.smt2",
            bundle["ctx"].solver.sexpr()
            + "\n(check-sat-assuming (" + " ".join(value.sexpr() for value in arguments) + "))\n",
        )
        evidence = CheckEvidence(
            obligation.problem_id, status, "baseline", bundle["query_digest"],
            reason=reason, duration_ms=duration, artifact=str(path),
        )
        store.artifact("baseline.json", "query_check", {
            "evidence": as_object(evidence), "query_role": "originalC",
            "source_transcript": str(bundle["path"]),
            "source_transcript_digest": bundle["file_digest"],
            "guard_assignments": {schema.guard_name: False for schema in bundle["schemas"]},
            "solver_timeout_ms": bundle["last_query_timeout_ms"],
            "seed": self.config.limits.seed,
        }, (obligation.id, digest(baseline)))
        self._baselines[key] = evidence
        return evidence

    @staticmethod
    def _equal_call(det) -> str:
        arguments = [
            str(pair[key]) for pair in det.equal_arg_pairs for key in ("lhs", "rhs")
        ]
        return f"{det.equal_fn_name}({', '.join(arguments)})"

    def _structured_constraint(self, det, schemas, item: JsonObject):
        from .native.extract.predicates import (
            BoolPred, EqPred, LenEqPred, LenRangePred, NotEqualFnPred, RangePred,
            SetContainsPred, SetEmptyPred, SetLenGtPred, VariantIsPred,
        )

        if set(item) != {"variable", "predicate"}:
            raise ValueError("Structured search predicates require variable and predicate")
        variable = _string(item["variable"], "predicate variable")
        predicate = _object(item["predicate"], "predicate")
        kind = predicate.get("kind")
        if not isinstance(kind, str):
            raise ValueError("A structured predicate needs a kind")
        arguments = {name: value for name, value in predicate.items() if name != "kind"}
        kinds = {
            "eq": (EqPred, ("value",)), "range": (RangePred, ("lo", "hi")),
            "bool": (BoolPred, ("value",)), "variant": (VariantIsPred, ("variant",)),
            "len_eq": (LenEqPred, ("n",)), "len_range": (LenRangePred, ("lo", "hi")),
            "set_contains": (SetContainsPred, ("elem",)),
            "set_empty": (SetEmptyPred, ("elem_ty_name",)), "set_nonempty": (SetLenGtPred, ()),
        }
        if kind == "not_equal":
            if arguments or variable != "__tuple__":
                raise ValueError("not_equal refers only to the frozen output tuple")
            pred = NotEqualFnPred(self._equal_call(det))
        else:
            if kind not in kinds:
                raise ValueError(f"Unsupported structured predicate kind: {kind}")
            constructor, required = kinds[kind]
            if set(arguments) != set(required):
                raise ValueError(f"Predicate {kind} requires exactly {', '.join(required)}")
            for name, value in arguments.items():
                if kind == "bool":
                    valid = type(value) is bool
                elif name in {"variant", "elem_ty_name"}:
                    valid = isinstance(value, str)
                else:
                    valid = type(value) is int
                if not valid:
                    raise ValueError(f"Invalid {name} value in {kind} predicate")
            pred = constructor(variable, **arguments)
        matches = [
            (schema, bindings) for schema in schemas
            if (bindings := pred.match_and_bind(schema)) is not None
        ]
        if len(matches) != 1:
            raise ValueError("Predicate does not map to exactly one compiled schema")
        return matches[0]

    def _plan_constraints(self, obligation: Obligation, payload: JsonObject) -> list[JsonObject]:
        from .native.schema_search.schemas import render_schema_expression

        self._check_frozen(obligation)
        if set(payload) != {"constraints"}:
            raise ValueError("Search plans require only a constraints array")
        constraints = payload["constraints"]
        if not isinstance(constraints, list) or not constraints or len(constraints) > 64:
            raise ValueError("Search plans require between 1 and 64 compiled constraints")
        det, schemas = self._schema_objects(obligation)
        by_id = {schema.id: schema for schema in schemas}
        result: list[JsonObject] = []
        seen: set[str] = set()
        for raw in constraints:
            item = _object(raw, "search constraint")
            if set(item) == {"schema_id", "bindings"}:
                schema_id = _string(item["schema_id"], "schema_id")
                if schema_id not in by_id:
                    raise ValueError("Search plan names a schema not present in the frozen harness")
                schema = by_id[schema_id]
                bindings = _object(item["bindings"], "bindings")
            else:
                schema, bindings = self._structured_constraint(det, schemas, item)
            if schema.id in seen:
                raise ValueError("Each compiled schema may occur only once in a search plan")
            seen.add(schema.id)
            if set(bindings) != {name for name, _ in schema.k_params}:
                raise ValueError("Schema bindings must name exactly its compiled parameters")
            if any(type(value) is not int for value in bindings.values()):
                raise ValueError("Schema parameter values must be integers, not expressions")
            for name, ty in schema.k_params:
                if ty in {"nat", "usize", "u8", "u16", "u32", "u64", "u128"} and bindings[name] < 0:
                    raise ValueError("Unsigned schema parameters cannot be negative")
            try:
                expression = render_schema_expression(
                    schema, self._equal_call(det), bindings=bindings,
                )
            except (KeyError, ValueError) as error:
                raise ValueError(f"Compiled predicate cannot be rendered: {error}") from error
            result.append({
                "schema_id": schema.id, "bindings": _copy(bindings),
                "variable": schema.rust_var, "expression": expression,
                "predicate": {
                    "kind": schema.kind.value, "variant": schema.variant,
                    "bool_value": schema.bool_value, "str_value": schema.str_value,
                    "bindings": _copy(bindings),
                },
                "parent_chain": [list(pair) for pair in schema.parent_chain],
            })
        return result

    def _assume_json(self, assume, schemas) -> JsonObject:
        name = type(assume.pred).__name__.removesuffix("Pred")
        kind = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        result: JsonObject = {
            "variable": assume.var_name, "expression": assume.pred.to_rust(),
            "predicate": {"kind": kind, **as_object(assume.pred)},
            "description": assume.description,
        }
        matches = [
            (schema, bindings) for schema in schemas
            if (bindings := assume.pred.match_and_bind(schema)) is not None
        ]
        if len(matches) == 1:
            schema, bindings = matches[0]
            result["schema_id"] = schema.id
            result["bindings"] = _copy(bindings)
        return result

    def _trace_constraints(self, obligation: Obligation, bundle, entry: dict) -> tuple[JsonObject, ...]:
        raw = entry.get("query_constraints")
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            _fail(Stage.SEARCH, "missing_query_trace", "Native search did not preserve the actual checked guard constraints")
        expressions = set(raw)
        ctx = bundle["ctx"]
        active: list[JsonObject] = []
        for schema in bundle["schemas"]:
            guard = ctx.guard_consts.get(schema.guard_name)
            if guard is None:
                guard = ctx.guard_consts.get(schema.id)
            if guard is None:
                _fail(Stage.SEARCH, "missing_guard", "Native search references an uncompiled guard")
            enabled = guard.sexpr() in expressions
            disabled = f"(not {guard.sexpr()})" in expressions
            if enabled == disabled:
                _fail(Stage.SEARCH, "incomplete_guard_trace", "Every search query must explicitly fix every schema guard")
            if not enabled:
                continue
            bindings: JsonObject = {}
            for name, _ in schema.k_params:
                constant = ctx.k_consts.get(name)
                if constant is None:
                    _fail(Stage.SEARCH, "missing_binding", "Native search references an uncompiled parameter")
                pattern = rf"^\(=\s+{re.escape(constant.sexpr())}\s+(-?\d+|\(-\s+\d+\))\)$"
                values = [match[1] for text in raw if (match := re.fullmatch(pattern, text))]
                if len(values) != 1:
                    _fail(Stage.SEARCH, "incomplete_binding_trace", "Active schema parameter lacks one concrete checked value")
                value = values[0]
                bindings[name] = -int(re.search(r"\d+", value)[0]) if value.startswith("(-") else int(value)
            active.append({"schema_id": schema.id, "bindings": bindings})
        if not active:
            return ()
        return tuple(self._plan_constraints(obligation, {"constraints": active}))

    def search(
        self, obligation: Obligation, baseline: ProofCheckEvidence, artifact_dir: Path,
        *, max_rounds: int | None = None,
    ) -> SearchResult:
        from .native.schema_search import MissingSchemaBinding, UnsupportedTranscript
        from .native.schema_search.search import run_schema_search

        self._check_frozen(obligation)
        budget = self.config.limits.max_search_rounds if max_rounds is None else max_rounds
        if type(budget) is not int or budget < 0:
            raise ValueError("Remaining search budget must be a nonnegative integer")
        budget = min(budget, self.config.limits.max_search_rounds)
        accepted = self._accepted.get((obligation.problem_id, "search_plan"))
        planned = self._plan_constraints(obligation, _object(accepted["payload"], "search plan")) if accepted else []
        if budget == 0:
            return SearchResult(
                candidate_constraints=tuple(planned), exhausted=True,
                diagnostics=(Diagnostic(Stage.SEARCH, "search_stalled", "The remaining search-query budget is exhausted", "warning"),),
            )
        try:
            key, bundle = self._bundle(obligation, baseline)
        except (MissingSchemaBinding, UnsupportedTranscript) as error:
            return SearchResult(diagnostics=(Diagnostic(
                Stage.SEARCH, "unsupported_dimension", str(error), "warning",
            ),))
        if bundle is None:
            return SearchResult(diagnostics=(Diagnostic(
                Stage.SEARCH, "search_stalled", "No original-goal SMT bundle is available", "warning",
            ),))
        store = ArtifactStore(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        evidence: list[CheckEvidence] = []
        confirmed: tuple[JsonObject, ...] = ()
        candidate: tuple[JsonObject, ...] = tuple(planned)
        diagnostics: list[Diagnostic] = []
        traces: list[JsonObject] = []
        rounds = 0
        if accepted is not None and (key, str(accepted["proposal_id"])) not in self._executed_plans:
            status, duration, reason, arguments = self._solver_query(bundle, planned)
            trace = {
                "round": rounds, "scope": "slice", "origin": "accepted_search_plan",
                "proposal_id": accepted["proposal_id"], "constraints": _copy(planned),
                "query_constraints": [value.sexpr() for value in arguments],
                "z3_raw": status.value, "z3_ms": duration, "reason_unknown": reason,
                "timeout_ms": bundle["last_query_timeout_ms"], "seed": self.config.limits.seed,
            }
            traces.append(trace)
            path = store.write_json("plan-check.json", trace)
            store.write_text(
                "plan-query.smt2", bundle["ctx"].solver.sexpr()
                + "\n(check-sat-assuming (" + " ".join(value.sexpr() for value in arguments) + "))\n",
            )
            evidence.append(CheckEvidence(
                obligation.problem_id, status, "refinement", bundle["query_digest"],
                tuple(_copy(planned)), reason, duration, str(path),
            ))
            if status == SolverStatus.SAT:
                confirmed, candidate = tuple(_copy(planned)), ()
            self._executed_plans.add((key, str(accepted["proposal_id"])))
            rounds += 1
        exhausted = rounds >= budget
        if rounds < budget:
            timeout, seed = self._solver_options()
            try:
                witness = run_schema_search(
                    bundle["det"], bundle["ctx"], max_rounds=budget - rounds,
                    timeout_ms=timeout, seed=seed,
                )
            except (MissingSchemaBinding, UnsupportedTranscript) as error:
                store.artifact("failure.json", "native_search_failure", {
                    "reason": str(error), "remaining_budget": budget - rounds,
                    "partial_trace_available": False,
                }, (obligation.id, bundle["query_digest"]))
                _fail(
                    Stage.SEARCH, "native_search_error",
                    "Native search lost its fixed query context; do not retry with a refunded budget: " + str(error),
                )
            else:
                offset = rounds
                native_evidence: list[CheckEvidence] = []
                for entry in witness.trace:
                    raw_status = entry.get("z3_raw")
                    if type(entry.get("round")) is not int or entry["round"] != rounds - offset:
                        _fail(Stage.SEARCH, "native_trace_gap", "Native search did not preserve every charged attempt in order")
                    if rounds >= budget:
                        _fail(Stage.SEARCH, "native_budget_exceeded", "Native search exceeded the remaining query budget")
                    if raw_status not in {"sat", "unsat", "unknown"}:
                        if entry.get("result") not in {"unsupported", "interrupted"} or raw_status is not None:
                            _fail(Stage.SEARCH, "invalid_native_trace", "Native search returned an unrecognized attempt status")
                        trace = _copy(entry)
                        trace["round"], trace["native_round"] = rounds, entry["round"]
                        traces.append(trace)
                        path = store.write_json(f"checks/{rounds:06d}.json", trace)
                        interrupted = entry.get("result") == "interrupted"
                        reason = str(entry.get("diagnostic") or entry.get("reason_unknown") or
                                     "No compiled schema supports this candidate")
                        constraints = self._trace_constraints(obligation, bundle, entry) if interrupted else ()
                        role = "baseline" if interrupted and entry.get("scope") == "baseline" and not constraints else "refinement"
                        native_evidence.append(CheckEvidence(
                            obligation.problem_id,
                            SolverStatus.INTERRUPTED if interrupted else SolverStatus.UNSUPPORTED,
                            role, bundle["query_digest"], constraints,
                            reason=reason, artifact=str(path),
                        ))
                        diagnostics.append(Diagnostic(
                            Stage.SEARCH, "run_budget_exhausted" if interrupted else "unsupported_dimension",
                            reason, "warning",
                        ))
                        rounds += 1
                        continue
                    constraints = self._trace_constraints(obligation, bundle, entry)
                    status = self._solver_status(raw_status)
                    role = "baseline" if entry.get("scope") == "baseline" and not constraints else "refinement"
                    trace = _copy(entry)
                    trace["round"] = rounds
                    trace["native_round"] = entry.get("round")
                    trace["constraints"] = _copy(constraints)
                    traces.append(trace)
                    path = store.write_json(f"checks/{rounds:06d}.json", trace)
                    native_evidence.append(CheckEvidence(
                        obligation.problem_id, status, role, bundle["query_digest"], constraints,
                        str(entry.get("reason_unknown") or ""), float(entry.get("z3_ms") or 0), str(path),
                    ))
                    rounds += 1
                if witness.assumes:
                    expressions = sorted(assume.pred.to_rust() for assume in witness.assumes)
                    if not any(
                        entry.get("z3_raw") == "sat" and sorted(entry.get("assumes", [])) == expressions
                        for entry in witness.trace
                    ):
                        _fail(Stage.SEARCH, "unconfirmed_witness", "Native constraints have no corresponding SAT confirmation")
                    confirmed = tuple(self._assume_json(assume, bundle["schemas"]) for assume in witness.assumes)
                if witness.candidate_assumes:
                    candidate = tuple(self._assume_json(assume, bundle["schemas"]) for assume in witness.candidate_assumes)
                evidence.extend(native_evidence)
                exhausted = bool(witness.search_exhausted) or rounds >= budget
                diagnostics.extend(
                    Diagnostic(Stage.SEARCH, "native_search_note", message, "info")
                    for message in witness.diagnostics
                )
                store.artifact("native-witness.json", "native_search_result", {
                    "r0_z3": witness.r0_z3,
                    "confirmed_constraints": [self._assume_json(a, bundle["schemas"]) for a in witness.assumes],
                    "candidate_constraints": [self._assume_json(a, bundle["schemas"]) for a in witness.candidate_assumes],
                    "last_sat_round": witness.last_sat_round,
                    "query_budget": budget - offset, "search_exhausted": witness.search_exhausted,
                    "diagnostics": witness.diagnostics,
                }, (obligation.id, bundle["query_digest"]))
        result = SearchResult(
            tuple(evidence), confirmed, candidate, rounds, exhausted, tuple(diagnostics),
        )
        store.artifact("search.json", "search_result", result, (obligation.id, bundle["query_digest"]))
        store.artifact("trace.json", "solver_trace", traces, (obligation.id, bundle["query_digest"]))
        return result

    def generation_context(
        self, stage: Stage, project: PreparedProject | None, target: TargetRef | None,
        contract: Contract | None, obligation: Obligation | None,
        diagnostics: tuple[Diagnostic, ...],
    ) -> JsonObject:
        context: JsonObject = {
            "backend": "verus", "stage": stage.value,
            "semantic_config": self._semantic_config(),
            "limits": as_object(self.config.limits),
            "diagnostics": [as_object(item) for item in diagnostics][-16:],
            "profile_trace": _copy(self._profile_trace),
            "proposal_guidance": {
                "proof": {"proof": "Verus proof statements only", "helpers": "proof fn lemma_<name> definitions; every helper is verified"},
                "search_plan": {"constraints": "Compiled schema_id and exact integer/bool bindings only"},
                "counterexample": {
                    "bindings": "Concrete constructor values for every unguarded det parameter, including r1/r2",
                    "type_arguments": "Explicit scalar type and const instantiations, if required",
                    "rule": "Candidate values are not proof and never change the raw SMT status",
                },
                "normalization": {"edits": [{"file": "project-relative.rs", "before": "unique original source", "after": "replacement"}]},
                "preparation": {"edits": [{"file": "project-relative.rs", "before": "unique original source", "after": "replacement"}]},
                "target_candidates": {"targets": [{"file": "project-relative.rs", "name": "exact name", "line": 1}]},
                "project_profile": {"build": as_object(self.config.build)},
                "extraction": {"function_spec": "Complete native FunctionSpec JSON shown in this context; changed semantics remain untrusted"},
                "type_model": {"slot": "return or param:<exact parameter name>", "type": "Complete native TypeInfo JSON"},
                "observation": {
                    "policy": ["verus-observable-v1", "verus-strict-v1"],
                    "abstract_inputs": "Optional exact parameter names whose existing source-backed Views should be paired",
                },
                "equality_policy": {"policy": ["verus-observable-v1", "verus-strict-v1"]},
                "obligation": {"native": "Only the identical mechanically lowered obligation is acceptable; proof bodies are proposed separately"},
                "diagnostic": {"message": "Source-backed diagnostic text", "details": {}},
                "explanation": {"message": "Attributed explanation; cannot alter solver evidence", "details": {}},
                "faithfulness": "Parsing or verification alone does not confirm a changed source contract or observation relation",
            },
        }
        if project is not None:
            context["snapshot_digest"] = project.snapshot_digest
            context["files"] = sorted(project.files)[:256]
            context["files_truncated"] = len(project.files) > 256
            context["transformations"] = _copy([
                *self._transformations.get(project.snapshot_digest, []),
                *(self._transformations.get(self._target_key(project, target), []) if target else []),
            ])
        else:
            context["project_files"] = sorted(
                name for name in self._profile_files()
                if name.endswith(".rs") or Path(name).name == "Cargo.toml"
            )[:256]
        if target is not None:
            context["target"] = as_object(target)
        if project is not None and target is not None:
            source = self._source(project, target.file, target)
            effective_target = self._effective_targets.get(self._target_key(project, target), target)
            if effective_target != target:
                context["effective_target"] = as_object(effective_target)
            lines = source.splitlines(keepends=True)
            if len(source) > 24_000:
                start = max(0, effective_target.line - 41)
                excerpt = "".join(lines[start:start + 180])[:24_000]
            else:
                start, excerpt = 0, source
            context["source"] = {
                "file": target.file, "digest": text_digest(source),
                "start_line": start + 1, "text": excerpt,
                "truncated": excerpt != source,
            }
        elif project is not None:
            sample_files = [relative for relative in sorted(project.files) if relative.endswith(".rs")][:4]
            context["source_samples"] = [
                {"file": relative, "digest": project.files[relative],
                 "text": self._source(project, relative)[:4000]}
                for relative in sample_files
            ]
        if contract is not None:
            context["contract_digest"] = contract.id
            context["source_digest"] = contract.source_digest
            serialized = canonical_json(contract.native.get("function_spec", {}))
            context["function_spec"] = json.loads(serialized) if len(serialized) <= 24_000 else {
                "digest": text_digest(serialized), "truncated": True,
            }
            context["type_source_samples"] = [
                {"file": entry["file"], "digest": entry["digest"], "text": str(entry["source"])[:4000]}
                for entry in contract.native.get("type_sources", [])[:4]
            ]
        if obligation is not None:
            context["problem_id"] = obligation.problem_id
            context["obligation_digest"] = obligation.id
            context["analysis_kind"] = obligation.native.get("analysis_kind", "concrete_determinism")
            context["input_relation"] = _copy(obligation.native.get("input_relation", {}))
            context["output_observations"] = _copy(
                obligation.native.get("observations", {}).get("comparisons", [])
            )
            context["proof_requirements"] = (
                "Prove the frozen det fn. In abstract mode, input views agree but concrete "
                "representations may differ. Use the separate precondition of each run; "
                "never strengthen view equivalence to concrete input equality, change "
                "output equality, or infer nondeterminism from failure to prove."
            )
            context["template"] = str(obligation.native.get("template", ""))[:24_000]
            context["schemas"] = _copy(obligation.native.get("schemas", []))[:96]
            context["ignored_dimensions"] = _copy(obligation.native.get("ignored_dimensions", []))
        return context

    def _record_validation(self, proposal: Proposal, status: str, message: str = "",
                           *, changes_model: bool = False) -> ValidationRecord:
        diagnostics = ()
        if message:
            diagnostics = (Diagnostic(
                proposal.stage, "proposal_validation", message,
                "error" if status == "rejected" else "warning",
            ),)
        record = ValidationRecord(proposal.id, status, diagnostics, changes_model, "verus-v1")
        self._validated[proposal.id] = record
        return record

    def _proposal_base(
        self, project: PreparedProject | None, target: TargetRef | None,
        contract: Contract | None, obligation: Obligation | None,
    ) -> JsonObject:
        return {
            "project": project.snapshot_digest if project is not None else "",
            "target": self._target_key(project, target),
            "contract": contract.id if contract is not None else "",
            "obligation": obligation.id if obligation is not None else "",
            "config": digest(self.config.to_dict()),
            "overlays": digest(self._effective_overlays(project, target)) if project is not None else "",
        }

    def validate_proposal(
        self, request: GenerationRequest, proposal: Proposal,
        project: PreparedProject | None, target: TargetRef | None,
        contract: Contract | None, obligation: Obligation | None,
    ) -> ValidationRecord:
        try:
            if project is not None:
                self._check_project(project)
            if (
                proposal.schema_version != 1 or request.schema_version != 1
                or proposal.request_id != request.id or proposal.stage != request.stage
                or proposal.language != "verus" or request.language != "verus"
                or proposal.kind not in request.allowed_kinds
                or proposal.kind not in KINDS[proposal.stage]
                or proposal.base_source_digest != request.source_digest
                or proposal.base_problem_id != request.problem_id
            ):
                raise ValueError("Proposal kind, request, stage, language, or frozen base hashes do not match")
            if obligation is not None:
                self._check_frozen(obligation)
                if request.problem_id != obligation.problem_id:
                    raise ValueError("Proposal refers to a stale obligation")
            elif request.problem_id:
                raise ValueError("No current obligation matches the proposal")
            if target is not None and request.target_id and request.target_id != target.id:
                raise ValueError("Proposal refers to a different target")
            if contract is not None:
                expected_source = contract.source_digest
            elif project is not None and target is not None:
                expected_source = target.source_digest or text_digest(self._source(project, target.file, target))
            elif project is not None:
                expected_source = project.snapshot_digest
            else:
                expected_source = self._request_config_digest
            empty_early_identity = (
                not request.source_digest and project is None and target is None
                and contract is None and obligation is None
            )
            if request.source_digest != expected_source and not empty_early_identity:
                raise ValueError("Proposal source hash is not the current source")
            self._validation_bases[proposal.id] = self._proposal_base(project, target, contract, obligation)
            payload = _object(proposal.payload, "payload")
            if proposal.kind == "proof":
                if obligation is None or set(payload) != {"proof", "helpers"}:
                    raise ValueError("Proof proposals require a frozen obligation and proof/helpers text")
                proof = _string(payload["proof"], "proof")
                helpers = _string(payload["helpers"], "helpers")
                self._scan_candidate(obligation, proof, helpers)
                return self._record_validation(proposal, "accepted_for_check")
            if proposal.kind == "search_plan":
                if obligation is None:
                    raise ValueError("Search plans require a frozen obligation")
                self._plan_constraints(obligation, payload)
                return self._record_validation(proposal, "accepted_for_check")
            if proposal.kind == "counterexample":
                from .witness_replay import render_witness_replay

                if obligation is None or set(payload) - {"bindings", "type_arguments"} or "bindings" not in payload:
                    raise ValueError("Counterexamples require frozen bindings and optional type arguments")
                values = _object(payload["bindings"], "counterexample bindings")
                arguments = _object(payload.get("type_arguments", {}), "counterexample type arguments")
                render_witness_replay(obligation, values, type_arguments=arguments)
                return self._record_validation(proposal, "accepted_for_check")
            if proposal.kind in {"normalization", "preparation"}:
                if project is None:
                    raise ValueError("Source edits require a prepared project")
                edits = payload.get("edits")
                if not isinstance(edits, list):
                    raise ValueError("Source edits require an edits array")
                names = {
                    relative_path(_object(edit, "edit").get("file"))
                    for edit in edits
                }
                sources = {name: self._source(project, name, target) for name in names}
                _, _, faithful = checked_edits(payload, sources)
                return self._record_validation(
                    proposal, "accepted" if faithful else "needs_approval",
                    "" if faithful else "Changed program tokens or unresolved syntax require explicit approval; the translation remains untrusted",
                    changes_model=not faithful,
                )
            if proposal.kind == "target_candidates":
                if project is None or set(payload) != {"targets"}:
                    raise ValueError("Target candidates require a prepared project and a targets array")
                proposed = payload["targets"]
                if not isinstance(proposed, list) or not proposed:
                    raise ValueError("targets must be a nonempty array")
                locations, _ = self._discover_locations(project, apply_selection=False)
                seen: set[tuple[str, str, int]] = set()
                for item in proposed:
                    candidate = _object(item, "target candidate")
                    if set(candidate) != {"file", "name", "line"}:
                        raise ValueError("Each target candidate requires exactly file, name, and line")
                    relative = relative_path(candidate["file"])
                    name = _string(candidate["name"], "target name")
                    line = candidate["line"]
                    if type(line) is not int or line <= 0:
                        raise ValueError("Target line must be a positive integer")
                    found = [
                        loc for loc in locations
                        if (loc.target.file, loc.target.name, loc.target.line) == (relative, name, line)
                    ]
                    if len(found) != 1 or found[0].unsupported:
                        return self._record_validation(
                            proposal, "unverified",
                            "A proposed target is not an unambiguous, supported source declaration",
                        )
                    identity = (relative, name, line)
                    if identity in seen:
                        raise ValueError("Target candidates must not repeat")
                    seen.add(identity)
                return self._record_validation(proposal, "accepted")
            if proposal.kind == "project_profile":
                if target is not None:
                    raise ValueError("Project profiles must be adopted during project preflight, not by an individual target")
                files = set(project.files) if project is not None else self._profile_files()
                updated, faithful = checked_profile(payload, self.config, files)
                if updated.build.adapter == "verus.cargo":
                    self._cargo_context(project, stage=proposal.stage, config=updated)
                return self._record_validation(
                    proposal, "accepted" if faithful else "needs_approval",
                    "" if faithful else "The proposed profile changes the logical build context",
                    changes_model=not faithful,
                )
            if proposal.kind in {"observation", "equality_policy"}:
                allowed = {"policy", "abstract_inputs"} if proposal.kind == "observation" else {"policy"}
                if not payload or set(payload) - allowed:
                    raise ValueError("Observation proposals require named output policy and/or source-backed abstract inputs")
                same = True
                if "policy" in payload:
                    if payload["policy"] not in {"verus-observable-v1", "verus-strict-v1"}:
                        raise ValueError("Unknown output observation policy")
                    same = payload["policy"] == self._policy_for(project, target)
                if "abstract_inputs" in payload:
                    from .abstract import InputViewError, resolve_inputs
                    from .native.extract import function_spec_from_dict

                    values = payload["abstract_inputs"]
                    if (
                        contract is None or not isinstance(values, list)
                        or any(not isinstance(name, str) or not name for name in values)
                        or len(set(values)) != len(values)
                    ):
                        raise ValueError("abstract_inputs requires a contract and unique parameter names")
                    function = self._resolved_function(contract)
                    registry = self._registry(contract, function)
                    candidate = resolve_inputs(function, registry, tuple(values))
                    try:
                        current = resolve_inputs(
                            function, registry, self._abstract_inputs_for(project, target),
                        )
                    except InputViewError:
                        same = False
                    else:
                        same = same and candidate.pairs == current.pairs
                return self._record_validation(
                    proposal, "accepted" if same else "needs_approval",
                    "" if same else "Changing the observation relation defines a different problem",
                    changes_model=not same,
                )
            if proposal.kind in {"extraction", "type_model", "obligation"}:
                return self._validate_model_proposal(proposal, project, target, contract, obligation)
            if proposal.kind in {"diagnostic", "explanation"}:
                if set(payload) - {"message", "details"} or not isinstance(payload.get("message"), str):
                    raise ValueError("Annotations require message text and optional JSON details")
                if len(payload["message"]) > 16_000:
                    raise ValueError("Annotation exceeds its size limit")
                return self._record_validation(proposal, "accepted")
            raise ValueError("Unsupported proposal kind")
        except (ValueError, TypeError, KeyError, StageError) as error:
            return self._record_validation(proposal, "rejected", str(error))

    def _profile_files(self) -> set[str]:
        files = set()
        root = self.config.project_root.resolve()
        ignored = {".git", "target", ".venv", "__pycache__", ".pytest_cache", "backups"}
        for directory, dirs, names in os.walk(root, followlinks=False):
            base = Path(directory)
            dirs[:] = [
                name for name in dirs
                if name not in ignored and not (base / name).is_symlink()
                and (base / name).resolve() != self.config.output_dir.resolve()
                and not any(fnmatch.fnmatchcase(
                    (base / name).relative_to(root).as_posix() + "/", pattern,
                ) for pattern in self.config.exclude)
            ]
            for name in names:
                path = base / name
                relative = path.relative_to(root).as_posix()
                if (
                    path.is_file() and not path.is_symlink()
                    and not any(fnmatch.fnmatchcase(relative, rule) for rule in self.config.exclude)
                ):
                    files.add(relative)
        return files

    def _policy_for(self, project: PreparedProject | None, target: TargetRef | None) -> str:
        key = self._target_key(project, target)
        accepted = self._accepted.get((key, "policy"))
        if accepted is not None:
            return _string(_object(accepted["payload"], "policy")["policy"], "policy")
        return self.config.observation_policy

    def _abstract_inputs_for(
        self, project: PreparedProject | None, target: TargetRef | None,
    ) -> tuple[str, ...]:
        accepted = self._accepted.get((self._target_key(project, target), "abstract_inputs"))
        if accepted is not None:
            return tuple(accepted["payload"]["abstract_inputs"])
        return tuple(self.config.abstract_inputs)

    def adopt_proposal(
        self, proposal: Proposal, project: PreparedProject | None,
        target: TargetRef | None, contract: Contract | None, obligation: Obligation | None,
    ) -> Stage:
        record = self._validated.get(proposal.id)
        if record is None or record.status not in {"accepted", "accepted_for_check", "needs_approval"}:
            _fail(proposal.stage, "proposal_not_validated", "Only validated, explicitly adopted proposals can change backend state")
        if self._validation_bases.get(proposal.id) != self._proposal_base(project, target, contract, obligation):
            _fail(proposal.stage, "proposal_base_changed", "Proposal adoption no longer matches its validated target, model, or configuration")
        payload = _copy(proposal.payload)
        stored = {
            "payload": payload, "proposal_id": proposal.id,
            "changes_model": record.changes_model, "validation_status": record.status,
        }
        target_key = self._target_key(project, target)
        if proposal.kind == "proof":
            if obligation is None:
                _fail(proposal.stage, "missing_obligation", "A proof needs a frozen obligation")
            self._accepted[(obligation.problem_id, "proof")] = stored
            return Stage.PROOF_GENERATION
        if proposal.kind == "search_plan":
            if obligation is None:
                _fail(proposal.stage, "missing_obligation", "A search plan needs a frozen obligation")
            self._accepted[(obligation.problem_id, "search_plan")] = stored
            return Stage.SEARCH
        if proposal.kind == "counterexample":
            if obligation is None:
                _fail(proposal.stage, "missing_obligation", "Counterexample needs a frozen obligation")
            self._accepted[(obligation.problem_id, "counterexample")] = stored
            return Stage.COUNTEREXAMPLE
        if proposal.kind in {"normalization", "preparation"}:
            if project is None:
                _fail(proposal.stage, "missing_project", "Source edits need a prepared project")
            names = {relative_path(_object(edit, "edit")["file"]) for edit in payload["edits"]}
            sources = {name: self._source(project, name, target) for name in names}
            overlays, transformations, faithful = checked_edits(payload, sources)
            scope = target_key if target is not None else project.snapshot_digest
            self._overlays.setdefault(scope, {}).update(overlays)
            if target is not None:
                self._scoped_targets[(project.snapshot_digest, scope)] = target
            for transformation in transformations:
                transformation["proposal_id"] = proposal.id
            self._transformations.setdefault(scope, []).extend(transformations)
            if not faithful:
                self._untrusted_snapshots.add(scope)
            self._accepted[(target_key, proposal.kind)] = stored
            return Stage.DISCOVER
        if proposal.kind == "target_candidates":
            if project is None:
                _fail(proposal.stage, "missing_project", "Target selection needs a prepared project")
            found, _ = self._discover_locations(project, apply_selection=False)
            identities = {(item["file"], item["name"], item["line"]) for item in payload["targets"]}
            self._target_selections[project.snapshot_digest] = tuple(
                loc.target for loc in found
                if (loc.target.file, loc.target.name, loc.target.line) in identities
            )
            return Stage.DISCOVER
        if proposal.kind == "project_profile":
            files = set(project.files) if project is not None else self._profile_files()
            self.config, faithful = checked_profile(payload, self.config, files)
            self._executor, self._identity = None, None
            self._profile_trace.append({
                "proposal_id": proposal.id, "effective_config": self._semantic_config(),
                "context_preserving": faithful,
                "source_selection": {
                    "include": list(self.config.include), "exclude": list(self.config.exclude),
                    "visibility": self.config.visibility,
                },
            })
            self._accepted[(target_key, proposal.kind)] = stored
            return Stage.DISCOVER if project is not None else Stage.CONFIG
        if proposal.kind in {"observation", "equality_policy"}:
            if "policy" in payload:
                self._accepted[(target_key, "policy")] = stored
            if "abstract_inputs" in payload:
                self._accepted[(target_key, "abstract_inputs")] = stored
            return Stage.OBSERVATIONS
        if proposal.kind == "extraction":
            self._accepted[(target_key, "extraction")] = stored
            for key in [key for key in self._accepted if key[0] == target_key and key[1].startswith("type_model:")]:
                del self._accepted[key]
            return Stage.EXTRACT
        if proposal.kind == "type_model":
            self._accepted[(target_key, "type_model:" + str(payload["slot"]))] = stored
            return Stage.EXTRACT
        if proposal.kind == "obligation":
            self._accepted[(target_key, "obligation")] = stored
            return Stage.LOWER
        return Stage.REPORT

    def find_counterexample(
        self, project: PreparedProject, obligation: Obligation, artifact_dir: Path,
        *, max_candidates: int,
    ):
        from .counterexamples import find_counterexample

        self._check_frozen(obligation)
        accepted = self._accepted.get((obligation.problem_id, "counterexample"))
        return find_counterexample(
            self, project, obligation, artifact_dir, max_candidates=max_candidates,
            accepted=_copy(accepted["payload"]) if accepted else None,
        )
