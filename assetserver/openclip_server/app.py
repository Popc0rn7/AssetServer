"""HTTP API for OpenCLIP embeddings."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel


class TextEmbeddingRequest(BaseModel):
    inputs: list[str]
    normalize: bool = True


def create_app(
    *,
    ready_check: Callable[[], tuple[bool, str | None]],
    text_embed: Callable[[list[str], bool], list[list[float]]],
    image_embed: Callable[[list[bytes], bool], list[list[float]]],
    model_info: dict,
    max_batch_size: int = 32,
    max_image_bytes: int = 25 * 1024 * 1024,
) -> FastAPI:
    app = FastAPI(title="AssetServer OpenCLIP", version="1")

    def ensure_ready() -> None:
        ready, error = ready_check()
        if not ready:
            raise HTTPException(status_code=503, detail=error or "model is loading")

    def response(embeddings: list[list[float]]) -> dict:
        return {**model_info, "embeddings": embeddings}

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live", "backend": "openclip"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        ensure_ready()
        return {"status": "ready", "backend": "openclip"}

    @app.post("/v1/embeddings/text")
    async def embed_text(request: TextEmbeddingRequest) -> dict:
        ensure_ready()
        if not request.inputs or len(request.inputs) > max_batch_size:
            raise HTTPException(status_code=422, detail="invalid text batch size")
        if any(not item.strip() for item in request.inputs):
            raise HTTPException(status_code=422, detail="text inputs must be non-empty")
        return response(text_embed(request.inputs, request.normalize))

    @app.post("/v1/embeddings/images")
    async def embed_images(
        images: list[UploadFile] = File(...), normalize: bool = True
    ) -> dict:
        ensure_ready()
        if not images or len(images) > max_batch_size:
            raise HTTPException(status_code=422, detail="invalid image batch size")
        payloads = []
        for image in images:
            if image.content_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise HTTPException(status_code=415, detail="unsupported image type")
            payload = await image.read(max_image_bytes + 1)
            if len(payload) > max_image_bytes:
                raise HTTPException(status_code=413, detail="image is too large")
            payloads.append(payload)
        return response(image_embed(payloads, normalize))

    return app
