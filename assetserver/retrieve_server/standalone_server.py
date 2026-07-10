"""Command-line entry point for the unified retrieve server."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from assetserver.config import load_assetserver_config

from .server_manager import RetrieveServer

console_logger = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AssetServer retrieve server")
    parser.add_argument("--config", default=None, help="Path to server YAML config")
    parser.add_argument("--host", default=None, help="Override configured host")
    parser.add_argument(
        "--port", type=int, default=None, help="Override configured port"
    )
    parser.add_argument(
        "--clip-device", default=None, help="Override configured CLIP device"
    )
    parser.add_argument(
        "--preload",
        action="store_true",
        default=None,
        help="Preload configured retrievers on startup.",
    )
    parser.add_argument(
        "--no-preload",
        action="store_false",
        dest="preload",
        help="Load retrievers lazily on first request.",
    )
    parser.add_argument(
        "--no-openclip-warmup",
        action="store_true",
        help="Do not warm up OpenCLIP at server startup.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    return parser.parse_args()


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def main() -> int:
    args = parse_arguments()
    setup_logging(args.verbose)

    cfg = load_assetserver_config(config_path=args.config)
    retrieve_cfg = cfg.runtime.get("retrieve_server", {})
    host = args.host or retrieve_cfg.get("host", "127.0.0.1")
    port = args.port or int(retrieve_cfg.get("port", 7005))
    preload = (
        bool(retrieve_cfg.get("preload", True))
        if args.preload is None
        else bool(args.preload)
    )
    clip_device = args.clip_device or retrieve_cfg.get("clip_device")

    server = RetrieveServer(
        host=host,
        port=port,
        config=cfg,
        preload_retrievers=preload,
        clip_device=clip_device,
        warmup_openclip=not args.no_openclip_warmup,
    )

    def signal_handler(signum, _) -> None:
        console_logger.info("Received signal %s, shutting down", signum)
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        server.start()
        console_logger.info("Retrieve server running at http://%s:%s", host, port)
        while server.is_running():
            time.sleep(1)
    except Exception as exc:
        console_logger.error("Failed to start retrieve server: %s", exc)
        return 1
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
