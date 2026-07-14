"""Gateway-side collision readiness, caching, SDF rewriting, and publication."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import os
import posixpath
import time
import uuid
import xml.etree.ElementTree as ET

from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import numpy as np
import trimesh

from assetserver.artifacts import GLOBAL_ARTIFACTS
from assetserver.asset_store import ContentAddressedAssetStore, StoredAsset
from assetserver.postprocess.config import (
    PostprocessConfig,
    artifact_key,
    derivation_key,
    profile_sha256,
)
from assetserver.postprocess.store import DerivationStore
from assetserver.postprocess_server.client import (
    PostprocessClient,
    PostprocessClientError,
)


COLLISION_PIPELINE_VERSION = "assetserver-collision/v1"
SDF_GENERATOR_VERSION = "assetserver-sdf/v1"
DRAKE_NAMESPACE = "drake.mit.edu"
ET.register_namespace("drake", DRAKE_NAMESPACE)


class CollisionPostprocessError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = 503 if retryable else 422


class CollisionPostprocessor:
    """Publish engine-neutral simulation collision while keeping the parent immutable."""

    def __init__(
        self,
        assets: ContentAddressedAssetStore,
        config: PostprocessConfig,
        *,
        client: PostprocessClient | None = None,
        store: DerivationStore | None = None,
        coacd_version: str | None = None,
    ) -> None:
        self.assets = assets
        self.config = config
        self.client = client or PostprocessClient(config.url, config.timeout_s)
        self.store = store or DerivationStore(config.database)
        self.coacd_version = coacd_version or _package_version("coacd")

    async def ensure_simulation_ready(self, asset_ref: str) -> StoredAsset:
        parent = self.assets.resolve(asset_ref)
        if parent.manifest.get("kind") == "material":
            return parent
        readiness = _readiness(parent, self.config.profile, self.coacd_version)
        if readiness == "ready":
            return parent
        if readiness != "rigid-triangle":
            raise _invalid(readiness)

        visual = parent.manifest["visual"]
        visual_record = _file_record(parent, visual["entrypoint"])
        akey = artifact_key(
            visual_record["sha256"],
            {
                **parent.manifest["canonical_frame"],
                "visual_transform_to_asset": visual["transform_to_asset"],
            },
            self.config.profile,
            self.coacd_version,
        )
        dkey = derivation_key(parent.digest, akey, SDF_GENERATOR_VERSION)
        cached = self.store.get_derivation(dkey)
        if cached:
            try:
                return self.assets.resolve(cached)
            except Exception:
                self.store.delete_derivation(dkey)

        manifest = await self._ensure_artifact(parent, akey)
        # Another waiter may have published this parent-specific derivation.
        cached = self.store.get_derivation(dkey)
        if cached:
            try:
                return self.assets.resolve(cached)
            except Exception:
                self.store.delete_derivation(dkey)
        try:
            child = self._publish(parent, akey, manifest)
        except CollisionPostprocessError:
            raise
        except Exception as exc:
            raise CollisionPostprocessError(
                str(exc), code="postprocess_invalid_asset", retryable=False
            ) from exc
        self.store.put_derivation(dkey, parent.digest, akey, child.asset_ref)
        return child

    async def ensure_collision_ready(self, asset_ref: str) -> StoredAsset:
        """Compatibility alias for the engine-neutral readiness boundary."""
        return await self.ensure_simulation_ready(asset_ref)

    async def _ensure_artifact(
        self, parent: StoredAsset, key: str
    ) -> dict[str, Any]:
        owner = uuid.uuid4().hex
        deadline = time.monotonic() + self.config.timeout_s
        while True:
            record, claimed = self.store.acquire(
                key, owner, lease_seconds=self.config.timeout_s + 30
            )
            if claimed:
                try:
                    response = await self._call_worker(parent, key, deadline)
                    manifest = self._validate_staged(key, response)
                    self.store.complete(
                        key,
                        owner,
                        manifest,
                        float(response.get("processing_time_s", 0)),
                    )
                    return manifest
                except CollisionPostprocessError as exc:
                    self.store.fail(
                        key,
                        owner,
                        code=exc.code,
                        message=str(exc),
                        retryable=exc.retryable,
                    )
                    raise
                except Exception as exc:
                    error = CollisionPostprocessError(
                        str(exc), code="postprocess_invalid_asset", retryable=False
                    )
                    self.store.fail(
                        key,
                        owner,
                        code=error.code,
                        message=str(error),
                        retryable=False,
                    )
                    raise error from exc
            if record.status == "complete":
                try:
                    assert record.manifest is not None
                    return self._validate_staged(key, {"pieces": record.manifest})
                except Exception:
                    self.store.invalidate(key)
                    continue
            if record.status == "failed":
                raise CollisionPostprocessError(
                    record.error_message or "collision postprocess failed",
                    code=record.error_code or "postprocess_invalid_asset",
                    retryable=record.retryable,
                )
            if time.monotonic() >= deadline:
                raise CollisionPostprocessError(
                    "timed out waiting for collision postprocess",
                    code="postprocess_unavailable",
                    retryable=True,
                )
            await asyncio.sleep(0.05)

    async def _call_worker(
        self, parent: StoredAsset, key: str, deadline: float
    ) -> dict[str, Any]:
        delay = 0.1
        while True:
            try:
                return await self.client.decompose(
                    request_id=key,
                    asset_digest=parent.digest,
                    entrypoint=parent.manifest["visual"]["entrypoint"],
                    profile=self.config.profile,
                )
            except PostprocessClientError as exc:
                if not exc.retryable:
                    raise CollisionPostprocessError(
                        str(exc), code="postprocess_invalid_asset", retryable=False
                    ) from exc
                if time.monotonic() + delay >= deadline:
                    raise CollisionPostprocessError(
                        str(exc), code="postprocess_unavailable", retryable=True
                    ) from exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)

    def _validate_staged(
        self, key: str, response: dict[str, Any]
    ) -> dict[str, Any]:
        pieces = response.get("pieces")
        maximum = int(self.config.profile["max_convex_hulls"])
        if not isinstance(pieces, list) or not 1 <= len(pieces) <= maximum:
            raise ValueError("worker returned an invalid hull count")
        directory = (self.config.staging_root / key).resolve()
        root = self.config.staging_root.resolve()
        if root not in directory.parents:
            raise ValueError("unsafe staging directory")
        validated = []
        for item in pieces:
            if not isinstance(item, dict):
                raise ValueError("invalid hull manifest")
            relative = PurePosixPath(str(item.get("path", "")))
            if (
                relative.is_absolute()
                or len(relative.parts) != 1
                or relative.suffix.lower() != ".obj"
            ):
                raise ValueError("unsafe hull path")
            path = (directory / relative.name).resolve()
            if directory not in path.parents or not path.is_file():
                raise ValueError("staged hull is missing")
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if digest != item.get("sha256"):
                raise ValueError("staged hull digest mismatch")
            mesh = trimesh.load(path, force="mesh", process=False)
            vertices = np.asarray(mesh.vertices)
            faces = np.asarray(mesh.faces)
            if (
                len(vertices) < 4
                or len(faces) < 4
                or not np.isfinite(vertices).all()
                or faces.min(initial=0) < 0
                or faces.max(initial=0) >= len(vertices)
                or not mesh.is_convex
                or float(mesh.convex_hull.volume) <= 1e-12
            ):
                raise ValueError("worker returned a degenerate or non-convex hull")
            if int(item.get("vertices", -1)) != len(vertices) or int(
                item.get("faces", -1)
            ) != len(faces):
                raise ValueError("hull statistics mismatch")
            validated.append(
                {
                    "path": relative.name,
                    "sha256": digest,
                    "vertices": len(vertices),
                    "faces": len(faces),
                }
            )
        return validated

    def _publish(
        self, parent: StoredAsset, key: str, pieces: list[dict[str, Any]]
    ) -> StoredAsset:
        files = {
            record["path"]: self.assets.file_path(parent.root, record["path"]).read_bytes()
            for record in parent.manifest["files"]
        }
        transform = _visual_to_simulation(parent.manifest)
        simulation_to_asset = np.asarray(
            parent.manifest["simulation"]["transform_to_asset"], dtype=float
        )
        collision_records = []
        asset_vertices: list[np.ndarray] = []
        for index, item in enumerate(pieces):
            source = self.config.staging_root / key / item["path"]
            mesh = trimesh.load(source, force="mesh", process=False)
            mesh.apply_transform(transform)
            asset_mesh = mesh.copy()
            asset_mesh.apply_transform(simulation_to_asset)
            asset_vertices.append(np.asarray(asset_mesh.vertices))
            output = mesh.export(file_type="obj")
            if isinstance(output, str):
                output = output.encode()
            name = f"collision/hull_{index:03d}.obj"
            files[name] = output
            collision_records.append(
                {
                    "entrypoint": name,
                    "method": "coacd",
                    "profile": self.config.profile["name"],
                    "parameters_sha256": profile_sha256(self.config.profile),
                    "transform_to_asset": parent.manifest["simulation"][
                        "transform_to_asset"
                    ],
                }
            )
        _validate_hull_bounds(np.vstack(asset_vertices), parent.manifest["bounds"])
        simulation_name = parent.manifest["simulation"]["entrypoint"]
        files[simulation_name] = rewrite_sdf(
            files[simulation_name],
            simulation_name=simulation_name,
            visual_name=parent.manifest["visual"]["entrypoint"],
            hull_names=[item["entrypoint"] for item in collision_records],
            base_link=parent.manifest["simulation"]["base_link"],
        )
        source = dict(parent.manifest.get("source") or {})
        source_frame = source.pop("frame")
        return self.assets.ingest(
            files,
            visual=parent.manifest["visual"],
            simulation=parent.manifest["simulation"],
            collision=collision_records,
            bounds=parent.manifest.get("bounds"),
            joints=parent.manifest.get("joints"),
            support_surfaces=parent.manifest.get("support_surfaces"),
            metadata=parent.manifest.get("metadata"),
            source=source,
            source_frame=source_frame,
            license=parent.manifest.get("license"),
            tool_versions={
                **dict(parent.manifest.get("tool_versions") or {}),
                "collision_pipeline": COLLISION_PIPELINE_VERSION,
                "coacd": self.coacd_version,
                "sdf_generator": SDF_GENERATOR_VERSION,
            },
            parent={
                "asset_ref": parent.asset_ref,
                "operation": f"collision:{self.config.profile['name']}",
                "operation_version": "1",
            },
            preview=parent.manifest.get("preview"),
        )


def rewrite_sdf(
    content: bytes,
    *,
    simulation_name: str,
    visual_name: str,
    hull_names: list[str],
    base_link: str,
) -> bytes:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("simulation SDF cannot be parsed") from exc
    if root.tag != "sdf":
        raise ValueError("collision rewriting currently requires SDF")
    links = [item for item in root.iter("link") if item.get("name") == base_link]
    if len(links) != 1:
        raise ValueError("simulation base link is missing or ambiguous")
    link = links[0]
    simulation_dir = PurePosixPath(simulation_name).parent
    visual_resolved = posixpath.normpath(visual_name)
    for collision in list(link.findall("collision")):
        uri = collision.find("./geometry/mesh/uri")
        if uri is None or not uri.text:
            continue
        resolved = posixpath.normpath(
            posixpath.join(simulation_dir.as_posix(), uri.text.strip())
        )
        if resolved == visual_resolved or resolved == visual_name:
            link.remove(collision)
    for index, hull_name in enumerate(hull_names):
        collision = ET.SubElement(
            link, "collision", {"name": f"assetserver_collision_{index:03d}"}
        )
        geometry = ET.SubElement(collision, "geometry")
        mesh = ET.SubElement(geometry, "mesh")
        uri = ET.SubElement(mesh, "uri")
        uri.text = posixpath.relpath(hull_name, simulation_dir.as_posix())
        ET.SubElement(mesh, f"{{{DRAKE_NAMESPACE}}}declare_convex")
    rewritten = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    # Verify the generated document and all new references before publication.
    check = ET.fromstring(rewritten)
    generated = [
        item for item in check.iter("collision") if (item.get("name") or "").startswith("assetserver_collision_")
    ]
    if len(generated) != len(hull_names):
        raise ValueError("generated SDF collision verification failed")
    return rewritten + b"\n"


def _readiness(
    parent: StoredAsset, profile: dict[str, Any], coacd_version: str
) -> str:
    manifest = parent.manifest
    simulation = manifest.get("simulation")
    visual = manifest.get("visual")
    if not simulation or not simulation.get("base_link"):
        return "asset has no simulation/base link"
    if not visual:
        return "asset has no visual entrypoint"
    suffix = Path(simulation["entrypoint"]).suffix.lower()
    if suffix != ".sdf":
        return "collision rewriting requires an SDF simulation"
    try:
        root = ET.fromstring(
            ContentAddressedAssetStore.file_path(
                parent.root, simulation["entrypoint"]
            ).read_bytes()
        )
    except (OSError, ET.ParseError):
        return "simulation SDF cannot be parsed"
    if not _sdf_uris_valid(parent, root):
        return "simulation SDF contains an unresolved or unsafe mesh URI"
    if len(list(root.iter("model"))) != 1:
        return "simulation must contain exactly one model"
    links = list(root.iter("link"))
    articulated = bool(manifest.get("joints")) or bool(
        (manifest.get("metadata") or {}).get("articulated")
    )
    if articulated:
        if links and all(link.find("collision") is not None for link in links):
            return "ready"
        return "articulated asset has incomplete per-link collision"
    if len(links) != 1:
        return "multi-link asset is not declared articulated"
    geometries = list(links[0].findall("collision/geometry"))
    if geometries and all(geometry.find("mesh") is None for geometry in geometries):
        return "ready"
    collisions = manifest.get("collision") or []
    if (
        collisions
        and all(
            item.get("method") == "coacd"
            and item.get("profile") == profile["name"]
            and item.get("parameters_sha256") == profile_sha256(profile)
            and _valid_convex_entry(parent, item)
            for item in collisions
        )
        and manifest.get("tool_versions", {}).get("coacd") == coacd_version
        and _sdf_meshes_are_convex_declared(root)
    ):
        return "ready"
    return "rigid-triangle"


def _invalid(reason: str) -> CollisionPostprocessError:
    return CollisionPostprocessError(
        reason, code="postprocess_invalid_asset", retryable=False
    )


def _file_record(parent: StoredAsset, name: str) -> dict[str, Any]:
    for item in parent.manifest["files"]:
        if item["path"] == name:
            return item
    raise _invalid(f"asset file is missing: {name}")


def _valid_convex_entry(parent: StoredAsset, item: dict[str, Any]) -> bool:
    try:
        path = ContentAddressedAssetStore.file_path(parent.root, item["entrypoint"])
        mesh = trimesh.load(path, force="mesh", process=False)
        return bool(
            len(mesh.vertices) >= 4
            and len(mesh.faces) >= 4
            and np.isfinite(mesh.vertices).all()
            and mesh.is_convex
            and mesh.convex_hull.volume > 1e-12
        )
    except Exception:
        return False


def _sdf_meshes_are_convex_declared(root: ET.Element) -> bool:
    meshes = [
        mesh
        for collision in root.iter("collision")
        for mesh in collision.findall("./geometry/mesh")
        if mesh.find("uri") is not None
    ]
    return bool(meshes) and all(
        mesh.find(f"{{{DRAKE_NAMESPACE}}}declare_convex") is not None
        for mesh in meshes
    )


def _sdf_uris_valid(parent: StoredAsset, root: ET.Element) -> bool:
    simulation = parent.manifest["simulation"]["entrypoint"]
    base = PurePosixPath(simulation).parent.as_posix()
    files = {item["path"] for item in parent.manifest["files"]}
    for mesh in root.iter("mesh"):
        uri = mesh.find("uri")
        if uri is None or not uri.text:
            return False
        value = uri.text.strip()
        if "://" in value or "\\" in value or value.startswith("/"):
            return False
        resolved = posixpath.normpath(posixpath.join(base, value))
        if resolved.startswith("../") or resolved not in files:
            return False
    return True


def _visual_to_simulation(manifest: dict[str, Any]) -> np.ndarray:
    visual = np.asarray(manifest["visual"]["transform_to_asset"], dtype=float)
    simulation = np.asarray(
        manifest["simulation"]["transform_to_asset"], dtype=float
    )
    transform = np.linalg.inv(simulation) @ visual
    if not np.isfinite(transform).all():
        raise ValueError("invalid visual/simulation frame transform")
    return transform


def _validate_hull_bounds(vertices: np.ndarray, bounds: dict[str, Any]) -> None:
    expected_min = np.asarray(bounds["min"], dtype=float)
    expected_max = np.asarray(bounds["max"], dtype=float)
    actual_min = vertices.min(axis=0)
    actual_max = vertices.max(axis=0)
    expected_extent = expected_max - expected_min
    actual_extent = actual_max - actual_min
    active = expected_extent > 1e-8
    if active.any():
        ratios = actual_extent[active] / expected_extent[active]
        diagonal = max(float(np.linalg.norm(expected_extent)), 1e-8)
        center_error = np.linalg.norm(
            (actual_min + actual_max - expected_min - expected_max) / 2
        )
        if (ratios < 0.01).any() or (ratios > 100).any() or center_error > 10 * diagonal:
            raise ValueError("collision hull bounds are inconsistent with visual bounds")


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def publish_collision_asset(
    store: ContentAddressedAssetStore,
    asset_ref: str,
    collision_files: dict[str, bytes],
    *,
    method: str = "coacd",
    operation_version: str = "1",
) -> StoredAsset:
    """Compatibility helper for publishing an immutable collision child."""
    parent = store.resolve(asset_ref)
    files = {
        record["path"]: store.file_path(parent.root, record["path"]).read_bytes()
        for record in parent.manifest["files"]
    }
    normalized = {
        f"collision/{Path(name).name}": value
        for name, value in collision_files.items()
    }
    files.update(normalized)
    source = dict(parent.manifest["source"])
    frame = source.pop("frame")
    return store.ingest(
        files,
        visual=parent.manifest["visual"],
        simulation=parent.manifest.get("simulation"),
        collision=[{"entrypoint": name, "method": method} for name in sorted(normalized)],
        bounds=parent.manifest.get("bounds"),
        joints=parent.manifest.get("joints"),
        support_surfaces=parent.manifest.get("support_surfaces"),
        metadata=parent.manifest.get("metadata"),
        source=source,
        source_frame=frame,
        license=parent.manifest.get("license"),
        tool_versions={"collision": operation_version},
        preview=parent.manifest.get("preview"),
        parent={
            "asset_ref": asset_ref,
            "operation": method,
            "operation_version": operation_version,
        },
    )


def generate_collision_artifacts(
    mesh_path: str | Path,
    output_dir: str | Path | None = None,
    method: str | None = None,
    host: str | None = None,
    port: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Compatibility client for v1 producers during the one-release migration."""
    source = Path(mesh_path).resolve()
    target = (
        Path(output_dir).resolve()
        if output_dir is not None
        else source.parent / "collision" / source.stem
    )
    target.mkdir(parents=True, exist_ok=True)
    server_host = host or os.environ.get("ASSETSERVER_POSTPROCESS_HOST", "127.0.0.1")
    server_port = port or int(os.environ.get("ASSETSERVER_POSTPROCESS_PORT", "7100"))
    timeout = timeout_s or float(
        os.environ.get("ASSETSERVER_POSTPROCESS_TIMEOUT", "300")
    )
    try:
        response = httpx.post(
            f"http://{server_host}:{server_port}/generate_collision",
            json={
                "mesh_path": str(source),
                "method": method or os.environ.get("ASSETSERVER_COLLISION_METHOD", "coacd"),
            },
            timeout=timeout,
            trust_env=False,
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"legacy collision postprocess failed: {exc}") from exc
    pieces = data.get("collision_pieces") or []
    if not data.get("success") or not pieces:
        raise RuntimeError(data.get("error_message") or "postprocess returned no pieces")
    assets = []
    for index, piece in enumerate(pieces):
        path = target / f"{source.stem}_collision_{index}.obj"
        trimesh.Trimesh(vertices=piece["vertices"], faces=piece["faces"]).export(path)
        artifact = GLOBAL_ARTIFACTS.register(path)
        assets.append(
            {
                "index": index,
                "mesh_path": artifact["mesh_path"],
                "asset_id": artifact["asset_id"],
                "download_url": artifact["download_url"],
            }
        )
    return {
        "required": True,
        "status": "complete",
        "method": method or "coacd",
        "piece_count": len(assets),
        "processing_time_s": data.get("processing_time_s"),
        "assets": assets,
    }
