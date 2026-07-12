"""Stable asset catalog and deterministic ZIP delivery cache."""

from __future__ import annotations

import hashlib
import json
import threading
import zipfile

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AssetDescriptor:
    source: str
    resource_key: str
    root: Path
    files: tuple[Path, ...]
    metadata: dict


@dataclass(frozen=True)
class PackagedAsset:
    asset_id: str
    path: Path
    size_bytes: int
    sha256: str


def stable_asset_id(source: str, resource_key: str) -> str:
    return hashlib.sha256(f"{source}:{resource_key}".encode()).hexdigest()[:32]


class AssetCatalog:
    def __init__(self, cache_root: str | Path) -> None:
        self.cache_root = Path(cache_root)
        self._assets: dict[str, AssetDescriptor] = {}
        self._locks: dict[str, threading.Lock] = {}

    def register(self, descriptor: AssetDescriptor) -> str:
        asset_id = stable_asset_id(descriptor.source, descriptor.resource_key)
        self._assets[asset_id] = descriptor
        self._locks.setdefault(asset_id, threading.Lock())
        return asset_id

    def descriptor(self, asset_id: str) -> AssetDescriptor | None:
        return self._assets.get(asset_id)

    def package(self, asset_id: str) -> PackagedAsset:
        descriptor = self._assets.get(asset_id)
        if descriptor is None:
            raise KeyError(asset_id)
        with self._locks[asset_id]:
            self.cache_root.mkdir(parents=True, exist_ok=True)
            output = self.cache_root / f"{asset_id}.zip"
            if not output.is_file():
                part = output.with_suffix(".zip.part")
                try:
                    self._write_zip(part, descriptor)
                    part.replace(output)
                finally:
                    part.unlink(missing_ok=True)
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            return PackagedAsset(asset_id, output, output.stat().st_size, digest)

    def _write_zip(self, output: Path, descriptor: AssetDescriptor) -> None:
        root = descriptor.root.resolve()
        entries = []
        for path in sorted(descriptor.files, key=lambda item: item.as_posix()):
            resolved = path.resolve()
            if root != resolved and root not in resolved.parents:
                raise RuntimeError(f"asset file escapes dataset root: {path}")
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"invalid asset file: {path}")
            relative = resolved.relative_to(root).as_posix()
            entries.append(
                {
                    "path": relative,
                    "size_bytes": resolved.stat().st_size,
                    "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
                }
            )
        manifest = {
            "source": descriptor.source,
            "resource_key": descriptor.resource_key,
            "metadata": descriptor.metadata,
            "files": entries,
        }
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            self._write_entry(
                archive,
                "manifest.json",
                (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
            )
            for path, entry in zip(descriptor.files, entries, strict=True):
                self._write_entry(archive, entry["path"], path.read_bytes())

    @staticmethod
    def _write_entry(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o100644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, content)
