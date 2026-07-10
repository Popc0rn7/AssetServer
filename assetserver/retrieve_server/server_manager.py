"""Lifecycle manager for the unified retrieve server."""

from __future__ import annotations

import logging
import threading
import time

from threading import Thread
from typing import Any

import requests
import uvicorn

from assetserver.config import enabled_backend_specs
from assetserver.utils.network_utils import is_port_available

from .server_app import RetrieveServerApp

console_logger = logging.getLogger(__name__)


class RetrieveServer:
    """Run the unified retrieve FastAPI app in a managed thread."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7005,
        config: Any | None = None,
        preload_retrievers: bool = True,
        clip_device: str | None = None,
        warmup_openclip: bool = True,
    ) -> None:
        if not is_port_available(host, port):
            raise ValueError(f"Port {port} is not available on {host}")

        self._host = host
        self._port = port
        self._config = config
        self._preload_retrievers = preload_retrievers
        self._clip_device = clip_device
        self._warmup_openclip = warmup_openclip
        self._app: RetrieveServerApp | None = None
        self._http_server: uvicorn.Server | None = None
        self._server_thread: Thread | None = None
        self._running = False
        self._shutdown_event = threading.Event()

    def start(self) -> None:
        if self._running:
            raise RuntimeError("Server is already running")

        try:
            backend_specs = enabled_backend_specs(self._config) if self._config else []
            self._app = RetrieveServerApp(
                backend_specs=backend_specs,
                preload_retrievers=self._preload_retrievers,
                clip_device=self._clip_device,
                warmup_openclip=self._warmup_openclip,
            )
            self._app.start_processing()
            self._server_thread = Thread(target=self._run_server, daemon=False)
            self._server_thread.start()
            self._wait_until_ready()
            self._running = True
            console_logger.info(
                "Retrieve server ready on %s:%s", self._host, self._port
            )
        except Exception:
            self._cleanup()
            raise

    def stop(self) -> None:
        if not self._running:
            return
        self._shutdown_event.set()
        if self._app:
            self._app.stop_processing()
        if self._http_server is not None:
            self._http_server.should_exit = True
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)
            if self._server_thread.is_alive():
                console_logger.warning("Retrieve server thread did not stop gracefully")
        self._cleanup()

    def wait_until_ready(self, timeout_s: float = 30) -> None:
        if not self._running:
            raise RuntimeError("Server is not running")
        self._wait_until_ready(timeout_s)

    def is_running(self) -> bool:
        return self._running

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def _run_server(self) -> None:
        try:
            config = uvicorn.Config(
                app=self._app,
                host=self._host,
                port=self._port,
                log_level="info",
            )
            self._http_server = uvicorn.Server(config)
            self._http_server.run()
        except Exception as exc:
            console_logger.error("Retrieve server thread failed: %s", exc)
            self._shutdown_event.set()

    def _wait_until_ready(self, timeout: float = 30) -> None:
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = requests.get(
                    f"http://{self._host}:{self._port}/health", timeout=1
                )
                if response.status_code == 200:
                    return
            except requests.exceptions.RequestException:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"Server did not become ready within {timeout} seconds")

    def _cleanup(self) -> None:
        self._running = False
        self._app = None
        self._http_server = None
        self._server_thread = None
        self._shutdown_event.clear()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
