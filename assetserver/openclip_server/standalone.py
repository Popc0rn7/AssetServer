"""Container entry point for the OpenCLIP service."""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import create_app
from .runtime import OpenCLIPRuntime


def main() -> None:
    logging.basicConfig(level=os.environ.get("OPENCLIP_LOG_LEVEL", "INFO"))
    runtime = OpenCLIPRuntime(os.environ.get("OPENCLIP_MODEL_ROOT", "/models"))
    app = create_app(
        ready_check=runtime.readiness,
        text_embed=runtime.embed_text,
        image_embed=runtime.embed_images,
        model_info=runtime.model_info,
    )
    if os.environ.get("OPENCLIP_PRELOAD_SYNC", "0") == "1":
        runtime.preload()
    else:
        runtime.start_preload()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("OPENCLIP_PORT", "7006")))


if __name__ == "__main__":
    main()
