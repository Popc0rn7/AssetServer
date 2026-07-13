"""Client-side helpers for mandatory collision asset generation."""

from __future__ import annotations

import os

from pathlib import Path
from typing import Any

import requests
import trimesh

from assetserver.artifacts import GLOBAL_ARTIFACTS
from assetserver.asset_store import ContentAddressedAssetStore, StoredAsset


def publish_collision_asset(
    store: ContentAddressedAssetStore,
    asset_ref: str,
    collision_files: dict[str, bytes],
    *,
    method: str = "coacd",
    operation_version: str = "1",
) -> StoredAsset:
    """Publish collision output as an immutable child without touching its parent."""
    parent = store.resolve(asset_ref)
    files = {
        record["path"]: store.file_path(parent.root, record["path"]).read_bytes()
        for record in parent.manifest["files"]
    }
    normalized_collision = {
        f"collision/{Path(name).name}": content
        for name, content in collision_files.items()
    }
    files.update(normalized_collision)
    return store.ingest(
        files,
        visual=parent.manifest["visual"],
        simulation=parent.manifest.get("simulation"),
        collision=[
            {"entrypoint": name, "method": method}
            for name in sorted(normalized_collision)
        ],
        bounds=parent.manifest.get("bounds"),
        joints=parent.manifest.get("joints"),
        support_surfaces=parent.manifest.get("support_surfaces"),
        metadata=parent.manifest.get("metadata"),
        source={
            "type": "derived",
            "name": "collision",
            "resource_id": parent.digest,
        },
        source_frame=parent.manifest["source"]["frame"],
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
    """Generate convex collision meshes and export them next to the visual asset.

    This is intentionally mandatory for callers: connection failures, server
    errors, or empty decomposition results raise an exception.
    """
    source_path = Path(mesh_path).resolve()
    target_dir = (
        Path(output_dir).resolve()
        if output_dir is not None
        else source_path.parent / "collision" / source_path.stem
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    collision_method = method or os.environ.get("ASSETSERVER_COLLISION_METHOD", "coacd")
    server_host = host or os.environ.get("ASSETSERVER_POSTPROCESS_HOST", "127.0.0.1")
    server_port = port or int(os.environ.get("ASSETSERVER_POSTPROCESS_PORT", "7100"))
    timeout = timeout_s or float(
        os.environ.get("ASSETSERVER_POSTPROCESS_TIMEOUT", "300")
    )

    response = requests.post(
        f"http://{server_host}:{server_port}/generate_collision",
        json={"mesh_path": str(source_path), "method": collision_method},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success", False):
        error = data.get("error_message", "unknown postprocess error")
        raise RuntimeError(f"Collision postprocess failed: {error}")

    pieces = data.get("collision_pieces", [])
    if not pieces:
        raise RuntimeError(
            f"Collision postprocess returned no pieces for {source_path}"
        )

    assets: list[dict[str, str | int]] = []
    for index, piece in enumerate(pieces):
        collision_path = target_dir / f"{source_path.stem}_collision_{index}.obj"
        mesh = trimesh.Trimesh(vertices=piece["vertices"], faces=piece["faces"])
        mesh.export(collision_path)
        artifact = GLOBAL_ARTIFACTS.register(collision_path)
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
        "method": collision_method,
        "piece_count": len(assets),
        "processing_time_s": data.get("processing_time_s"),
        "assets": assets,
    }
