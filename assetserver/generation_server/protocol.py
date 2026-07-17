"""Protocol shared by the generation server and model pipelines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable


class GenerationValidationError(ValueError):
    """A caller supplied options that the selected pipeline cannot accept."""


@dataclass(frozen=True)
class GenerationRequest:
    generation_id: str
    image_path: Path
    prompt: str | None
    options: dict[str, Any]


@dataclass(frozen=True)
class GenerationResult:
    generation_id: str
    asset_ref: str
    backend: str

    def to_dict(self) -> dict[str, str]:
        return {
            "generation_id": self.generation_id,
            "asset_ref": self.asset_ref,
            "backend": self.backend,
        }


@runtime_checkable
class GenerationPipeline(Protocol):
    name: str

    def load(self) -> None: ...

    def generate(self, request: GenerationRequest, output_path: Path) -> None: ...

    def cleanup_request(self) -> None: ...

    def tool_versions(self) -> Mapping[str, str]: ...
