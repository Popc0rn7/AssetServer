"""Lifecycle, serialization, and background execution for one pipeline."""

from __future__ import annotations

import asyncio
import logging
import threading
import traceback

from pathlib import Path

from .protocol import GenerationPipeline, GenerationRequest

logger = logging.getLogger(__name__)


class GenerationRuntime:
    """Own one resident pipeline and execute at most one request at a time."""

    def __init__(self, pipeline: GenerationPipeline, *, preload: bool) -> None:
        self.pipeline = pipeline
        self.preload = preload
        self._request_lock = asyncio.Lock()
        self._load_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._loaded = False
        self._loading = False
        self._load_error: str | None = None
        self._preload_started = False

    def start(self) -> None:
        if not self.preload:
            return
        with self._state_lock:
            if self._preload_started:
                return
            self._preload_started = True
            self._loading = True
        threading.Thread(
            target=self._preload,
            name=f"{self.pipeline.name}-preload",
            daemon=True,
        ).start()

    def _preload(self) -> None:
        try:
            self._ensure_loaded()
        except Exception:
            # _ensure_loaded records the complete traceback for readiness.
            logger.exception("Failed to preload %s pipeline", self.pipeline.name)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            with self._state_lock:
                self._loading = True
                self._load_error = None
            try:
                self.pipeline.load()
            except Exception:
                failure = traceback.format_exc()
                with self._state_lock:
                    self._loading = False
                    self._load_error = failure
                raise
            with self._state_lock:
                self._loaded = True
                self._loading = False
                self._load_error = None

    def readiness(self) -> tuple[bool, str | None]:
        with self._state_lock:
            if self._load_error is not None:
                return False, self._load_error
            if self.preload and not self._loaded:
                return False, "pipeline is loading" if self._loading else "pipeline preload has not started"
            return True, None

    async def generate(self, request: GenerationRequest, output_path: Path) -> None:
        async with self._request_lock:
            task = asyncio.create_task(
                asyncio.to_thread(self._generate_sync, request, output_path)
            )
            # Polling also guarantees regular event-loop wakeups on runtimes whose
            # selector self-pipe does not reliably wake for executor callbacks.
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.1)
                task.result()
            except asyncio.CancelledError:
                # A cancelled HTTP request cannot release the serialization lock
                # while its non-cancellable model thread is still using the GPU.
                while not task.done():
                    await asyncio.wait({task}, timeout=0.1)
                try:
                    task.result()
                except Exception:
                    logger.exception(
                        "Generation failed after caller cancellation for %s",
                        self.pipeline.name,
                    )
                raise

    def _generate_sync(self, request: GenerationRequest, output_path: Path) -> None:
        try:
            self._ensure_loaded()
            self.pipeline.generate(request, output_path)
        finally:
            try:
                self.pipeline.cleanup_request()
            except Exception:
                logger.exception("Request cleanup failed for %s", self.pipeline.name)
