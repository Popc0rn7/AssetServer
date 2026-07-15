import argparse
import logging
import signal
import time
import uuid

from pathlib import Path

from assetserver.config import enabled_backend_specs, load_assetserver_config
from assetserver.runtime_version import register_runtime

from .server_manager import AssetAcquisitionServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
console_logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the unified asset acquisition HTTP server"
    )
    parser.add_argument("--config", default=None, help="Path to server YAML config")
    parser.add_argument("--host", default=None, help="Override configured host")
    parser.add_argument(
        "--port", type=int, default=None, help="Override configured port"
    )
    args = parser.parse_args()

    cfg = load_assetserver_config(config_path=args.config)
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    register_runtime(
        Path(str(cfg.server.storage.data_root)),
        role="api",
        instance_id=f"{host}-{port}-{uuid.uuid4().hex[:8]}",
        logger=console_logger,
    )

    server = AssetAcquisitionServer(host=host, port=port, config=cfg)

    def handle_shutdown(signum, frame) -> None:
        console_logger.info("Received shutdown signal")
        server.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    server.start()
    console_logger.info("Asset acquisition server running at http://%s:%s", host, port)
    console_logger.info(
        "Loaded config %s with %s enabled backend(s)",
        cfg.config_path,
        len(enabled_backend_specs(cfg)),
    )
    console_logger.info(
        "/generate_assets proxies to the enabled generate backend when no "
        "AssetManager handler is wired."
    )

    try:
        while server.is_running():
            time.sleep(1)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
