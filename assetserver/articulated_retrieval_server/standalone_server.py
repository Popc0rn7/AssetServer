#!/usr/bin/env python3
"""Standalone articulated retrieval server entry point."""

import argparse
import logging
import signal
import sys
import time

from pathlib import Path

from assetserver.articulated_retrieval.config import (
    ArticulatedConfig,
    ArticulatedSourceConfig,
)

from .server_manager import ArticulatedRetrievalServer

console_logger = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7003)
    parser.add_argument("--preload", action="store_true", default=True)
    parser.add_argument("--no-preload", action="store_false", dest="preload")
    parser.add_argument("--artvip-data-path", default="data/artvip_sdf")
    parser.add_argument("--artvip-embeddings-path", default="data/artvip_sdf/embeddings")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--clip-device", default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    config = ArticulatedConfig(
        sources={
            "artvip": ArticulatedSourceConfig(
                name="artvip",
                enabled=True,
                data_path=Path(args.artvip_data_path),
                embeddings_path=Path(args.artvip_embeddings_path),
            )
        },
        use_top_k=args.top_k,
    )
    server = ArticulatedRetrievalServer(
        host=args.host,
        port=args.port,
        preload_retriever=args.preload,
        articulated_config=config,
        clip_device=args.clip_device,
    )

    def signal_handler(signum, _):
        console_logger.info("Received signal %s, shutting down", signum)
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        server.start()
        while server.is_running():
            time.sleep(1)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
