"""SAM3D pipeline lifecycle with local-only model loading."""

from __future__ import annotations

import gc
import logging
import os
import shutil
import sys
import types

from pathlib import Path

from assetserver.generation_server.protocol import (
    GenerationRequest,
    GenerationValidationError,
)

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


class SAM3DPipeline:
    name = "sam3d"

    def __init__(self, config: dict) -> None:
        generation = config["generation"]
        self.defaults = dict(generation.get("defaults") or {})
        self.model_root = Path(generation["model"]["root"])
        self.sources = {
            name: Path(path)
            for name, path in dict(generation.get("sources") or {}).items()
        }
        self.cache_root = Path(generation["cache"]["root"])
        self.offline = bool(generation.get("offline", True))
        self.bundle: ModelBundle | None = None

    def _prepare_sources(self) -> None:
        required = {"sam3", "sam3d_objects", "dinov2"}
        missing = required - set(self.sources)
        if missing:
            raise RuntimeError(
                f"SAM3D source paths are missing from config: {', '.join(sorted(missing))}"
            )
        for name in sorted(required):
            source = self.sources[name].resolve()
            if not source.is_dir():
                raise RuntimeError(f"SAM3D source directory is missing: {source}")
            self.sources[name] = source
        for name in ("sam3d_objects", "sam3"):
            source = str(self.sources[name])
            if source not in sys.path:
                sys.path.insert(0, source)

    def _prepare_bundle(self) -> ModelBundle:
        if self.bundle is not None:
            return self.bundle
        self.bundle = validate_bundle(self.model_root)
        offline = "1" if self.offline else "0"
        os.environ["HF_HUB_OFFLINE"] = offline
        os.environ["TRANSFORMERS_OFFLINE"] = offline
        os.environ["XDG_CACHE_HOME"] = str(self.cache_root / "xdg")
        os.environ["XDG_CONFIG_HOME"] = str(self.cache_root / "config")
        os.environ["MPLCONFIGDIR"] = str(self.cache_root / "matplotlib")
        os.environ["HF_HOME"] = str(self.cache_root / "hf")
        os.environ["TORCH_HOME"] = str(self.cache_root / "torch")
        os.environ["TORCH_EXTENSIONS_DIR"] = str(
            self.cache_root / "torch-extensions"
        )
        ensure_runtime_cache_dirs(self.cache_root)
        os.environ["SAM3D_MOGE_MODEL_PATH"] = str(self.bundle.moge_model)
        seed_dinov2_cache(
            self.bundle,
            source_root=self.sources["dinov2"],
            torch_home=os.environ["TORCH_HOME"],
        )
        return self.bundle

    def load(self) -> None:
        self._prepare_sources()
        bundle = self._prepare_bundle()
        install_kaolin_testing_shim()
        force_local_dinov2_hub(self.sources["dinov2"])
        from .pipeline_manager import SAM3DPipelineManager

        SAM3DPipelineManager.get_pipelines(
            sam3_checkpoint=bundle.sam3_checkpoint,
            sam3d_checkpoint=bundle.pipeline_config,
        )

    def generate(self, request: GenerationRequest, output_path: Path) -> None:
        import torch

        from .pipeline_manager import generate_with_sam3d

        if self.bundle is None:
            raise RuntimeError("SAM3D pipeline is not loaded")

        unknown = set(request.options) - {"mode", "threshold"}
        if unknown:
            raise GenerationValidationError(
                f"unsupported SAM3D options: {', '.join(sorted(unknown))}"
            )
        mode = request.options.get("mode", self.defaults.get("mode", "foreground"))
        threshold = request.options.get(
            "threshold", self.defaults.get("threshold", 0.5)
        )
        if mode not in {"foreground", "object_description"}:
            raise GenerationValidationError(
                "mode must be foreground or object_description"
            )
        if mode == "object_description" and not request.prompt:
            raise GenerationValidationError(
                "prompt is required for object_description mode"
            )
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise GenerationValidationError("threshold must be a number in [0, 1]")
        if not 0 <= float(threshold) <= 1:
            raise GenerationValidationError("threshold must be in [0, 1]")

        # Upstream texture baking explicitly re-enables gradients where needed.
        with torch.inference_mode():
            generate_with_sam3d(
                image_path=request.image_path,
                output_path=output_path,
                sam3_checkpoint=self.bundle.sam3_checkpoint,
                sam3d_checkpoint=self.bundle.pipeline_config,
                mode=mode,
                object_description=request.prompt,
                threshold=float(threshold),
                debug_folder=None,
                use_pipeline_caching=True,
            )

    def cleanup_request(self) -> None:
        import torch

        self._release_request_memory(torch)

    def tool_versions(self) -> dict[str, str]:
        if self.bundle is None:
            raise RuntimeError("SAM3D pipeline is not loaded")
        return {
            "backend": "sam3d",
            "implementation": os.environ.get("SAM3D_IMAGE_VERSION", "dev"),
            "model_bundle": self.bundle.bundle_version,
        }

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
