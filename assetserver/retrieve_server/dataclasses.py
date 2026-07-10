"""Shared DTOs for the unified retrieve server."""

import json

from dataclasses import asdict, dataclass


@dataclass
class StreamedResult:
    """Single result in a streaming batch response."""

    index: int
    status: str
    data: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())
