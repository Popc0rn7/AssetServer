"""Persistent filesystem-backed SAM3D assets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Asset:
    asset_id: str
    path: Path
    sha256: str
    size_bytes: int


class Sam3DArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, source: Path, metadata: dict) -> Asset:
        asset_id = str(uuid.uuid4())
        staging = self.root / f".{asset_id}.tmp"
        destination = self.root / asset_id
        staging.mkdir()
        model = staging / "model.glb"
        shutil.move(str(source), model)
        digest = hashlib.sha256(model.read_bytes()).hexdigest()
        size_bytes = model.stat().st_size
        stored = {
            **metadata,
            "asset_id": asset_id,
            "sha256": digest,
            "size_bytes": size_bytes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (staging / "metadata.json").write_text(json.dumps(stored, indent=2) + "\n")
        os.replace(staging, destination)
        return Asset(asset_id, destination / "model.glb", digest, size_bytes)

    def get(self, asset_id: str) -> Asset | None:
        try:
            parsed = str(uuid.UUID(asset_id))
        except ValueError:
            return None
        path = self.root / parsed / "model.glb"
        metadata_path = path.parent / "metadata.json"
        if not path.is_file() or not metadata_path.is_file():
            return None
        metadata = json.loads(metadata_path.read_text())
        return Asset(parsed, path, metadata["sha256"], metadata["size_bytes"])


# Import compatibility for the v1 standalone backend.
AssetStore = Sam3DArtifactStore
