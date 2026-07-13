"""Persistent, revisioned SDF scene storage."""

import hashlib
import io
import json
import shutil
import stat
import threading
import uuid
import zipfile

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import urlparse
from xml.etree import ElementTree


class SceneError(Exception):
    """Base error for scene operations."""


class SceneNotFoundError(SceneError):
    """The requested scene or revision does not exist."""


class SceneConflictError(SceneError):
    """The submitted base revision is stale."""


class ScenePackageError(SceneError):
    """A scene package or SDF document is invalid."""


@dataclass(frozen=True)
class SceneRevision:
    scene_id: str
    revision: int
    sha256: str
    size_bytes: int


class SceneStore:
    _RESOURCE_ELEMENTS = {
        "uri",
        "albedo_map",
        "normal_map",
        "roughness_map",
        "metalness_map",
        "emissive_map",
        "environment_map",
        "light_map",
    }
    _UNSUPPORTED_STATIC_ELEMENTS = {"plugin", "joint", "include"}

    def __init__(
        self,
        root: str | Path,
        *,
        max_package_bytes: int = 2 * 1024**3,
        max_sdf_bytes: int = 10 * 1024**2,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_package_bytes = max_package_bytes
        self.max_sdf_bytes = max_sdf_bytes
        self._locks_guard = threading.Lock()
        self._scene_locks: dict[str, threading.Lock] = {}

    def create(self, package: bytes | BinaryIO) -> SceneRevision:
        package_file, package_size = self._package_file(package)
        if package_size > self.max_package_bytes:
            raise ScenePackageError("scene package is too large")
        scene_id = str(uuid.uuid4())
        scene_dir = self.root / scene_id
        assets_dir = scene_dir / "assets"
        revisions_dir = scene_dir / "revisions"
        try:
            assets_dir.mkdir(parents=True)
            revisions_dir.mkdir()
            sdf = self._extract_package(package_file, assets_dir)
            self._validate_sdf(sdf, assets_dir)
            revision = self._write_revision(scene_id, 1, sdf)
            now = datetime.now(UTC).isoformat()
            self._write_manifest(
                scene_id,
                {
                    "scene_id": scene_id,
                    "created_at": now,
                    "updated_at": now,
                    "latest_revision": 1,
                },
            )
            return revision
        except Exception:
            shutil.rmtree(scene_dir, ignore_errors=True)
            raise

    def update_sdf(
        self, scene_id: str, sdf: bytes, *, base_revision: int
    ) -> SceneRevision:
        with self._scene_lock(scene_id):
            manifest = self._manifest(scene_id)
            latest = int(manifest["latest_revision"])
            if base_revision != latest:
                raise SceneConflictError(
                    f"expected base revision {latest}, got {base_revision}"
                )
            self._validate_sdf(sdf, self._scene_dir(scene_id) / "assets")
            revision = self._write_revision(scene_id, latest + 1, sdf)
            manifest["latest_revision"] = revision.revision
            manifest["updated_at"] = datetime.now(UTC).isoformat()
            self._write_manifest(scene_id, manifest)
            return revision

    def latest_revision(self, scene_id: str) -> int:
        return int(self._manifest(scene_id)["latest_revision"])

    def read_sdf(self, scene_id: str, revision: int | None = None) -> bytes:
        revision = revision or self.latest_revision(scene_id)
        path = self._scene_dir(scene_id) / "revisions" / f"{revision:06d}.sdf"
        if not path.is_file():
            raise SceneNotFoundError(f"scene revision not found: {revision}")
        return path.read_bytes()

    def revision(self, scene_id: str, revision: int | None = None) -> SceneRevision:
        actual = revision or self.latest_revision(scene_id)
        sdf = self.read_sdf(scene_id, actual)
        return SceneRevision(
            scene_id, actual, hashlib.sha256(sdf).hexdigest(), len(sdf)
        )

    def build_package(self, scene_id: str, revision: int | None = None) -> bytes:
        sdf = self.read_sdf(scene_id, revision)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("scene.sdf", sdf)
            assets_dir = self._scene_dir(scene_id) / "assets"
            for path in sorted(assets_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(assets_dir).as_posix())
        return output.getvalue()

    def _extract_package(self, package: BinaryIO, assets_dir: Path) -> bytes:
        try:
            archive = zipfile.ZipFile(package)
        except zipfile.BadZipFile as exc:
            raise ScenePackageError("invalid ZIP scene package") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > 10_000:
                raise ScenePackageError("scene package contains too many entries")
            if sum(info.file_size for info in infos) > self.max_package_bytes:
                raise ScenePackageError("scene package uncompressed size is too large")
            scene_entries = []
            seen_paths: set[PurePosixPath] = set()
            for info in infos:
                path = PurePosixPath(info.filename)
                if "\\" in info.filename or path.is_absolute() or ".." in path.parts:
                    raise ScenePackageError(f"unsafe archive path: {info.filename}")
                if path in seen_paths:
                    raise ScenePackageError(f"duplicate archive path: {info.filename}")
                seen_paths.add(path)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ScenePackageError(
                        f"archive symlink is not allowed: {info.filename}"
                    )
                if info.is_dir():
                    continue
                if path == PurePosixPath("scene.sdf"):
                    scene_entries.append(info)
                    continue
                destination = assets_dir.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
            if len(scene_entries) != 1:
                raise ScenePackageError(
                    "package must contain exactly one root scene.sdf"
                )
            with archive.open(scene_entries[0]) as source:
                return source.read(self.max_sdf_bytes + 1)

    def _validate_sdf(self, sdf: bytes, assets_dir: Path) -> None:
        if len(sdf) > self.max_sdf_bytes:
            raise ScenePackageError("SDF is too large")
        try:
            root = ElementTree.fromstring(sdf)
        except ElementTree.ParseError as exc:
            raise ScenePackageError(f"invalid SDF XML: {exc}") from exc
        if root.tag.rsplit("}", 1)[-1] != "sdf":
            raise ScenePackageError("root XML element must be sdf")
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag in self._UNSUPPORTED_STATIC_ELEMENTS:
                raise ScenePackageError(f"unsupported static SDF element: {tag}")
            if tag not in self._RESOURCE_ELEMENTS or not element.text:
                continue
            uri = element.text.strip()
            parsed = urlparse(uri)
            path = PurePosixPath(uri)
            if parsed.scheme or path.is_absolute() or ".." in path.parts:
                raise ScenePackageError(f"unsupported asset URI: {uri}")
            asset = assets_dir.joinpath(*path.parts)
            if not asset.is_file():
                raise ScenePackageError(f"unresolved asset: {uri}")

    def _write_revision(
        self, scene_id: str, revision: int, sdf: bytes
    ) -> SceneRevision:
        path = self._scene_dir(scene_id) / "revisions" / f"{revision:06d}.sdf"
        with path.open("xb") as output:
            output.write(sdf)
        return SceneRevision(
            scene_id, revision, hashlib.sha256(sdf).hexdigest(), len(sdf)
        )

    def _scene_dir(self, scene_id: str) -> Path:
        try:
            normalized = str(uuid.UUID(scene_id))
        except ValueError as exc:
            raise SceneNotFoundError(f"scene not found: {scene_id}") from exc
        path = self.root / normalized
        if not path.is_dir():
            raise SceneNotFoundError(f"scene not found: {scene_id}")
        return path

    def _manifest(self, scene_id: str) -> dict:
        path = self._scene_dir(scene_id) / "manifest.json"
        if not path.is_file():
            raise SceneNotFoundError(f"scene not found: {scene_id}")
        return json.loads(path.read_text())

    def _write_manifest(self, scene_id: str, manifest: dict) -> None:
        scene_dir = self.root / scene_id
        temporary = scene_dir / f".manifest-{uuid.uuid4()}.json.tmp"
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.replace(scene_dir / "manifest.json")

    def _scene_lock(self, scene_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._scene_locks.setdefault(scene_id, threading.Lock())

    @staticmethod
    def _package_file(package: bytes | BinaryIO) -> tuple[BinaryIO, int]:
        if isinstance(package, bytes):
            return io.BytesIO(package), len(package)
        try:
            current = package.tell()
            package.seek(0, io.SEEK_END)
            size = package.tell()
            package.seek(current)
        except (AttributeError, OSError) as exc:
            raise ScenePackageError("scene package must be seekable") from exc
        return package, size
