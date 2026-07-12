"""Container entrypoint for the standalone SAM3D service."""

from __future__ import annotations

import os

import uvicorn

from .app import create_app
from .runtime import SAM3DRuntime


def main() -> None:
    runtime = SAM3DRuntime(os.environ.get("SAM3D_MODEL_ROOT", "/models"))
    app = create_app(
        asset_root=os.environ.get("SAM3D_ASSET_ROOT", "/var/lib/sam3d/assets"),
        generator=runtime.generate,
        ready_check=runtime.readiness,
        backend_version=os.environ.get("SAM3D_IMAGE_VERSION", "dev"),
        model_bundle_version=runtime.bundle.bundle_version,
    )
    runtime.start_preload()
    uvicorn.run(
        app,
        host=os.environ.get("SAM3D_HOST", "0.0.0.0"),
        port=int(os.environ.get("SAM3D_PORT", "7000")),
    )


if __name__ == "__main__":
    main()
