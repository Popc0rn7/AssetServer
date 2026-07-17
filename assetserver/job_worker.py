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
import json
import tempfile
import time
from pathlib import Path

from collections.abc import Callable
from typing import Any

from assetserver.jobs import Job, JobHandler, JobWorker, SQLiteJobStore
from assetserver.runtime_version import register_runtime


logger = logging.getLogger("assetserver.scene-viewer")


def _capability_heartbeat(root: str | Path, worker_id: str,
                          handlers: set[str], stop: threading.Event,
                          ready: threading.Event | None = None) -> None:
    path = Path(root) / "runtime" / "scene-worker-capabilities.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    while not stop.is_set():
        payload = {"worker_id": worker_id, "capabilities": sorted(handlers),
                   "worker_build": os.environ.get("ASSETSERVER_BUILD_VERSION", "dev"),
                   "updated_at": time.time()}
        if "asset_observe" in handlers:
            from assetserver.asset_observe import canonical_scene
            payload["canonical_scene_version"] = canonical_scene()["schema_version"]
        descriptor, name = tempfile.mkstemp(prefix=".capabilities-", dir=path.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.write_text(json.dumps(payload, sort_keys=True) + "\n")
            os.replace(temporary, path)
            if ready is not None:
                ready.set()
        finally:
            temporary.unlink(missing_ok=True)
        stop.wait(5)


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
        format="%(asctime)s | %(levelname)-5s | scene-viewer | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
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

    logger.info("Initializing worker runtime...")
    handlers = dict(load_handler(value) for value in args.handler)
    if "asset_observe" in handlers:
        # Fail startup before advertising readiness when packaged configuration
        # or the Blender runtime is incomplete.
        from assetserver.asset_observe import canonical_scene
        canonical_scene()
        import bpy  # noqa: F401
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
        logger.info("Running one queued job and then exiting")
        worker.run_once()
        return

    stop = threading.Event()
    heartbeat_ready = threading.Event()
    capability_thread = threading.Thread(
        target=_capability_heartbeat,
        args=(os.environ.get("ASSETSERVER_DATA_ROOT", "data"), worker_id,
              set(handlers), stop, heartbeat_ready), daemon=True,
    )
    capability_thread.start()
    if not heartbeat_ready.wait(timeout=5):
        raise RuntimeError("capability heartbeat did not become ready")
    logger.info(
        "READY  worker=%s  handlers=[%s]",
        worker_id,
        ", ".join(sorted(handlers)),
    )
    logger.info(
        "Polling %s every %.1fs  (job heartbeat %.0fs, lease %.0fs)",
        args.database,
        args.poll_seconds,
        args.heartbeat_seconds,
        args.lease_seconds,
    )
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    worker.run_forever(poll_seconds=args.poll_seconds, stop=stop)
    capability_thread.join(timeout=6)


if __name__ == "__main__":
    main()
