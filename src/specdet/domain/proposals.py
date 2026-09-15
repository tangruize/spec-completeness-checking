from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    Diagnostic, JsonObject, SCHEMA_VERSION, Stage, as_object, digest, require_text,
)


@dataclass(frozen=True)
class StageCapability:
    stage: Stage
    kinds: tuple[str, ...]
    triggers: tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True)
class GenerationRequest:
    stage: Stage
    language: str
    allowed_kinds: tuple[str, ...]
    source_digest: str
    problem_id: str
    context: JsonObject
    target_id: str = ""
    feedback: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    @property
    def id(self) -> str:
        return digest(self)

    def to_dict(self) -> JsonObject:
        return as_object(self)


@dataclass(frozen=True)
class RawResponse:
    request_id: str
    text: str
    provider: str
    model: str = ""
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class Proposal:
    request_id: str
    stage: Stage
    language: str
    kind: str
    base_source_digest: str
    base_problem_id: str
    payload: JsonObject
    origin: str = "llm"
    schema_version: int = SCHEMA_VERSION

    @property
    def id(self) -> str:
        return digest(self)

    def to_dict(self) -> JsonObject:
        return as_object(self)

    @classmethod
    def from_dict(cls, data: JsonObject) -> Proposal:
        version = data.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported proposal schema_version: {version!r}")
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Proposal payload must be an object")
        allowed = {
            "schema_version", "request_id", "stage", "language", "kind",
            "base_source_digest", "base_problem_id", "payload", "origin",
        }
        if extra := set(data) - allowed:
            raise ValueError(f"Unknown proposal fields: {', '.join(sorted(extra))}")
        return cls(
            request_id=require_text(data, "request_id"),
            stage=Stage(require_text(data, "stage")),
            language=require_text(data, "language"),
            kind=require_text(data, "kind"),
            base_source_digest=require_text(data, "base_source_digest"),
            base_problem_id=require_text(data, "base_problem_id"),
            payload=payload,
            origin=require_text(data, "origin", default="llm"),
        )


@dataclass(frozen=True)
class ValidationRecord:
    proposal_id: str
    status: str
    diagnostics: tuple[Diagnostic, ...] = ()
    changes_model: bool = False
    validator_version: str = "1"


@dataclass(frozen=True)
class AdoptionRecord:
    proposal_id: str
    validation_digest: str
    status: str
    reason: str
    schema_version: int = SCHEMA_VERSION

