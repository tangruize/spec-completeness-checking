from __future__ import annotations

from pathlib import Path

from specdet.assistance.prompts import MAX_RESPONSE_BYTES, strict_json_object
from specdet.domain.models import SCHEMA_VERSION, JsonObject, require_text
from specdet.domain.proposals import GenerationRequest, RawResponse

from .errors import ProviderError

_MAX_RECORD_BYTES = MAX_RESPONSE_BYTES * 6 + 65536


class ReplayProvider:
    """Read <responses>/<exact request.id>/response.json, then <request.id>.json.

    Captured raw_response envelopes are accepted directly. Handwritten fixtures may
    instead be objects with request_id/text and optional provider/model/metadata.
    An existing invalid nested record never falls back to a different record.
    Captured provider_error envelopes re-raise the recorded typed error, preserving
    retry feedback and therefore the identities of subsequent captured requests.
    """

    mode = "replay"

    def __init__(self, responses: Path | str):
        self.responses = Path(responses).expanduser().resolve()

    def _read(self, path: Path) -> JsonObject:
        if not path.resolve().is_relative_to(self.responses):
            raise ProviderError("replay_path_escape", "Replay record escapes its pinned directory")
        with path.open("r", encoding="utf-8") as stream:
            text = stream.read(_MAX_RECORD_BYTES + 1)
        if len(text.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise ProviderError("response_too_large", "Replay record exceeds the size limit")
        return strict_json_object(text)

    def generate(self, request: GenerationRequest) -> RawResponse:
        nested = self.responses / request.id / "response.json"
        flat = self.responses / f"{request.id}.json"
        path = nested
        try:
            try:
                data = self._read(nested)
            except FileNotFoundError:
                path = flat
                data = self._read(flat)
        except FileNotFoundError as error:
            raise ProviderError(
                "replay_miss", f"No pinned response exists for request {request.id}",
            ) from error
        except (OSError, UnicodeError) as error:
            raise ProviderError("replay_read_error", str(error)) from error
        except (ValueError, TypeError, RecursionError) as error:
            raise ProviderError("replay_invalid", str(error)) from error

        try:
            record_type = "raw_response"
            if "record_type" in data or "data" in data:
                if set(data) != {"schema_version", "record_type", "data"}:
                    raise ValueError("Invalid raw_response envelope fields")
                if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
                    raise ValueError("Unsupported raw_response schema_version")
                record_type = data["record_type"]
                if record_type not in ("raw_response", "provider_error") or not isinstance(data["data"], dict):
                    raise ValueError("Expected a raw_response or provider_error record")
                data = data["data"]
            elif "schema_version" in data:
                if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
                    raise ValueError("Unsupported raw_response schema_version")
                data = {key: value for key, value in data.items() if key != "schema_version"}
            if record_type == "provider_error":
                if set(data) != {"request_id", "code", "message", "retryable", "metadata"}:
                    raise ValueError("Invalid provider_error fields")
                recorded_id = require_text(data, "request_id")
                code = require_text(data, "code")
                message = require_text(data, "message")
                retryable = data["retryable"]
                metadata = data["metadata"]
                if not code or type(retryable) is not bool or not isinstance(metadata, dict):
                    raise ValueError("Invalid provider_error field types")
                if recorded_id != request.id:
                    raise ProviderError(
                        "replay_request_mismatch", "Pinned error has a different request_id",
                    )
                raise ProviderError(
                    code, message, retryable=retryable,
                    metadata={
                        "replay_path": str(path.relative_to(self.responses)),
                        "recorded_metadata": metadata,
                    },
                )
            if set(data) - {"request_id", "text", "provider", "model", "metadata"}:
                raise ValueError("Unknown raw_response fields")
            recorded_id = require_text(data, "request_id")
            text = require_text(data, "text")
            provider = require_text(data, "provider", default="replay")
            model = require_text(data, "model", default="")
            metadata = data.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("raw_response metadata must be an object")
        except (ValueError, TypeError) as error:
            raise ProviderError("replay_invalid", str(error)) from error
        if recorded_id != request.id:
            raise ProviderError("replay_request_mismatch", "Pinned response has a different request_id")
        return RawResponse(
            request.id, text, "replay", model,
            {
                "replay_path": str(path.relative_to(self.responses)),
                "recorded_provider": provider,
                "recorded_metadata": metadata,
            },
        )
