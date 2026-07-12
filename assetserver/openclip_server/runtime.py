"""OpenCLIP model lifecycle, loaded lazily without importing Torch in Gateway."""

from __future__ import annotations

import io
import logging
import os
import threading

from pathlib import Path

from .model_bundle import OpenCLIPBundle, validate_bundle

logger = logging.getLogger(__name__)


class OpenCLIPRuntime:
    def __init__(self, model_root: str | Path) -> None:
        self.bundle: OpenCLIPBundle = validate_bundle(model_root)
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._device = None
        self._ready = False
        self._error: str | None = "model is loading"
        self._state_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    @property
    def model_info(self) -> dict:
        return {
            "model": self.bundle.model,
            "revision": self.bundle.revision,
            "dimension": self.bundle.dimension,
        }

    def start_preload(self) -> None:
        threading.Thread(target=self._preload, daemon=True).start()

    def _preload(self) -> None:
        try:
            import open_clip
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
            model, _, preprocess = open_clip.create_model_and_transforms(
                self.bundle.model,
                pretrained=str(self.bundle.checkpoint),
                device=device,
            )
            tokenizer = open_clip.get_tokenizer(self.bundle.model)
            model.eval()
            with self._state_lock:
                self._model = model
                self._preprocess = preprocess
                self._tokenizer = tokenizer
                self._device = device
                self._ready = True
                self._error = None
            logger.info("OpenCLIP %s loaded on %s", self.bundle.model, device)
        except Exception as exc:
            logger.exception("OpenCLIP preload failed")
            with self._state_lock:
                self._ready = False
                self._error = str(exc)

    def readiness(self) -> tuple[bool, str | None]:
        with self._state_lock:
            return self._ready, self._error

    def embed_text(self, inputs: list[str], normalize: bool) -> list[list[float]]:
        import torch

        with self._inference_lock, torch.inference_mode():
            tokens = self._tokenizer(inputs).to(self._device)
            features = self._model.encode_text(tokens)
            if normalize:
                features = features / features.norm(dim=-1, keepdim=True)
            return features.float().cpu().tolist()

    def embed_images(self, images: list[bytes], normalize: bool) -> list[list[float]]:
        import torch
        from PIL import Image

        with self._inference_lock, torch.inference_mode():
            batch = torch.stack(
                [self._preprocess(Image.open(io.BytesIO(item)).convert("RGB")) for item in images]
            ).to(self._device)
            features = self._model.encode_image(batch)
            if normalize:
                features = features / features.norm(dim=-1, keepdim=True)
            return features.float().cpu().tolist()
