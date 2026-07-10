#!/usr/bin/env python3
"""Standalone materials retrieval server entry point."""

import argparse
import logging
import signal
import sys
import time

from pathlib import Path

from assetserver.materials_retrieval.config import MaterialsConfig

from .server_manager import MaterialsRetrievalServer

console_logger = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7004)
    parser.add_argument("--preload", action="store_true", default=True)
    parser.add_argument("--no-preload", action="store_false", dest="preload")
    parser.add_argument("--materials-data-path", default="data/materials")
    parser.add_argument("--materials-embeddings-path", default="data/materials/embeddings")
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
    config = MaterialsConfig(
        data_path=Path(args.materials_data_path),
        embeddings_path=Path(args.materials_embeddings_path),
        use_top_k=args.top_k,
    )
    server = MaterialsRetrievalServer(
        host=args.host,
        port=args.port,
        preload_retriever=args.preload,
        materials_config=config,
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
