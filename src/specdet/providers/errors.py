from __future__ import annotations

from specdet.domain.models import JsonObject


class ProviderError(Exception):
    def __init__(
        self, code: str, message: str, *, retryable: bool = False,
        metadata: JsonObject | None = None,
    ):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.metadata = {} if metadata is None else metadata

    def to_dict(self) -> JsonObject:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "metadata": self.metadata,
        }
