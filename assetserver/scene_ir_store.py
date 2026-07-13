"""Persistent revision storage for normalized Scene IR documents."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from assetserver.asset_store import AssetStore, AssetStoreError
from assetserver.scene_ir import SceneIR, dump_scene_yaml, load_scene_yaml


class IRSceneNotFoundError(ValueError):
    pass


class IRSceneConflictError(ValueError):
    pass


class IRSceneAssetError(ValueError):
    pass


@dataclass(frozen=True)
class IRSceneRevision:
    scene_id: str
    revision: int
    sha256: str
    size_bytes: int


class IRSceneStore:
    def __init__(self, root: str | Path, asset_store: AssetStore) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.asset_store = asset_store
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def create(self, content: bytes) -> IRSceneRevision:
        scene = load_scene_yaml(content)
        scene_id = str(uuid.uuid4())
        scene = scene.model_copy(update={"scene_id": scene_id})
        self._validate_assets(scene)
        scene_dir = self.root / scene_id
        (scene_dir / "revisions").mkdir(parents=True)
        now = datetime.now(UTC).isoformat()
        info = self._write_revision(scene, 1)
        self._write_manifest(scene_id, {
            "scene_id": scene_id, "created_at": now, "updated_at": now,
            "latest_revision": 1, "schema_version": "scene-ir/v1",
        })
        self._write_current(scene_id, dump_scene_yaml(scene))
        return info

    def update(self, scene_id: str, content: bytes, *, base_revision: int) -> IRSceneRevision:
        with self._lock(scene_id):
            manifest = self._manifest(scene_id)
            latest = int(manifest["latest_revision"])
            if latest != base_revision:
                raise IRSceneConflictError(f"expected base revision {latest}, got {base_revision}")
            scene = load_scene_yaml(content)
            if scene.scene_id not in (None, scene_id):
                raise IRSceneConflictError("scene_id cannot be changed")
            scene = scene.model_copy(update={"scene_id": scene_id})
            self._validate_assets(scene)
            info = self._write_revision(scene, latest + 1)
            manifest["latest_revision"] = info.revision
            manifest["updated_at"] = datetime.now(UTC).isoformat()
            self._write_manifest(scene_id, manifest)
            self._write_current(scene_id, dump_scene_yaml(scene))
            return info

    def read(self, scene_id: str, revision: int | None = None) -> bytes:
        actual = revision or int(self._manifest(scene_id)["latest_revision"])
        path = self._scene_dir(scene_id) / "revisions" / f"{actual:06d}.yaml"
        if not path.is_file():
            raise IRSceneNotFoundError(f"scene revision not found: {actual}")
        return path.read_bytes()

    def revision(self, scene_id: str, revision: int | None = None) -> IRSceneRevision:
        actual = revision or int(self._manifest(scene_id)["latest_revision"])
        content = self.read(scene_id, actual)
        return IRSceneRevision(scene_id, actual, hashlib.sha256(content).hexdigest(), len(content))

    def _validate_assets(self, scene: SceneIR) -> None:
        for asset_ref in sorted(scene.asset_refs()):
            try:
                self.asset_store.resolve(asset_ref)
            except AssetStoreError as exc:
                raise IRSceneAssetError(str(exc)) from exc

    def _write_revision(self, scene: SceneIR, revision: int) -> IRSceneRevision:
        content = dump_scene_yaml(scene)
        path = self.root / scene.scene_id / "revisions" / f"{revision:06d}.yaml"
        path.write_bytes(content)
        return IRSceneRevision(scene.scene_id or "", revision, hashlib.sha256(content).hexdigest(), len(content))

    def _write_current(self, scene_id: str, content: bytes) -> None:
        temporary = self._scene_dir(scene_id) / f".current-{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(content)
        temporary.replace(self._scene_dir(scene_id) / "current.yaml")

    def _scene_dir(self, scene_id: str) -> Path:
        try:
            normalized = str(uuid.UUID(scene_id))
        except ValueError as exc:
            raise IRSceneNotFoundError(f"scene not found: {scene_id}") from exc
        path = self.root / normalized
        if not path.is_dir():
            raise IRSceneNotFoundError(f"scene not found: {scene_id}")
        return path

    def _manifest(self, scene_id: str) -> dict:
        path = self._scene_dir(scene_id) / "manifest.json"
        if not path.is_file():
            raise IRSceneNotFoundError(f"scene not found: {scene_id}")
        return json.loads(path.read_text())

    def _write_manifest(self, scene_id: str, manifest: dict) -> None:
        directory = self.root / scene_id
        temporary = directory / f".manifest-{uuid.uuid4().hex}.tmp"
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.replace(directory / "manifest.json")

    def _lock(self, scene_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(scene_id, threading.Lock())
