from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from specdet import __version__
from specdet.domain.models import ArtifactRef, JsonObject, as_object, digest, json_value


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError(f"Artifact path escapes its run: {relative}")
        return path

    def write_text(self, relative: str, text: str) -> Path:
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary = stream.name
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return path

    def write_json(self, relative: str, value: object) -> Path:
        return self.write_text(
            relative, json.dumps(json_value(value), ensure_ascii=False, indent=2) + "\n"
        )

    def artifact(
        self, relative: str, kind: str, payload: object, inputs: tuple[str, ...] = (),
    ) -> ArtifactRef:
        body = json_value(payload)
        payload_digest = digest(body)
        self.write_json(relative, {
            "schema_version": 1,
            "kind": kind,
            "producer_version": __version__,
            "input_digests": list(inputs),
            "payload_digest": payload_digest,
            "payload": body,
        })
        return ArtifactRef(kind=kind, path=relative, digest=payload_digest)

    def read_artifact(self, relative: str, *, expected_kind: str | None = None) -> JsonObject:
        envelope = as_object(json.loads(self.path(relative).read_text(encoding="utf-8")))
        if type(envelope.get("schema_version")) is not int or envelope["schema_version"] != 1:
            raise ValueError("Unsupported artifact schema")
        if expected_kind is not None and envelope.get("kind") != expected_kind:
            raise ValueError("Artifact kind does not match its consumer")
        if digest(envelope.get("payload")) != envelope.get("payload_digest"):
            raise ValueError(f"Artifact digest mismatch: {relative}")
        return as_object(envelope["payload"])

    def event(self, event: JsonObject) -> None:
        path = self.path("events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(json_value(event), ensure_ascii=False, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
