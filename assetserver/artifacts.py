"""Runtime registry for HTTP-downloadable model artifacts."""

from __future__ import annotations

import hashlib

from pathlib import Path


class ArtifactRegistry:
    """In-memory mapping from stable asset IDs to generated model files."""

    def __init__(self) -> None:
        self._paths: dict[str, Path] = {}

    def register(self, path: str | Path) -> dict[str, str]:
        resolved = Path(path).resolve()
        asset_id = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
        self._paths[asset_id] = resolved
        return {
            "asset_id": asset_id,
            "download_url": f"/assets/{asset_id}",
            "mesh_path": str(resolved),
        }

    def get(self, asset_id: str) -> Path | None:
        path = self._paths.get(asset_id)
        if path is None or not path.exists():
            return None
        return path


def attach_artifact_fields(data: dict, mesh_path_key: str = "mesh_path") -> dict:
    """Return a copy of a response dict with artifact fields when a path exists."""
    path = data.get(mesh_path_key)
    if not path:
        return data
    enriched = dict(data)
    enriched.update(GLOBAL_ARTIFACTS.register(path))
    return enriched


GLOBAL_ARTIFACTS = ArtifactRegistry()


def artifact_media_type(path: Path) -> str:
    """Return a reasonable media type for a registered artifact."""
    suffix = path.suffix.lower()
    if suffix == ".glb":
        return "model/gltf-binary"
    if suffix == ".gltf":
        return "model/gltf+json"
    if suffix == ".obj":
        return "model/obj"
    if suffix == ".stl":
        return "model/stl"
    return "application/octet-stream"
