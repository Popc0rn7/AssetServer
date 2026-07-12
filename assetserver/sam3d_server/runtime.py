"""SAM3D pipeline lifecycle with local-only model loading."""

from __future__ import annotations

import os
import shutil
import threading

from pathlib import Path

from .model_bundle import ModelBundle, validate_bundle


def ensure_runtime_cache_dirs(cache_root: str | Path) -> None:
    root = Path(cache_root)
    for name in (
        "xdg",
        "config",
        "matplotlib",
        "hf",
        "torch",
        "torch-extensions",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)


def seed_dinov2_cache(
    bundle: ModelBundle, *, source_root: str | Path, torch_home: str | Path
) -> None:
    source = Path(source_root)
    cache = Path(torch_home)
    repo = cache / "hub" / "facebookresearch_dinov2_main"
    checkpoints = cache / "hub" / "checkpoints"
    if not source.is_dir():
        raise RuntimeError(f"DINOv2 source missing from image: {source}")
    shutil.copytree(source, repo, dirs_exist_ok=True)
    checkpoints.mkdir(parents=True, exist_ok=True)
    cached_weight = checkpoints / "dinov2_vitl14_reg4_pretrain.pth"
    if cached_weight.exists() or cached_weight.is_symlink():
        cached_weight.unlink()
    cached_weight.symlink_to(bundle.dino_weights)


class SAM3DRuntime:
    def __init__(self, model_root: str | Path) -> None:
        self.bundle: ModelBundle = validate_bundle(model_root)
        self._ready = False
        self._error: str | None = "pipeline is loading"
        self._lock = threading.Lock()
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("XDG_CACHE_HOME", "/var/cache/sam3d/xdg")
        os.environ.setdefault("XDG_CONFIG_HOME", "/var/cache/sam3d/config")
        os.environ.setdefault("MPLCONFIGDIR", "/var/cache/sam3d/matplotlib")
        os.environ.setdefault("HF_HOME", "/var/cache/sam3d/hf")
        os.environ.setdefault("TORCH_HOME", "/var/cache/sam3d/torch")
        os.environ.setdefault(
            "TORCH_EXTENSIONS_DIR", "/var/cache/sam3d/torch-extensions"
        )
        ensure_runtime_cache_dirs(Path(os.environ["XDG_CACHE_HOME"]).parent)
        os.environ["SAM3D_MOGE_MODEL_PATH"] = str(self.bundle.moge_model)
        seed_dinov2_cache(
            self.bundle,
            source_root=os.environ.get("SAM3D_DINOV2_SOURCE", "/opt/dinov2"),
            torch_home=os.environ["TORCH_HOME"],
        )

    def start_preload(self) -> None:
        threading.Thread(target=self._preload, daemon=True).start()

    def _preload(self) -> None:
        try:
            from assetserver.geometry_generation_server.sam3d_pipeline_manager import (
                SAM3DPipelineManager,
            )

            SAM3DPipelineManager.get_pipelines(
                sam3_checkpoint=self.bundle.sam3_checkpoint,
                sam3d_checkpoint=self.bundle.pipeline_config,
            )
            with self._lock:
                self._ready = True
                self._error = None
        except Exception as exc:
            with self._lock:
                self._ready = False
                self._error = str(exc)

    def readiness(self) -> tuple[bool, str | None]:
        with self._lock:
            return self._ready, self._error

    def generate(self, image_path: Path, output_path: Path, **options) -> None:
        healthy, error = self.readiness()
        if not healthy:
            raise RuntimeError(error or "pipeline not ready")
        from assetserver.geometry_generation_server.sam3d_pipeline_manager import (
            generate_with_sam3d,
        )

        generate_with_sam3d(
            image_path=image_path,
            output_path=output_path,
            sam3_checkpoint=self.bundle.sam3_checkpoint,
            sam3d_checkpoint=self.bundle.pipeline_config,
            debug_folder=None,
            use_pipeline_caching=True,
            **options,
        )
