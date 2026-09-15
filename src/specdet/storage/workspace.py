from __future__ import annotations

import fnmatch
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from specdet.config import Config
from specdet.domain.models import Diagnostic, JsonObject, Stage, StageError, digest
from .artifacts import ArtifactStore, file_digest

_IGNORED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "target", "backups"}


@dataclass(frozen=True)
class PreparedProject:
    root: Path
    source_root: Path
    snapshot_digest: str
    files: dict[str, str]

    def source_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"Source reference escapes workspace: {relative}")
        return path


def prepare_project(config: Config, run_dir: Path) -> PreparedProject:
    """Copy real input bytes, never writable symlinks or hardlinks to user files."""
    root = config.project_root.resolve()
    destination = (run_dir / "workspace").resolve()
    if destination.exists():
        raise FileExistsError(f"Workspace already exists: {destination}")
    destination.mkdir(parents=True)
    manifest: dict[str, str] = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        kept = []
        for name in sorted(dirs):
            candidate = base / name
            relative = candidate.relative_to(root).as_posix()
            if name in _IGNORED_DIRS or candidate.resolve() == config.output_dir:
                continue
            if candidate.resolve() == run_dir.resolve():
                continue
            if any(fnmatch.fnmatchcase(relative + "/", pattern) for pattern in config.exclude):
                continue
            if candidate.is_symlink():
                raise StageError(Diagnostic(
                    Stage.PREPARE, "symlink_input",
                    f"Directory symlinks require an explicit project profile: {relative}",
                ))
            kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            source = base / name
            relative = source.relative_to(root).as_posix()
            if any(fnmatch.fnmatchcase(relative, pattern) for pattern in config.exclude):
                continue
            if source.is_symlink():
                raise StageError(Diagnostic(
                    Stage.PREPARE, "symlink_input",
                    f"File symlinks require an explicit project profile: {relative}",
                ))
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            before = file_digest(source)
            shutil.copy2(source, target)
            copied = file_digest(target)
            if copied != before or file_digest(source) != before:
                raise StageError(Diagnostic(
                    Stage.PREPARE, "source_changed",
                    f"Input changed while making snapshot: {relative}",
                ))
            manifest[relative] = copied
    if not manifest:
        raise StageError(Diagnostic(Stage.PREPARE, "empty_project", "No input files found"))
    snapshot_digest = digest(manifest)
    ArtifactStore(run_dir).artifact("snapshot.json", "source_snapshot", {
        "source_root": str(root),
        "snapshot_digest": snapshot_digest,
        "files": manifest,
    })
    return PreparedProject(destination, root, snapshot_digest, manifest)


def input_files(config: Config, project: PreparedProject) -> list[Path]:
    selected: dict[str, Path] = {}
    for pattern in config.include:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError(f"Input glob escapes project: {pattern}")
        for path in sorted(project.root.glob(pattern)):
            relative = path.relative_to(project.root).as_posix()
            if path.is_file() and relative in project.files:
                if not any(fnmatch.fnmatchcase(relative, rule) for rule in config.exclude):
                    selected[relative] = path
    return [selected[key] for key in sorted(selected)]

