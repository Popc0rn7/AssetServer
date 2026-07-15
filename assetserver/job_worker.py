"""CLI lifecycle for a SQLite scene job worker."""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
import socket
import threading
import uuid

from collections.abc import Callable
from typing import Any

from assetserver.jobs import Job, JobHandler, JobWorker, SQLiteJobStore
from assetserver.runtime_version import register_runtime


logger = logging.getLogger(__name__)


def load_handler(specification: str) -> tuple[str, JobHandler]:
    """Load ``job_type=module:function`` without coupling the queue to runtimes."""
    try:
        job_type, target = specification.split("=", 1)
        module_name, function_name = target.split(":", 1)
    except ValueError as exc:
        raise ValueError("handler must be job_type=module:function") from exc
    function: Callable[[Job], dict[str, Any]] = getattr(
        importlib.import_module(module_name), function_name
    )
    return job_type, function


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Run a persistent AssetServer job worker"
    )
    parser.add_argument("--database", default="data/jobs/jobs.sqlite3")
    parser.add_argument(
        "--handler", action="append", default=[], metavar="TYPE=MODULE:FUNCTION"
    )
    parser.add_argument("--worker-id")
    parser.add_argument("--lease-seconds", type=float, default=300)
    parser.add_argument("--heartbeat-seconds", type=float, default=30)
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    handlers = dict(load_handler(value) for value in args.handler)
    worker_id = args.worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    register_runtime(
        os.environ.get("ASSETSERVER_DATA_ROOT", "data"),
        role="scene-worker",
        instance_id=worker_id,
        logger=logger,
    )
    worker = JobWorker(
        SQLiteJobStore(args.database),
        worker_id,
        handlers,
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
    )
    if args.once:
        worker.run_once()
        return

    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    worker.run_forever(poll_seconds=args.poll_seconds, stop=stop)


if __name__ == "__main__":
    main()
