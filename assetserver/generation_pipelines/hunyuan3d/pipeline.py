"""Hunyuan3D image-to-GLB pipeline."""

from __future__ import annotations

import gc
import logging
import os

from pathlib import Path
from typing import Any

from PIL import Image

from assetserver.generation_server.protocol import (
    GenerationRequest,
    GenerationValidationError,
)

logger = logging.getLogger(__name__)


class Hunyuan3DPipeline:
    name = "hunyuan3d"
    _generation_defaults = {
        "num_inference_steps": 5,
        "guidance_scale": 5.0,
        "octree_resolution": 256,
        "num_chunks": 8000,
    }

    def __init__(self, config: dict[str, Any]) -> None:
        generation = config["generation"]
        model = generation["model"]
        self.full_root = Path(model["full_root"])
        self.mini_root = Path(model["mini_root"])
        defaults = dict(generation.get("defaults") or {})
        self.variant = defaults.pop("variant", "full")
        if self.variant not in {"full", "mini"}:
            raise ValueError("Hunyuan3D variant must be full or mini")
        self.defaults = {**self._generation_defaults, **defaults}
        self._components: tuple[Any, Any, Any, Any] | None = None

    def load(self) -> None:
        # The upstream loader reads these variables. The variant is immutable for
        # the lifetime of this pipeline instance, so no request can trigger reload.
        os.environ["HUNYUAN3D_MODEL_DIR"] = str(self.full_root)
        os.environ["HUNYUAN3D_MINI_MODEL_DIR"] = str(self.mini_root)
        from .pipeline_manager import Hunyuan3DPipelineManager

        self._components = Hunyuan3DPipelineManager.get_pipelines(
            use_mini=self.variant == "mini"
        )

    def generate(self, request: GenerationRequest, output_path: Path) -> None:
        if self._components is None:
            raise RuntimeError("Hunyuan3D pipeline is not loaded")
        forbidden = {"variant", "use_mini"} & set(request.options)
        if forbidden:
            raise GenerationValidationError(
                "Hunyuan3D model variant is fixed by server configuration"
            )
        allowed = set(self._generation_defaults)
        unknown = set(request.options) - allowed
        if unknown:
            raise GenerationValidationError(
                f"unsupported Hunyuan3D options: {', '.join(sorted(unknown))}"
            )
        parameters = {**self.defaults, **request.options}
        shape, texture, face_reducer, background_remover = self._components
        try:
            from hy3dgen.shapegen.pipelines import export_to_trimesh
        except ImportError as exc:
            raise ImportError(
                "Hunyuan3D-2 is not installed; run scripts/install_hunyuan3d.sh"
            ) from exc

        image = background_remover(Image.open(request.image_path).convert("RGBA"))
        outputs = shape(image=image, output_type="mesh", **parameters)
        mesh = face_reducer(export_to_trimesh(outputs)[0])
        textured = texture(mesh, image=image)
        textured.export(output_path)

    def cleanup_request(self) -> None:
        gc.collect()
        try:
            import torch

            if not torch.cuda.is_available():
                return
            torch.cuda.synchronize()
            allocated = torch.cuda.memory_allocated()
            reserved_before = torch.cuda.memory_reserved()
            torch.cuda.empty_cache()
            logger.info(
                "Hunyuan3D CUDA cleanup: allocated=%.1f MiB, "
                "reserved_before=%.1f MiB, reserved_after=%.1f MiB",
                allocated / 1024**2,
                reserved_before / 1024**2,
                torch.cuda.memory_reserved() / 1024**2,
            )
        except Exception:
            logger.exception("Hunyuan3D CUDA cleanup failed")

    def tool_versions(self) -> dict[str, str]:
        return {
            "backend": "hunyuan3d",
            "implementation": os.environ.get("ASSETSERVER_BUILD_VERSION", "dev"),
            "model_variant": self.variant,
        }
