import time
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from assetserver.jobs import (
    JobExecutionError,
    JobOwnershipError,
    JobWorker,
    SQLiteJobStore,
)


def test_submit_is_durable_and_idempotent(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(path)
    first, created = store.submit("observe", "scene", 2, {"views": ["top"]})
    second, duplicate_created = SQLiteJobStore(path).submit(
        "observe", "scene", 2, {"views": ["top"]}
    )
    assert created is True
    assert duplicate_created is False
    assert second.job_id == first.job_id


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_terminal_job_is_never_a_valid_dedup_target(tmp_path, terminal):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    first, _ = store.submit("observe", "scene", 1, {"views": ["top"]})
    if terminal == "failed":
        store.claim("worker")
        store.fail(
            first.job_id,
            "worker",
            code="schema_error",
            message="old worker rejected the scene",
            retryable=False,
        )
    else:
        store.cancel(first.job_id)

    second, created = store.submit("observe", "scene", 1, {"views": ["top"]})

    assert created is True
    assert second.job_id != first.job_id
    assert second.status == "queued"
    assert store.claim("new-worker").job_id == second.job_id
    assert store.get(first.job_id).status == terminal


def test_completed_job_deduplicates_but_cache_version_change_does_not(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    first, _ = store.submit("observe", "scene", 1, {}, cache_version="worker/v1")
    store.claim("worker")
    store.complete(first.job_id, "worker", {"ok": True})

    duplicate, created = store.submit(
        "observe", "scene", 1, {}, cache_version="worker/v1"
    )
    upgraded, upgraded_created = store.submit(
        "observe", "scene", 1, {}, cache_version="worker/v2"
    )

    assert created is False
    assert duplicate.job_id == first.job_id
    assert upgraded_created is True
    assert upgraded.job_id != first.job_id


def test_concurrent_workers_claim_job_exactly_once(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    submitted, _ = store.submit("observe", "scene", 1, {})
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda index: store.claim(f"worker-{index}"), range(8)))
    jobs = [job for job in claimed if job]
    assert len(jobs) == 1
    assert jobs[0].job_id == submitted.job_id
    assert jobs[0].attempt == 1


def test_heartbeat_and_completion_require_lease_owner(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    job, _ = store.submit("validate", "scene", 1, {})
    store.claim("worker")
    with pytest.raises(JobOwnershipError):
        store.heartbeat(job.job_id, "other")
    running = store.heartbeat(job.job_id, "worker", progress=0.5)
    assert running.progress == 0.5
    completed = store.complete(job.job_id, "worker", {"valid": True})
    assert completed.status == "completed"
    assert completed.result == {"valid": True}


def test_expired_lease_retries_then_fails_at_limit(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    job, _ = store.submit("observe", "scene", 1, {}, max_attempts=2)
    store.claim("dead-1", lease_seconds=0.001)
    time.sleep(0.01)
    retried = store.claim("worker-2", lease_seconds=0.001)
    assert retried.attempt == 2
    time.sleep(0.01)
    assert store.claim("worker-3") is None
    failed = store.get(job.job_id)
    assert failed.status == "failed"
    assert failed.error_code == "lease_expired"


def test_retryable_failure_requeues_and_worker_dispatches(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    job, _ = store.submit("export", "scene", 1, {})
    attempts = []

    def handler(claimed):
        attempts.append(claimed.attempt)
        if len(attempts) == 1:
            raise JobExecutionError("temporary", retryable=True)
        return {"path": "outputs/scene.zip"}

    worker = JobWorker(
        store, "worker", {"export": handler}, lease_seconds=1, heartbeat_seconds=0.1
    )
    assert worker.run_once().status == "queued"
    assert worker.run_once().status == "completed"
    assert store.get(job.job_id).result["path"] == "outputs/scene.zip"


def test_worker_run_forever_stops_cleanly(tmp_path):
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    job, _ = store.submit("validate", "scene", 1, {})
    stop = threading.Event()

    def handler(_job):
        stop.set()
        return {"valid": True}

    worker = JobWorker(
        store, "worker", {"validate": handler}, lease_seconds=1, heartbeat_seconds=0.1
    )
    worker.run_forever(poll_seconds=0.01, stop=stop)
    assert store.get(job.job_id).status == "completed"
