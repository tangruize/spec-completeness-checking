from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from specdet.domain.models import JsonObject, SCHEMA_VERSION, as_object, canonical_json
from specdet.domain.proposals import (
    AdoptionRecord, GenerationRequest, Proposal, RawResponse, ValidationRecord,
)

from .prompts import PROMPT_VERSION


def atomic_json(path: Path, value: object) -> None:
    """Replace a record atomically using an exclusive sibling file, not system temp."""
    text = canonical_json(value) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid4().hex}.pending")
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def write_record(path: Path, record_type: str, data: object) -> None:
    atomic_json(path, {
        "schema_version": SCHEMA_VERSION,
        "record_type": record_type,
        "data": as_object(data),
    })


class AttemptRecords:
    def __init__(self, run_dir: Path, request: GenerationRequest, attempt_id: str):
        self.run_dir = run_dir
        self.request = request
        self.directory = run_dir / "generation" / request.id
        self.attempt_directory = self.directory / "attempts" / attempt_id

    def _write(self, name: str, record_type: str, data: object) -> None:
        write_record(self.attempt_directory / name, record_type, data)
        write_record(self.directory / name, record_type, data)

    def _proposal_record(
        self, proposal_id: str, name: str, record_type: str, data: object,
    ) -> None:
        directory = self.run_dir / "proposals" / proposal_id
        write_record(directory / "attempts" / self.attempt_directory.name / name, record_type, data)
        write_record(directory / name, record_type, data)

    def start(self, prompt: str) -> None:
        self._write("request.json", "generation_request", {
            "request_id": self.request.id,
            "request": self.request.to_dict(),
            "prompt_version": PROMPT_VERSION,
            "prompt": prompt,
        })

    def response(self, response: RawResponse) -> None:
        self._write("response.json", "raw_response", response)

    def provider_error(self, error: JsonObject) -> None:
        self._write("response.json", "provider_error", {
            "request_id": self.request.id, **error,
        })

    def proposal(self, proposal: Proposal) -> None:
        self._write("proposal.json", "proposal", proposal)
        self._proposal_record(proposal.id, "proposal.json", "proposal", proposal)

    def validation(self, record: ValidationRecord) -> None:
        self._write("validation.json", "validation", record)
        if record.proposal_id:
            self._proposal_record(record.proposal_id, "validation.json", "validation", record)

    def adoption(self, record: AdoptionRecord) -> None:
        self._write("adoption.json", "adoption", record)
        if record.proposal_id:
            self._proposal_record(record.proposal_id, "adoption.json", "adoption", record)

    def telemetry(self, data: object) -> None:
        self._write("telemetry.json", "generation_attempt", data)
