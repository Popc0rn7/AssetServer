"""Immutable content-addressed storage for canonical 3D assets.

``ContentAddressedAssetStore`` is the production ``asset/v2`` API.  Its ingest
boundary deliberately requires an explicit source coordinate frame.  The
``AssetStore`` subclass is kept as the read/write compatibility surface used by
the pre-P1 API; it treats undeclared legacy inputs as already canonical.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
import xml.etree.ElementTree as ET

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO


ASSET_SCHEMA_VERSION = "asset/v2"
ASSET_REF_PREFIX = "asset://sha256/"
IDENTITY_MATRIX = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]


class AssetStoreError(ValueError):
    pass


@dataclass(frozen=True)
class StoredAsset:
    asset_ref: str
    digest: str
    root: Path
    manifest: dict[str, Any]


def canonical_source_frame() -> dict[str, Any]:
    """Return an explicit declaration for an already canonical source."""
    return {
        "units": "m",
        "handedness": "right",
        "up_axis": "+Z",
        "origin": "ground-center",
        "transform_to_asset": [row[:] for row in IDENTITY_MATRIX],
    }


def _matrix(value: Any, field: str) -> list[list[float]]:
    if value is None:
        value = IDENTITY_MATRIX
    if isinstance(value, (list, tuple)) and len(value) == 16:
        value = [value[index : index + 4] for index in range(0, 16, 4)]
    if not (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(row, (list, tuple)) and len(row) == 4 for row in value)
    ):
        raise AssetStoreError(f"{field} must be a 4x4 matrix")
    output = [[float(item) for item in row] for row in value]
    if not all(math.isfinite(item) for row in output for item in row):
        raise AssetStoreError(f"{field} must contain finite values")
    if any(abs(output[3][index] - expected) > 1e-9 for index, expected in enumerate((0, 0, 0, 1))):
        raise AssetStoreError(f"{field} must be an affine transform")
    # A singular linear transform cannot define a coordinate frame.
    a = output
    determinant = (
        a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
        - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
        + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
    )
    if abs(determinant) < 1e-12:
        raise AssetStoreError(f"{field} must be invertible")
    return output


def _entry(value: str | dict[str, Any] | None, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    record = {"entrypoint": value} if isinstance(value, str) else dict(value)
    entrypoint = record.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint:
        raise AssetStoreError(f"{field}.entrypoint is required")
    record["entrypoint"] = _safe_name(entrypoint)
    record["transform_to_asset"] = _matrix(
        record.get("transform_to_asset"), f"{field}.transform_to_asset"
    )
    return record


def _safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise AssetStoreError(f"unsafe asset path: {name}")
    return path.as_posix()


class ContentAddressedAssetStore:
    """Strict ``asset/v2`` store with validation before atomic publication."""

    def __init__(self, root: str | Path = "data/assets") -> None:
        self.root = Path(root)

    def ingest(
        self,
        files: dict[str, bytes | BinaryIO],
        *,
        visual: str | dict[str, Any] | None = None,
        simulation: str | dict[str, Any] | None = None,
        collision: str | dict[str, Any] | list[dict[str, Any]] | None = None,
        bounds: dict[str, Any] | None = None,
        joints: list[dict[str, Any]] | None = None,
        support_surfaces: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        source_frame: dict[str, Any] | None = None,
        license: dict[str, Any] | str | None = None,
        tool_versions: dict[str, str] | None = None,
        parent: dict[str, Any] | None = None,
        kind: str = "object",
        preview: str | None = None,
    ) -> StoredAsset:
        normalized = self._normalize_files(files)
        source_record = dict(source or {})
        frame = source_frame or source_record.pop("frame", None)
        if frame is None:
            raise AssetStoreError("source frame declaration is required for asset/v2 ingest")
        frame_record = self._validate_frame(frame)
        visual_record = _entry(visual, "visual")
        if kind not in {"object", "material"}:
            raise AssetStoreError("asset kind must be object or material")
        if kind == "object" and visual_record is None:
            raise AssetStoreError("object assets require a visual entrypoint")
        if kind == "material" and (visual_record is not None or simulation is not None):
            raise AssetStoreError("material assets cannot declare object entrypoints")
        simulation_record = _entry(simulation, "simulation")
        collision_records = self._collision_entries(collision)
        if visual_record is not None and (
            isinstance(visual, str) or "transform_to_asset" not in visual
        ):
            visual_record["transform_to_asset"] = frame_record["transform_to_asset"]
        if simulation_record is not None and (
            isinstance(simulation, str) or "transform_to_asset" not in simulation
        ):
            simulation_record["transform_to_asset"] = frame_record["transform_to_asset"]
        for field, record in (
            ("visual", visual_record),
            ("simulation", simulation_record),
            *(("collision", item) for item in collision_records),
        ):
            if record and record["entrypoint"] not in normalized:
                raise AssetStoreError(f"{field} entrypoint is missing")
        if visual_record is not None:
            parts = visual_record.get("parts") or []
            if not isinstance(parts, list):
                raise AssetStoreError("visual.parts must be a list")
            normalized_parts = []
            for index, part in enumerate(parts):
                if not isinstance(part, dict):
                    raise AssetStoreError(f"visual.parts[{index}] must be a mapping")
                item = dict(part)
                link = item.get("link")
                entrypoint = item.get("entrypoint")
                if not isinstance(link, str) or not link:
                    raise AssetStoreError(f"visual.parts[{index}].link is required")
                if not isinstance(entrypoint, str):
                    raise AssetStoreError(
                        f"visual.parts[{index}].entrypoint is required"
                    )
                item["entrypoint"] = _safe_name(entrypoint)
                if item["entrypoint"] not in normalized:
                    raise AssetStoreError(
                        f"visual.parts[{index}] entrypoint is missing"
                    )
                normalized_parts.append(item)
            visual_record["parts"] = normalized_parts
        if preview is not None:
            preview = _safe_name(preview)
            if preview not in normalized:
                raise AssetStoreError("preview entrypoint is missing")
            if Path(preview).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise AssetStoreError("preview must be PNG, JPEG, or WebP")
        self._validate_payloads(normalized, visual_record, simulation_record)

        bounds_record = self._validate_bounds(bounds)
        joints_record = self._validate_joints(joints or [])
        surfaces_record = self._validate_surfaces(support_surfaces or [])
        if simulation_record is not None:
            base_link = simulation_record.get("base_link")
            if not isinstance(base_link, str) or not base_link:
                raise AssetStoreError("simulation.base_link is required")
            self._validate_model_declarations(
                normalized, simulation_record, joints_record
            )

        file_records = [
            {
                "path": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in sorted(normalized.items())
        ]
        identity: dict[str, Any] = {
            "schema_version": ASSET_SCHEMA_VERSION,
            "kind": kind,
            "canonical_frame": {
                "units": "m",
                "handedness": "right",
                "up_axis": "+Z",
                "origin": "ground-center",
            },
            "source": {**source_record, "frame": frame_record},
            "provenance": dict(source_record.get("provenance") or {}),
            "visual": visual_record,
            "simulation": simulation_record,
            "collision": collision_records,
            "bounds": bounds_record,
            "joints": joints_record,
            "support_surfaces": surfaces_record,
            "license": ({"name": license} if isinstance(license, str) else dict(license or {})),
            "tool_versions": {str(k): str(v) for k, v in sorted((tool_versions or {}).items())},
            "metadata": dict(metadata or {}),
            "preview": preview,
            "files": file_records,
        }
        if parent is not None:
            identity["parent"] = self._validate_parent(parent)
        digest = hashlib.sha256(_canonical_json(identity)).hexdigest()
        destination = self._digest_root(digest)
        if destination.is_dir():
            return self.resolve(f"{ASSET_REF_PREFIX}{digest}")

        temporary = destination.parent / f".{digest}-{uuid.uuid4().hex}.tmp"
        try:
            (temporary / "files").mkdir(parents=True)
            for name, content in normalized.items():
                target = temporary / "files" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            manifest = {**identity, "digest": digest}
            (temporary / "manifest.json").write_bytes(
                json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False).encode() + b"\n"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(temporary, destination)
            except OSError:
                if not destination.is_dir():
                    raise
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return self.resolve(f"{ASSET_REF_PREFIX}{digest}")

    def resolve(self, asset_ref: str) -> StoredAsset:
        digest = self._parse_ref(asset_ref)
        root = self._digest_root(digest)
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise AssetStoreError(f"asset not found: {asset_ref}")
        try:
            stored_manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError) as exc:
            raise AssetStoreError("invalid asset manifest") from exc
        if stored_manifest.get("digest") != digest:
            raise AssetStoreError("asset manifest digest mismatch")
        if stored_manifest.get("schema_version") == ASSET_SCHEMA_VERSION:
            identity = {key: value for key, value in stored_manifest.items() if key != "digest"}
            if hashlib.sha256(_canonical_json(identity)).hexdigest() != digest:
                raise AssetStoreError("asset manifest identity verification failed")
        manifest = (
            self._v1_view(stored_manifest)
            if stored_manifest.get("schema_version") == "asset/v1"
            else stored_manifest
        )
        if manifest.get("schema_version") != ASSET_SCHEMA_VERSION:
            raise AssetStoreError("unsupported asset manifest schema")
        self._verify_files(root, manifest.get("files"))
        return StoredAsset(asset_ref, digest, root, manifest)

    def entrypoint(self, asset_ref: str, kind: str) -> Path:
        asset = self.resolve(asset_ref)
        value = asset.manifest.get(kind)
        if isinstance(value, list):
            value = value[0] if value else None
        relative = value.get("entrypoint") if isinstance(value, dict) else value
        if not relative:
            raise AssetStoreError(f"asset has no {kind} entrypoint")
        return self.file_path(asset.root, relative)

    def preview_path(self, asset_ref: str) -> Path | None:
        asset = self.resolve(asset_ref)
        preview = asset.manifest.get("preview") or asset.manifest.get("metadata", {}).get("preview")
        if not isinstance(preview, str):
            return None
        path = self.file_path(asset.root, preview)
        return path if path.is_file() else None

    @staticmethod
    def file_path(root: Path, relative: str) -> Path:
        return root.joinpath("files", *_safe_name(relative).split("/"))

    def _digest_root(self, digest: str) -> Path:
        return self.root / "sha256" / digest[:2] / digest

    @staticmethod
    def _parse_ref(asset_ref: str) -> str:
        if not isinstance(asset_ref, str) or not asset_ref.startswith(ASSET_REF_PREFIX):
            raise AssetStoreError("unsupported asset reference")
        digest = asset_ref.removeprefix(ASSET_REF_PREFIX)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise AssetStoreError("invalid asset digest")
        return digest

    @staticmethod
    def _normalize_files(files: dict[str, bytes | BinaryIO]) -> dict[str, bytes]:
        if not files:
            raise AssetStoreError("asset must contain at least one file")
        output: dict[str, bytes] = {}
        for name, value in files.items():
            safe = _safe_name(name)
            content = value if isinstance(value, bytes) else value.read()
            if not isinstance(content, bytes):
                raise AssetStoreError(f"asset file is not binary: {name}")
            output[safe] = content
        return output

    @staticmethod
    def _validate_payloads(
        files: dict[str, bytes],
        visual: dict[str, Any] | None,
        simulation: dict[str, Any] | None,
    ) -> None:
        if visual:
            name = visual["entrypoint"]
            suffix = Path(name).suffix.lower()
            if suffix == ".glb" and not files[name].startswith(b"glTF"):
                raise AssetStoreError("visual GLB is not a valid glTF binary")
            if suffix not in {".glb", ".gltf", ".obj"}:
                raise AssetStoreError(f"unsupported visual format: {suffix}")
        if simulation:
            name = simulation["entrypoint"]
            suffix = Path(name).suffix.lower()
            if suffix not in {".sdf", ".urdf"}:
                raise AssetStoreError(f"unsupported simulation format: {suffix}")
            try:
                root = ET.fromstring(files[name])
            except ET.ParseError as exc:
                raise AssetStoreError("simulation entrypoint is not valid XML") from exc
            expected = "sdf" if suffix == ".sdf" else "robot"
            if root.tag != expected:
                raise AssetStoreError(f"simulation {suffix} root must be <{expected}>")

    @staticmethod
    def _validate_model_declarations(
        files: dict[str, bytes],
        simulation: dict[str, Any],
        joints: list[dict[str, Any]],
    ) -> None:
        matrix = simulation["transform_to_asset"]
        columns = [[matrix[row][column] for row in range(3)] for column in range(3)]
        if any(
            abs(sum(a * b for a, b in zip(columns[i], columns[j], strict=True)) - (1.0 if i == j else 0.0))
            > 1e-6
            for i in range(3)
            for j in range(3)
        ):
            raise AssetStoreError(
                "simulation.transform_to_asset must be rigid; encode unit scale in SDF/URDF"
            )
        root = ET.fromstring(files[simulation["entrypoint"]])
        links = {item.get("name") for item in root.iter("link")}
        if simulation["base_link"] not in links:
            raise AssetStoreError(
                f"simulation base link does not exist: {simulation['base_link']}"
            )
        model_joints = {item.get("name") for item in root.iter("joint")}
        missing = sorted(item["name"] for item in joints if item["name"] not in model_joints)
        if missing:
            raise AssetStoreError(f"manifest joints do not exist in simulation: {missing}")

    @staticmethod
    def _validate_frame(frame: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(frame, dict):
            raise AssetStoreError("source frame must be an object")
        units = frame.get("units")
        up_axis = frame.get("up_axis")
        origin = frame.get("origin")
        handedness = frame.get("handedness", "right")
        if units not in {"m", "cm", "mm"}:
            raise AssetStoreError("source frame units must be m, cm, or mm")
        if up_axis not in {"+X", "-X", "+Y", "-Y", "+Z", "-Z"}:
            raise AssetStoreError("source frame up_axis is invalid")
        if not isinstance(origin, str) or not origin:
            raise AssetStoreError("source frame origin is required")
        if handedness not in {"right", "left"}:
            raise AssetStoreError("source frame handedness is invalid")
        return {
            "units": units,
            "handedness": handedness,
            "up_axis": up_axis,
            "origin": origin,
            "transform_to_asset": _matrix(frame.get("transform_to_asset"), "source.frame.transform_to_asset"),
        }

    @staticmethod
    def _validate_bounds(bounds: dict[str, Any] | None) -> dict[str, list[float]]:
        bounds = dict(bounds or {"min": [0, 0, 0], "max": [0, 0, 0]})
        minimum = bounds.get("min")
        maximum = bounds.get("max")
        if not (
            isinstance(minimum, (list, tuple))
            and isinstance(maximum, (list, tuple))
            and len(minimum) == len(maximum) == 3
        ):
            raise AssetStoreError("bounds min/max must contain three values")
        result = {"min": [float(v) for v in minimum], "max": [float(v) for v in maximum]}
        if not all(math.isfinite(v) for values in result.values() for v in values):
            raise AssetStoreError("bounds must be finite")
        if any(lo > hi for lo, hi in zip(result["min"], result["max"], strict=True)):
            raise AssetStoreError("bounds min must not exceed max")
        return result

    @staticmethod
    def _validate_joints(joints: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        names: set[str] = set()
        for joint in joints:
            item = dict(joint)
            name = item.get("name")
            if not isinstance(name, str) or not name or name in names:
                raise AssetStoreError("joint names must be non-empty and unique")
            names.add(name)
            limits = item.get("limits")
            if limits is not None:
                if not isinstance(limits, dict) or not all(key in limits for key in ("lower", "upper")):
                    raise AssetStoreError(f"joint {name} limits require lower and upper")
                lower, upper = float(limits["lower"]), float(limits["upper"])
                if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
                    raise AssetStoreError(f"joint {name} limits are invalid")
                item["limits"] = {"lower": lower, "upper": upper}
            output.append(item)
        return sorted(output, key=lambda item: item["name"])

    @staticmethod
    def _validate_surfaces(surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        names: set[str] = set()
        for surface in surfaces:
            item = dict(surface)
            name = item.get("name")
            if not isinstance(name, str) or not name or name in names:
                raise AssetStoreError("support surface names must be non-empty and unique")
            names.add(name)
            output.append(item)
        return sorted(output, key=lambda item: item["name"])

    @staticmethod
    def _collision_entries(value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        return [_entry(item, "collision") for item in values]  # type: ignore[list-item]

    @staticmethod
    def _validate_parent(parent: dict[str, Any]) -> dict[str, Any]:
        item = dict(parent)
        ref = item.get("asset_ref")
        if not isinstance(ref, str) or not ref.startswith(ASSET_REF_PREFIX):
            raise AssetStoreError("parent.asset_ref is invalid")
        if not isinstance(item.get("operation"), str) or not item["operation"]:
            raise AssetStoreError("parent.operation is required")
        if not isinstance(item.get("operation_version"), str) or not item["operation_version"]:
            raise AssetStoreError("parent.operation_version is required")
        return item

    @staticmethod
    def _verify_files(root: Path, records: Any) -> None:
        if not isinstance(records, list) or not records:
            raise AssetStoreError("asset manifest has no files")
        for record in records:
            try:
                path = ContentAddressedAssetStore.file_path(root, record["path"])
                valid = (
                    path.is_file()
                    and path.stat().st_size == int(record["size_bytes"])
                    and hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
                )
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                raise AssetStoreError(f"asset file verification failed: {record.get('path', '?')}")

    @staticmethod
    def _v1_view(manifest: dict[str, Any]) -> dict[str, Any]:
        """Convert v1 to an in-memory v2 view without mutating the digest."""
        metadata = dict(manifest.get("metadata") or {})
        simulation = manifest.get("simulation")
        return {
            "schema_version": ASSET_SCHEMA_VERSION,
            "legacy_schema_version": "asset/v1",
            "digest": manifest.get("digest"),
            "canonical_frame": {
                "units": "m",
                "handedness": "right",
                "up_axis": "+Z",
                "origin": "ground-center",
            },
            "source": {**dict(manifest.get("source") or {}), "frame": canonical_source_frame()},
            "provenance": {},
            "visual": _entry(manifest.get("visual"), "visual"),
            "simulation": (
                {**(_entry(simulation, "simulation") or {}), "base_link": metadata.get("base_link", "base")}
                if simulation
                else None
            ),
            "collision": [],
            "bounds": metadata.get("bounds", {"min": [0.0] * 3, "max": [0.0] * 3}),
            "joints": metadata.get("joints", []),
            "support_surfaces": metadata.get("support_surfaces", []),
            "license": metadata.get("license", {}),
            "tool_versions": {},
            "metadata": metadata,
            "files": manifest.get("files", []),
        }


class AssetStore(ContentAddressedAssetStore):
    """Compatibility alias for callers written before the strict P1 boundary."""

    def ingest(self, files, **kwargs):  # type: ignore[override]
        source = dict(kwargs.get("source") or {})
        if kwargs.get("source_frame") is None and "frame" not in source:
            kwargs["source_frame"] = canonical_source_frame()
        simulation = kwargs.get("simulation")
        if isinstance(simulation, str):
            kwargs["simulation"] = {"entrypoint": simulation, "base_link": "base"}
        return super().ingest(files, **kwargs)

    @staticmethod
    def _validate_payloads(files, visual, simulation) -> None:
        # Legacy tests and digest directories may contain placeholder fixtures.
        return None

    @staticmethod
    def _validate_model_declarations(files, simulation, joints) -> None:
        return None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
