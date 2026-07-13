"""Persistent content-addressed storage for internal 3D assets."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class AssetStoreError(ValueError):
    pass


@dataclass(frozen=True)
class StoredAsset:
    asset_ref: str
    digest: str
    root: Path
    manifest: dict


class AssetStore:
    def __init__(self, root: str | Path = "data/assets") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def ingest(
        self,
        files: dict[str, bytes | BinaryIO],
        *,
        visual: str,
        simulation: str | None = None,
        metadata: dict | None = None,
        source: dict | None = None,
    ) -> StoredAsset:
        normalized = self._normalize_files(files)
        if visual not in normalized:
            raise AssetStoreError("visual entrypoint is missing")
        if simulation is not None and simulation not in normalized:
            raise AssetStoreError("simulation entrypoint is missing")
        file_records = [
            {
                "path": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in sorted(normalized.items())
        ]
        identity = {
            "files": file_records,
            "visual": visual,
            "simulation": simulation,
            "metadata": metadata or {},
            "source": source or {},
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        destination = self._digest_root(digest)
        if destination.is_dir():
            return self.resolve(f"asset://sha256/{digest}")

        temporary = destination.parent / f".{digest}-{uuid.uuid4().hex}.tmp"
        try:
            (temporary / "files").mkdir(parents=True)
            for name, content in normalized.items():
                target = temporary / "files" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            manifest = {"schema_version": "asset/v1", "digest": digest, **identity}
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                temporary.replace(destination)
            except FileExistsError:
                pass
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return self.resolve(f"asset://sha256/{digest}")

    def resolve(self, asset_ref: str) -> StoredAsset:
        prefix = "asset://sha256/"
        if not asset_ref.startswith(prefix):
            raise AssetStoreError("unsupported asset reference")
        digest = asset_ref.removeprefix(prefix)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise AssetStoreError("invalid asset digest")
        root = self._digest_root(digest)
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise AssetStoreError(f"asset not found: {asset_ref}")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("digest") != digest:
            raise AssetStoreError("asset manifest digest mismatch")
        for record in manifest.get("files", []):
            path = self.file_path(root, record["path"])
            if (
                not path.is_file()
                or path.stat().st_size != record["size_bytes"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]
            ):
                raise AssetStoreError(f"asset file verification failed: {record['path']}")
        return StoredAsset(asset_ref, digest, root, manifest)

    def entrypoint(self, asset_ref: str, kind: str) -> Path:
        asset = self.resolve(asset_ref)
        relative = asset.manifest.get(kind)
        if not relative:
            raise AssetStoreError(f"asset has no {kind} entrypoint")
        return self.file_path(asset.root, relative)

    @staticmethod
    def file_path(root: Path, relative: str) -> Path:
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            raise AssetStoreError(f"unsafe asset path: {relative}")
        return root.joinpath("files", *path.parts)

    def _digest_root(self, digest: str) -> Path:
        return self.root / "sha256" / digest[:2] / digest

    @staticmethod
    def _normalize_files(files: dict[str, bytes | BinaryIO]) -> dict[str, bytes]:
        if not files:
            raise AssetStoreError("asset must contain at least one file")
        output: dict[str, bytes] = {}
        for name, value in files.items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise AssetStoreError(f"unsafe asset path: {name}")
            output[path.as_posix()] = value if isinstance(value, bytes) else value.read()
        return output
