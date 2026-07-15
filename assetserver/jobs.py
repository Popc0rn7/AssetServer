"""Durable SQLite-backed job queue for scene workers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from assetserver.runtime_version import scene_job_cache_version


JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
JobHandler = Callable[["Job"], dict[str, Any]]


class JobNotFoundError(ValueError):
    pass


class JobOwnershipError(RuntimeError):
    pass


class JobExecutionError(RuntimeError):
    def __init__(
        self, message: str, *, code: str = "job_failed", retryable: bool = False
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Job:
    job_id: str
    job_type: str
    scene_id: str
    scene_revision: int
    request: dict[str, Any]
    request_hash: str
    status: JobStatus
    progress: float
    worker_id: str | None
    lease_expires_at: float | None
    attempt: int
    max_attempts: int
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: float
    started_at: float | None
    finished_at: float | None
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SQLiteJobStore:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def submit(
        self,
        job_type: str,
        scene_id: str,
        scene_revision: int,
        request: dict[str, Any],
        *,
        max_attempts: int = 3,
        cache_version: str | None = None,
    ) -> tuple[Job, bool]:
        if not job_type or scene_revision < 1 or max_attempts < 1:
            raise ValueError("invalid job submission")
        request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
        cache_identity = json.dumps(
            {
                "request": request,
                "cache_version": cache_version or scene_job_cache_version(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(cache_identity.encode()).hexdigest()
        now = time.time()
        job_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    job_id, job_type, scene_id, scene_revision, request_json,
                    request_hash, status, max_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    scene_id,
                    scene_revision,
                    request_json,
                    request_hash,
                    max_attempts,
                    now,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            if not created:
                row = connection.execute(
                    """SELECT * FROM jobs WHERE job_type=? AND scene_id=?
                       AND scene_revision=? AND request_hash=?""",
                    (job_type, scene_id, scene_revision, request_hash),
                ).fetchone()
                if row["status"] not in {"failed", "cancelled"}:
                    connection.commit()
                    return self._job(row), False
                # Keep the terminal job addressable, but vacate its cache key so a
                # fresh submission can enter the queue. The tombstone is unique and
                # can never be selected by a future request hash.
                connection.execute(
                    "UPDATE jobs SET request_hash=? WHERE job_id=?",
                    (f"terminal:{row['job_id']}:{request_hash}", row["job_id"]),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, job_type, scene_id, scene_revision, request_json,
                        request_hash, status, max_attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (
                        job_id,
                        job_type,
                        scene_id,
                        scene_revision,
                        request_json,
                        request_hash,
                        max_attempts,
                        now,
                        now,
                    ),
                )
                created = cursor.rowcount == 1
            fresh = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            connection.commit()
            return self._job(fresh), created
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, job_id: str) -> Job:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(f"job not found: {job_id}")
        return self._job(row)

    def claim(self, worker_id: str, *, lease_seconds: float = 60.0) -> Job | None:
        if not worker_id or lease_seconds <= 0:
            raise ValueError("invalid worker lease")
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE jobs SET status='failed', error_code='lease_expired',
                   error_message='worker lease expired after maximum attempts',
                   retryable=0, worker_id=NULL, lease_expires_at=NULL,
                   finished_at=?, updated_at=?
                   WHERE status='running' AND lease_expires_at < ?
                     AND attempt >= max_attempts""",
                (now, now, now),
            )
            row = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE (status='queued' OR (status='running' AND lease_expires_at < ?))
                  AND attempt < max_attempts
                ORDER BY created_at, job_id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job_id = row["job_id"]
            connection.execute(
                """
                UPDATE jobs SET status='running', worker_id=?, lease_expires_at=?,
                    attempt=attempt+1, started_at=COALESCE(started_at, ?),
                    updated_at=?, retryable=0, progress=0,
                    error_code=NULL, error_message=NULL
                WHERE job_id=?
                """,
                (worker_id, now + lease_seconds, now, now, job_id),
            )
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            connection.commit()
            return self._job(claimed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 60.0,
        progress: float | None = None,
    ) -> Job:
        now = time.time()
        if progress is not None and not 0 <= progress <= 1:
            raise ValueError("progress must be between 0 and 1")
        assignments = "lease_expires_at=?, updated_at=?"
        parameters: list[Any] = [now + lease_seconds, now]
        if progress is not None:
            assignments += ", progress=?"
            parameters.append(progress)
        parameters.extend([job_id, worker_id])
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id=? AND worker_id=? AND status='running'",
                parameters,
            )
            if cursor.rowcount != 1:
                raise JobOwnershipError("job is not leased by this worker")
        return self.get(job_id)

    def complete(self, job_id: str, worker_id: str, result: dict[str, Any]) -> Job:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE jobs SET status='completed', progress=1, result_json=?,
                   lease_expires_at=NULL, finished_at=?, updated_at=?, retryable=0
                   WHERE job_id=? AND worker_id=? AND status='running'""",
                (json.dumps(result, sort_keys=True), now, now, job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise JobOwnershipError("job is not leased by this worker")
        return self.get(job_id)

    def fail(
        self,
        job_id: str,
        worker_id: str,
        *,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> Job:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempt, max_attempts FROM jobs WHERE job_id=? AND worker_id=? AND status='running'",
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                raise JobOwnershipError("job is not leased by this worker")
            requeue = retryable and row["attempt"] < row["max_attempts"]
            connection.execute(
                """UPDATE jobs SET status=?, worker_id=NULL, lease_expires_at=NULL,
                   error_code=?, error_message=?, retryable=?, finished_at=?, updated_at=?
                   WHERE job_id=?""",
                (
                    "queued" if requeue else "failed",
                    code,
                    message,
                    int(retryable),
                    None if requeue else now,
                    now,
                    job_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(job_id)

    def cancel(self, job_id: str) -> Job:
        self.get(job_id)
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE jobs SET status='cancelled', lease_expires_at=NULL,
                   finished_at=?, updated_at=? WHERE job_id=? AND status='queued'""",
                (now, now, job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("only queued jobs can be cancelled")
        return self.get(job_id)

    def delete_completed(self, job_id: str) -> bool:
        """Remove a completed cache record whose retained result has disappeared."""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM jobs WHERE job_id=? AND status='completed'", (job_id,)
            )
        return cursor.rowcount == 1

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(
                    f"unsupported jobs database schema version: {version}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    scene_id TEXT NOT NULL,
                    scene_revision INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
                    progress REAL NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    lease_expires_at REAL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    updated_at REAL NOT NULL,
                    UNIQUE(job_type, scene_id, scene_revision, request_hash)
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS jobs_lease ON jobs(status, lease_expires_at);
                """
            )
            connection.execute("PRAGMA user_version=1")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            job_type=row["job_type"],
            scene_id=row["scene_id"],
            scene_revision=row["scene_revision"],
            request=json.loads(row["request_json"]),
            request_hash=row["request_hash"],
            status=row["status"],
            progress=row["progress"],
            worker_id=row["worker_id"],
            lease_expires_at=row["lease_expires_at"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error_code=row["error_code"],
            error_message=row["error_message"],
            retryable=bool(row["retryable"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            updated_at=row["updated_at"],
        )


class JobWorker:
    def __init__(
        self,
        store: SQLiteJobStore,
        worker_id: str,
        handlers: dict[str, JobHandler],
        *,
        lease_seconds: float = 60,
        heartbeat_seconds: float = 20,
    ) -> None:
        if heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat interval must be shorter than lease")
        self.store = store
        self.worker_id = worker_id
        self.handlers = handlers
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    def run_once(self) -> Job | None:
        job = self.store.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return None
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat, args=(job.job_id, stop), daemon=True
        )
        heartbeat.start()
        try:
            handler = self.handlers.get(job.job_type)
            if handler is None:
                raise JobExecutionError(
                    f"unsupported job type: {job.job_type}", code="unsupported_job_type"
                )
            result = handler(job)
            return self.store.complete(job.job_id, self.worker_id, result)
        except JobExecutionError as exc:
            return self.store.fail(
                job.job_id,
                self.worker_id,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
        except Exception as exc:
            return self.store.fail(
                job.job_id,
                self.worker_id,
                code="worker_error",
                message=str(exc),
                retryable=True,
            )
        finally:
            stop.set()
            heartbeat.join()

    def run_forever(
        self, *, poll_seconds: float = 1.0, stop: threading.Event | None = None
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll interval must be positive")
        stop = stop or threading.Event()
        while not stop.is_set():
            if self.run_once() is None:
                stop.wait(poll_seconds)

    def _heartbeat(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            try:
                self.store.heartbeat(
                    job_id, self.worker_id, lease_seconds=self.lease_seconds
                )
            except JobOwnershipError:
                return
