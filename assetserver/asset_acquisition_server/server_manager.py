import logging
import threading

from collections.abc import Callable
from threading import Thread
from typing import Any

import requests
import uvicorn

from assetserver.utils.network_utils import is_port_available

from .dataclasses import (
    AssetAcquisitionServerRequest,
    AssetAcquisitionServerResponse,
)
from .server_app import AssetAcquisitionApp

console_logger = logging.getLogger(__name__)

GenerateAssetsHandler = Callable[
    [AssetAcquisitionServerRequest], AssetAcquisitionServerResponse
]


class AssetAcquisitionServer:
    """Lifecycle manager for the unified asset acquisition HTTP server."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7010,
        generate_assets_handler: GenerateAssetsHandler | None = None,
        config: Any | None = None,
    ) -> None:
        if not is_port_available(host, port):
            raise ValueError(f"Port {port} is not available on {host}")

        self._host = host
        self._port = port
        self._generate_assets_handler = generate_assets_handler
        self._config = config
        self._app: AssetAcquisitionApp | None = None
        self._http_server: uvicorn.Server | None = None
        self._server_thread: Thread | None = None
        self._running = False
        self._shutdown_event = threading.Event()

    def start(self) -> None:
        if self._running:
            raise RuntimeError("Asset acquisition server is already running")

        self._app = AssetAcquisitionApp(
            generate_assets_handler=self._generate_assets_handler,
            config=self._config,
        )
        self._server_thread = Thread(target=self._run_server, daemon=False)
        self._server_thread.start()
        self.wait_until_ready()
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return

        self._shutdown_event.set()
        if self._http_server is not None:
            self._http_server.should_exit = True

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)

        self._running = False

    def wait_until_ready(self, timeout_s: float = 10.0) -> None:
        import time

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"http://{self._host}:{self._port}/health", timeout=1
                )
                if response.status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.1)
        raise TimeoutError("Asset acquisition server did not become ready")

    def is_running(self) -> bool:
        return self._running

    def _run_server(self) -> None:
        if self._app is None:
            raise RuntimeError("Server app was not initialized")
        config = uvicorn.Config(
            app=self._app,
            host=self._host,
            port=self._port,
            log_level="info",
        )
        self._http_server = uvicorn.Server(config)
        self._http_server.run()
