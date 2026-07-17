"""Stateless canonical review renders for immutable assets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any

from PIL import Image

from assetserver.artifact_store import ArtifactCatalog
from assetserver.asset_store import ASSET_REF_PREFIX, AssetStoreError, ContentAddressedAssetStore
from assetserver.blender_scene_worker import BlenderRecipeError, render_recipe
from assetserver.jobs import Job, JobExecutionError

PRODUCER_VERSION = "asset-observe-worker/1"
CANONICAL_RESOURCE = "canonical_asset_scene.v1.json"


def canonical_scene() -> dict[str, Any]:
    value = json.loads(files("assetserver").joinpath(CANONICAL_RESOURCE).read_text())
    if value.get("schema_version") != "asset-canonical-scene/v1" or value.get("views") != [
        "perspective", "front", "side", "top"
    ]:
        raise RuntimeError("invalid canonical asset scene")
    return value


def asset_observe(job: Job) -> dict[str, Any]:
    if job.subject_type != "asset":
        raise JobExecutionError("asset_observe requires an asset subject", code="asset_load_failed")
    root = Path(os.environ.get("ASSETSERVER_DATA_ROOT", "data"))
    ref = f"{ASSET_REF_PREFIX}{job.subject_id}"
    try:
        asset = ContentAddressedAssetStore(root / "assets").resolve(ref)
        visual = asset.manifest.get("visual")
        if not isinstance(visual, dict):
            raise AssetStoreError("asset has no visual entrypoint")
        entrypoint = ContentAddressedAssetStore.file_path(asset.root, visual["entrypoint"])
        if entrypoint.suffix.lower() not in {".glb", ".gltf", ".obj"}:
            raise AssetStoreError("unsupported visual format")
    except (AssetStoreError, KeyError, OSError) as exc:
        raise JobExecutionError(str(exc), code="asset_load_failed", retryable=False) from exc

    canonical = canonical_scene()
    destination = root / "asset-observations" / job.subject_id / job.job_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{job.job_id}-", dir=destination.parent))
    try:
        parts = []
        for part in visual.get("parts") or []:
            parts.append({**part, "visual": str(ContentAddressedAssetStore.file_path(asset.root, part["entrypoint"]))})
        instance = {
            "name": "asset", "translation": [0, 0, 0], "rotation_radians": [0, 0, 0],
            "scale": 1.0, "visual": str(entrypoint),
            "asset_transform": visual.get("transform_to_asset"), "visual_parts": parts,
            "initial_joints": {item["name"]: item.get("default", 0.0) for item in asset.manifest.get("joints") or []},
        }
        simulation = asset.manifest.get("simulation")
        if parts and isinstance(simulation, dict):
            instance["simulation"] = str(ContentAddressedAssetStore.file_path(asset.root, simulation["entrypoint"]))
        recipe = temporary / "recipe.json"
        recipe.write_text(json.dumps({"schema_version": "blender-recipe/v1", "instances": [instance],
                                      "normalize_asset_ground_center": True,
                                      "canonical_asset_review": canonical}))
        try:
            rendered = render_recipe(recipe, temporary, views=canonical["views"], width=512, height=512, image_format="webp")
        except BlenderRecipeError as exc:
            raise JobExecutionError(str(exc), code="asset_render_failed", retryable=False) from exc
        recipe.unlink(missing_ok=True)
        digests = []
        records = []
        for item in rendered:
            path = Path(item["path"])
            try:
                with Image.open(path) as image:
                    image.load()
                    extrema = image.convert("RGB").getextrema()
                    if image.size != (512, 512) or all(low == high for low, high in extrema):
                        raise ValueError("blank or incorrectly sized render")
            except Exception as exc:
                raise JobExecutionError(str(exc), code="asset_render_invalid", retryable=True) from exc
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            digests.append(digest)
            records.append({**item, "path": path.name, "sha256": digest, "size_bytes": path.stat().st_size})
        if len(records) != 4 or len(set(digests)) == 1:
            raise JobExecutionError("canonical views are missing or all identical", code="asset_render_invalid", retryable=True)
        os.replace(temporary, destination)
        bounds = next((item.get("world_bounds") for item in records if item.get("world_bounds")), None) or asset.manifest.get("bounds") or {}
        provenance = {"job_id": job.job_id, "asset_ref": ref, "producer_version": PRODUCER_VERSION,
                      "canonical_scene_version": canonical["schema_version"]}
        catalog = ArtifactCatalog(root / "artifacts" / "artifacts.sqlite3")
        artifacts = catalog.publish_many([{
            "logical_key": f"asset-observation:{job.job_id}:view:{item['view']}",
            "path": destination / item["path"], "kind": "asset_observation_view", "media_type": "image/webp",
            "provenance": provenance,
            "metadata": {"view": item["view"], "width": 512, "height": 512, "bounds": bounds,
                         "camera": {k: item[k] for k in ("camera_location", "target", "extrinsics", "intrinsics") if k in item},
                         "instance_scale": 1.0, "framing_algorithm_version": canonical["framing_algorithm_version"]},
        } for item in records])
        return {"schema_version": "asset-observation/v1", "asset_ref": ref, "views": [
            {"view": item["view"], "artifact_id": artifact.artifact_id,
             "content_url": f"/v2/artifacts/{artifact.artifact_id}/content", "media_type": "image/webp",
             "sha256": artifact.sha256, "size_bytes": artifact.size_bytes, "width": 512, "height": 512}
            for item, artifact in zip(records, artifacts)
        ]}
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
