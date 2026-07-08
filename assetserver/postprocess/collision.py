"""Client-side helpers for mandatory collision asset generation."""

from __future__ import annotations

import os

from pathlib import Path
from typing import Any

import requests
import trimesh

from assetserver.artifacts import GLOBAL_ARTIFACTS


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
