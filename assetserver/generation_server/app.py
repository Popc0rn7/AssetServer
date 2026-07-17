"""Path-free multipart HTTP API shared by all generation pipelines."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid

from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from assetserver.asset_normalization import inspect_y_up_glb
from assetserver.asset_store import ContentAddressedAssetStore
from assetserver.staging import cleanup_staging

from .protocol import GenerationRequest, GenerationResult, GenerationValidationError
from .runtime import GenerationRuntime

ALLOWED_MEDIA_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
SDF = b"""<sdf version='1.10'><model name='generated'><link name='base'><visual name='visual'><geometry><mesh><uri>../visual/model.glb</uri></mesh></geometry></visual><collision name='collision'><geometry><mesh><uri>../visual/model.glb</uri></mesh></geometry></collision></link></model></sdf>\n"""


def _parse_options(value: str | None) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="options must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="options must be a JSON object")
    return parsed


def create_app(
    *,
    runtime: GenerationRuntime,
    asset_root: str | Path | None = None,
    staging_root: str | Path | None = None,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> FastAPI:
    backend = runtime.pipeline.name
    app = FastAPI(title=f"{backend} Generation Server", version="2")
    shared_assets = ContentAddressedAssetStore(
        asset_root or os.environ.get("ASSETSERVER_ASSET_ROOT", "data/assets")
    )
    staging = Path(
        staging_root
        or os.environ.get("ASSETSERVER_STAGING_ROOT", "data/jobs/staging")
    )
    runtime.start()

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live", "backend": backend}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        healthy, error = runtime.readiness()
        if not healthy:
            raise HTTPException(status_code=503, detail=error or "pipeline not ready")
        return {"status": "ready", "backend": backend}

    @app.post("/v2/generations")
    async def generate(
        image: UploadFile = File(...),
        prompt: str | None = Form(None),
        options: str | None = Form(None),
    ) -> dict[str, str]:
        healthy, error = runtime.readiness()
        if not healthy:
            raise HTTPException(status_code=503, detail=error or "pipeline not ready")
        suffix = ALLOWED_MEDIA_TYPES.get(image.content_type or "")
        if suffix is None:
            raise HTTPException(status_code=415, detail="unsupported image type")
        parsed_options = _parse_options(options)
        payload = await image.read(max_upload_bytes + 1)
        if len(payload) > max_upload_bytes:
            raise HTTPException(status_code=413, detail="image is too large")

        generation_id = str(uuid.uuid4())
        staging.mkdir(parents=True, exist_ok=True)
        cleanup_staging(staging)
        with tempfile.TemporaryDirectory(prefix=f"{generation_id}-", dir=staging) as temporary:
            root = Path(temporary)
            input_path = root / f"input{suffix}"
            output_path = root / "visual" / "model.glb"
            output_path.parent.mkdir()
            input_path.write_bytes(payload)
            request = GenerationRequest(
                generation_id=generation_id,
                image_path=input_path,
                prompt=prompt,
                options=parsed_options,
            )
            try:
                await runtime.generate(request, output_path)
            except GenerationValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise HTTPException(status_code=500, detail="pipeline produced no asset")

            versions = dict(runtime.pipeline.tool_versions())
            materialization_key = hashlib.sha256(
                payload
                + b"\0"
                + (prompt or "").encode()
                + b"\0"
                + json.dumps(parsed_options, sort_keys=True, separators=(",", ":")).encode()
                + b"\0"
                + json.dumps(versions, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            bounds, source_frame = inspect_y_up_glb(output_path)
            stored = shared_assets.ingest(
                {
                    "visual/model.glb": output_path.read_bytes(),
                    "simulation/model.sdf": SDF,
                },
                visual="visual/model.glb",
                simulation={"entrypoint": "simulation/model.sdf", "base_link": "base"},
                collision={"entrypoint": "visual/model.glb", "method": "triangle-mesh"},
                bounds=bounds,
                metadata={"category": "generated", "description": prompt or ""},
                source={
                    "type": "generated",
                    "name": backend,
                    "resource_id": materialization_key,
                    "provenance": {"conditioning": "image"},
                },
                source_frame=source_frame,
                tool_versions=versions,
            )
        return GenerationResult(
            generation_id=generation_id,
            asset_ref=stored.asset_ref,
            backend=backend,
        ).to_dict()

    return app
