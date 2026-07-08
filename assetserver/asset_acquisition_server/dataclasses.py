from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AssetAcquisitionServerRequest:
    """HTTP request for acquiring simulation-ready assets.

    The server owns generation/retrieval, mesh conversion, collision geometry
    generation, and registry updates. Callers provide the scene-local output
    directory where generated assets should be written.
    """

    output_dir: str
    object_type: str
    object_descriptions: list[str]
    short_names: list[str]
    desired_dimensions: list[list[float]]
    asset_category: str | None = None
    agent_type: str | None = None
    style_context: str | None = None
    operation_type: str = "initial"
    scene_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssetAcquisitionServerRequest":
        return cls(**data)

    @property
    def effective_asset_category(self) -> str | None:
        """Return the server-facing asset category, accepting legacy agent_type."""
        if self.asset_category is not None:
            return self.asset_category
        if self.agent_type is not None:
            return self.agent_type
        if self.object_type in _VALID_ASSET_CATEGORIES:
            return self.object_type
        return None

    def validate(self) -> None:
        if not self.output_dir:
            raise ValueError("output_dir is required")
        if not self.object_descriptions:
            raise ValueError("object_descriptions must be non-empty")
        if len(self.object_descriptions) != len(self.short_names):
            raise ValueError("object_descriptions and short_names lengths differ")
        if len(self.object_descriptions) != len(self.desired_dimensions):
            raise ValueError(
                "object_descriptions and desired_dimensions lengths differ"
            )
        if self.asset_category not in _VALID_OPTIONAL_ASSET_CATEGORIES:
            raise ValueError(f"Invalid asset_category: {self.asset_category}")
        if self.agent_type not in _VALID_OPTIONAL_ASSET_CATEGORIES:
            raise ValueError(f"Invalid legacy agent_type: {self.agent_type}")
        if self.object_type not in _VALID_OBJECT_TYPES:
            raise ValueError(f"Invalid object_type: {self.object_type}")
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir,
            "object_type": self.object_type,
            "object_descriptions": self.object_descriptions,
            "short_names": self.short_names,
            "desired_dimensions": self.desired_dimensions,
            "asset_category": self.asset_category,
            "agent_type": self.agent_type,
            "style_context": self.style_context,
            "operation_type": self.operation_type,
            "scene_id": self.scene_id,
        }


@dataclass
class AssetAcquisitionServerResponse:
    successful_assets: list[dict[str, Any]] = field(default_factory=list)
    failed_assets: list[dict[str, Any]] = field(default_factory=list)
    modification_info: dict[str, Any] | None = None

    @classmethod
    def from_assets(
        cls,
        assets: list[Any],
        failures: list[Any],
        modification_info: Any | None,
        scene_dir: Path | None = None,
    ) -> "AssetAcquisitionServerResponse":
        return cls(
            successful_assets=[asset.to_dict(scene_dir=scene_dir) for asset in assets],
            failed_assets=[
                {
                    "index": failure.index,
                    "description": failure.description,
                    "error_message": failure.error_message,
                }
                for failure in failures
            ],
            modification_info=(
                {
                    "original_description": modification_info.original_description,
                    "resulting_descriptions": modification_info.resulting_descriptions,
                    "discarded_manipulands": modification_info.discarded_manipulands,
                }
                if modification_info is not None
                else None
            ),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssetAcquisitionServerResponse":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "successful_assets": self.successful_assets,
            "failed_assets": self.failed_assets,
            "modification_info": self.modification_info,
        }


_VALID_ASSET_CATEGORIES = {
    "furniture",
    "wall_mounted",
    "ceiling_mounted",
    "manipuland",
}
_VALID_OPTIONAL_ASSET_CATEGORIES = _VALID_ASSET_CATEGORIES | {None}
_VALID_OBJECT_TYPES = _VALID_ASSET_CATEGORIES | {"thin_covering", "either"}
