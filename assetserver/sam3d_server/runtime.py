"""SAM3D pipeline lifecycle with local-only model loading."""

from __future__ import annotations

import gc
import logging
import os
import shutil
import sys
import threading
import types

from pathlib import Path

from .model_bundle import ModelBundle, validate_bundle

logger = logging.getLogger(__name__)


def install_kaolin_testing_shim() -> None:
    """Provide the only Kaolin helper used by SAM3D inference.

    Upstream SAM3D imports ``kaolin.utils.testing.check_tensor`` from its
    FlexiCubes implementation. Building the complete Kaolin 0.17 CUDA package
    is unnecessary for this shape check and is not Python 3.11 compatible.
    """
    try:
        from kaolin.utils.testing import check_tensor as _check_tensor  # noqa: F401

        return
    except ImportError:
        pass

    def check_tensor(tensor, shape=None, dtype=None, device=None, throw=True):
        error = None
        if shape is not None and len(shape) != tensor.ndim:
            error = f"tensor has {tensor.ndim} dimensions, expected {len(shape)}"
        elif shape is not None:
            for index, dimension in enumerate(shape):
                if dimension is not None and tensor.shape[index] != dimension:
                    error = f"tensor shape is {tensor.shape}, expected {shape}"
                    break
        if error is None and dtype is not None and dtype != tensor.dtype:
            error = f"tensor dtype is {tensor.dtype}, expected {dtype}"
        if error is None and device is not None and device != tensor.device.type:
            error = f"tensor device is {tensor.device.type}, expected {device}"
        if error is not None and throw:
            raise ValueError(error)
        return error is None

    kaolin = types.ModuleType("kaolin")
    utils = types.ModuleType("kaolin.utils")
    testing = types.ModuleType("kaolin.utils.testing")
    testing.check_tensor = check_tensor
    utils.testing = testing
    kaolin.utils = utils
    sys.modules.setdefault("kaolin", kaolin)
    sys.modules.setdefault("kaolin.utils", utils)
    sys.modules.setdefault("kaolin.utils.testing", testing)


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
        self._generation_lock = threading.Lock()
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
            install_kaolin_testing_shim()
            force_local_dinov2_hub(os.environ.get("SAM3D_DINOV2_SOURCE", "/opt/dinov2"))
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

        import torch

        from assetserver.geometry_generation_server.sam3d_pipeline_manager import (
            generate_with_sam3d,
        )

        # Keep the model resident, but isolate each request's temporary CUDA state.
        # Upstream texture baking explicitly re-enables gradients where it needs them.
        with self._generation_lock:
            try:
                with torch.inference_mode():
                    generate_with_sam3d(
                        image_path=image_path,
                        output_path=output_path,
                        sam3_checkpoint=self.bundle.sam3_checkpoint,
                        sam3d_checkpoint=self.bundle.pipeline_config,
                        debug_folder=None,
                        use_pipeline_caching=True,
                        **options,
                    )
            finally:
                self._release_request_memory(torch)

    @staticmethod
    def _release_request_memory(torch) -> None:
        """Release request-scoped objects and unused PyTorch CUDA cache."""
        gc.collect()
        if not torch.cuda.is_available():
            return

        try:
            # Native CUDA extensions launch asynchronous work. Synchronize before
            # measuring and returning their now-unused PyTorch allocations.
            torch.cuda.synchronize()
            allocated = torch.cuda.memory_allocated()
            reserved_before = torch.cuda.memory_reserved()
            torch.cuda.empty_cache()
            reserved_after = torch.cuda.memory_reserved()
        except Exception:
            # Cleanup must never hide the original generation error.
            logger.exception("SAM3D CUDA cleanup failed")
            return

        logger.info(
            "SAM3D CUDA cleanup: allocated=%.1f MiB, "
            "reserved_before=%.1f MiB, reserved_after=%.1f MiB",
            allocated / 1024**2,
            reserved_before / 1024**2,
            reserved_after / 1024**2,
        )
