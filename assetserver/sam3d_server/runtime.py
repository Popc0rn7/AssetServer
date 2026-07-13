"""SAM3D pipeline lifecycle with local-only model loading."""

from __future__ import annotations

import os
import shutil
import threading
import logging

from pathlib import Path

from .model_bundle import ModelBundle, validate_bundle

logger = logging.getLogger(__name__)


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
    if not (source / "hubconf.py").is_file():
        raise RuntimeError(f"DINOv2 hubconf.py missing from image: {source}")
    shutil.copytree(source, repo, dirs_exist_ok=True)
    checkpoints.mkdir(parents=True, exist_ok=True)
    cached_weight = checkpoints / "dinov2_vitl14_reg4_pretrain.pth"
    if cached_weight.exists() or cached_weight.is_symlink():
        cached_weight.unlink()
    cached_weight.symlink_to(bundle.dino_weights)


def force_local_dinov2_hub(source_root: str | Path) -> None:
    """Redirect the upstream SAM3D DINO hub call to image-local source code."""
    source = Path(source_root).resolve()
    if not (source / "hubconf.py").is_file():
        raise RuntimeError(f"DINOv2 local source is incomplete: {source}")

    import torch

    current = torch.hub.load
    if getattr(current, "_assetserver_dinov2_source", None) == str(source):
        return

    def local_load(repo_or_dir, model, *args, **kwargs):
        repository = str(repo_or_dir).split(":", 1)[0].rstrip("/")
        requested_source = str(kwargs.get("source", "github"))
        if repository == "facebookresearch/dinov2" and requested_source == "github":
            logger.info(
                "Redirecting DINOv2 torch.hub load to offline source %s", source
            )
            repo_or_dir = str(source)
            kwargs["source"] = "local"
        return current(repo_or_dir, model, *args, **kwargs)

    local_load._assetserver_dinov2_source = str(source)  # type: ignore[attr-defined]
    torch.hub.load = local_load


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
            force_local_dinov2_hub(
                os.environ.get("SAM3D_DINOV2_SOURCE", "/opt/dinov2")
            )
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
