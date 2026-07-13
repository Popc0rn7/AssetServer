"""Explicit, path-free HTTP API for the SAM3D backend."""

from __future__ import annotations

import os
import hashlib
import tempfile
import uuid

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from assetserver.asset_store import ContentAddressedAssetStore
from assetserver.asset_normalization import inspect_y_up_glb
from assetserver.staging import cleanup_staging

from .assets import Sam3DArtifactStore

Generator = Callable[..., None]
ReadyCheck = Callable[[], tuple[bool, str | None]]
ALLOWED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp"}


def create_app(
    *,
    asset_root: str | Path,
    generator: Generator,
    ready_check: ReadyCheck,
    backend_version: str = "dev",
    model_bundle_version: str = "unknown",
    max_upload_bytes: int = 25 * 1024 * 1024,
    shared_asset_root: str | Path | None = None,
) -> FastAPI:
    app = FastAPI(title="SAM3D Server", version="1")
    assets = Sam3DArtifactStore(asset_root)
    shared_assets = ContentAddressedAssetStore(
        shared_asset_root
        or os.environ.get("ASSETSERVER_ASSET_ROOT")
        or (Path(asset_root).parent / "assets-v2")
    )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live", "backend": "sam3d"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        healthy, error = ready_check()
        if not healthy:
            raise HTTPException(status_code=503, detail=error or "not ready")
        return {"status": "ready", "backend": "sam3d"}

    @app.post("/v1/sam3d/generations")
    async def generate(
        image: UploadFile = File(...),
        mode: str = Form("foreground"),
        prompt: str | None = Form(None),
        threshold: float = Form(0.5),
    ) -> dict:
        healthy, readiness_error = ready_check()
        if not healthy:
            raise HTTPException(
                status_code=503, detail=readiness_error or "backend not ready"
            )
        request_id = str(uuid.uuid4())
        if image.content_type not in ALLOWED_MEDIA_TYPES:
            raise HTTPException(status_code=415, detail="unsupported image type")
        if mode not in {"foreground", "object_description"}:
            raise HTTPException(status_code=422, detail="invalid mode")
        if mode == "object_description" and not prompt:
            raise HTTPException(status_code=422, detail="prompt is required")
        if not 0 <= threshold <= 1:
            raise HTTPException(status_code=422, detail="threshold must be in [0, 1]")

        with tempfile.TemporaryDirectory(prefix="sam3d-") as temporary:
            suffix = Path(image.filename or "image").suffix or ".img"
            input_path = Path(temporary) / f"input{suffix}"
            output_path = Path(temporary) / "model.glb"
            input_path.write_bytes(await image.read(max_upload_bytes + 1))
            if input_path.stat().st_size > max_upload_bytes:
                raise HTTPException(status_code=413, detail="image is too large")
            generator(
                image_path=input_path,
                output_path=output_path,
                mode=mode,
                object_description=prompt,
                threshold=threshold,
            )
            if not output_path.is_file():
                raise HTTPException(
                    status_code=500, detail="generator produced no asset"
                )
            asset = assets.create(
                output_path,
                {
                    "backend": "sam3d",
                    "backend_version": backend_version,
                    "model_bundle_version": model_bundle_version,
                    "request_id": request_id,
                },
            )

        return {
            "generation_id": request_id,
            "backend": "sam3d",
            "backend_version": backend_version,
            "model_bundle_version": model_bundle_version,
            "asset": {
                "asset_id": asset.asset_id,
                "media_type": "model/gltf-binary",
                "size_bytes": asset.size_bytes,
                "sha256": asset.sha256,
                "download_url": f"/v1/sam3d/assets/{asset.asset_id}",
            },
        }

    @app.get("/v1/sam3d/assets/{asset_id}")
    async def download(asset_id: str) -> Response:
        asset = assets.get(asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        return Response(
            content=asset.path.read_bytes(),
            media_type="model/gltf-binary",
            headers={
                "Content-Disposition": 'attachment; filename="model.glb"',
                "ETag": f'"{asset.sha256}"',
            },
        )

    @app.post("/v2/generations")
    async def generate_v2(
        image: UploadFile = File(...),
        mode: str = Form("foreground"),
        prompt: str | None = Form(None),
        threshold: float = Form(0.5),
    ) -> dict:
        healthy, readiness_error = ready_check()
        if not healthy:
            raise HTTPException(status_code=503, detail=readiness_error or "backend not ready")
        if image.content_type not in ALLOWED_MEDIA_TYPES:
            raise HTTPException(status_code=415, detail="unsupported image type")
        if mode not in {"foreground", "object_description"}:
            raise HTTPException(status_code=422, detail="invalid mode")
        if mode == "object_description" and not prompt:
            raise HTTPException(status_code=422, detail="prompt is required")
        if not 0 <= threshold <= 1:
            raise HTTPException(status_code=422, detail="threshold must be in [0, 1]")
        generation_id = str(uuid.uuid4())
        staging_root = Path(
            os.environ.get(
                "ASSETSERVER_STAGING_ROOT", str(Path(asset_root).parent / "jobs" / "staging")
            )
        )
        staging_root.mkdir(parents=True, exist_ok=True)
        cleanup_staging(staging_root)
        with tempfile.TemporaryDirectory(prefix=f"{generation_id}-", dir=staging_root) as temporary:
            suffix = Path(image.filename or "image").suffix or ".img"
            input_path = Path(temporary) / f"input{suffix}"
            output_path = Path(temporary) / "visual" / "model.glb"
            output_path.parent.mkdir()
            input_path.write_bytes(await image.read(max_upload_bytes + 1))
            if input_path.stat().st_size > max_upload_bytes:
                raise HTTPException(status_code=413, detail="image is too large")
            generator(
                image_path=input_path,
                output_path=output_path,
                mode=mode,
                object_description=prompt,
                threshold=threshold,
            )
            if not output_path.is_file():
                raise HTTPException(status_code=500, detail="generator produced no asset")
            materialization_key = hashlib.sha256(
                input_path.read_bytes()
                + f"\0{mode}\0{prompt or ''}\0{threshold}".encode()
                + backend_version.encode()
                + model_bundle_version.encode()
            ).hexdigest()
            bounds, source_frame = inspect_y_up_glb(output_path)
            sdf = b"""<sdf version='1.10'><model name='generated'><link name='base'><visual name='visual'><geometry><mesh><uri>../visual/model.glb</uri></mesh></geometry></visual><collision name='collision'><geometry><mesh><uri>../visual/model.glb</uri></mesh></geometry></collision></link></model></sdf>\n"""
            stored = shared_assets.ingest(
                {
                    "visual/model.glb": output_path.read_bytes(),
                    "simulation/model.sdf": sdf,
                },
                visual="visual/model.glb",
                simulation={"entrypoint": "simulation/model.sdf", "base_link": "base"},
                collision={"entrypoint": "visual/model.glb", "method": "triangle-mesh"},
                bounds=bounds,
                metadata={"category": "generated", "description": prompt or ""},
                source={
                    "type": "generated",
                    "name": "sam3d",
                    "resource_id": materialization_key,
                    "provenance": {"conditioning": "image"},
                },
                source_frame=source_frame,
                tool_versions={
                    "backend": backend_version,
                    "model_bundle": model_bundle_version,
                },
            )
        return {
            "generation_id": generation_id,
            "asset_ref": stored.asset_ref,
            "backend": "sam3d",
        }

    return app
