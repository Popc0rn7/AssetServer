"""SQLite state for collision artifacts and parent-specific derivations."""

from __future__ import annotations

import json
import sqlite3
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_key: str
    status: str
    owner: str | None
    lease_expires_at: float | None
    attempt: int
    manifest: dict[str, Any] | None
    processing_time_s: float | None
    error_code: str | None
    error_message: str | None
    retryable: bool


class DerivationStore:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def acquire(
        self, key: str, owner: str, *, lease_seconds: float
    ) -> tuple[ArtifactRecord, bool]:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO artifacts
                   (artifact_key, status, attempt, retryable, created_at, updated_at)
                   VALUES (?, 'pending', 0, 0, ?, ?)""",
                (key, now, now),
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_key=?", (key,)
            ).fetchone()
            may_claim = row["status"] == "pending" or (
                row["status"] == "running"
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] < now
            ) or (row["status"] == "failed" and bool(row["retryable"]))
            if may_claim:
                connection.execute(
                    """UPDATE artifacts SET status='running', owner=?,
                       lease_expires_at=?, attempt=attempt+1, error_code=NULL,
                       error_message=NULL, updated_at=? WHERE artifact_key=?""",
                    (owner, now + lease_seconds, now, key),
                )
                row = connection.execute(
                    "SELECT * FROM artifacts WHERE artifact_key=?", (key,)
                ).fetchone()
            connection.commit()
            return self._record(row), may_claim
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, key: str) -> ArtifactRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_key=?", (key,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def complete(
        self,
        key: str,
        owner: str,
        manifest: dict[str, Any],
        processing_time_s: float,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE artifacts SET status='complete', owner=NULL,
                   lease_expires_at=NULL, manifest_json=?, processing_time_s=?,
                   retryable=0, updated_at=?
                   WHERE artifact_key=? AND owner=? AND status='running'""",
                (
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                    processing_time_s,
                    time.time(),
                    key,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("artifact lease was lost")

    def fail(
        self,
        key: str,
        owner: str,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE artifacts SET status='failed', owner=NULL,
                   lease_expires_at=NULL, error_code=?, error_message=?,
                   retryable=?, updated_at=?
                   WHERE artifact_key=? AND owner=? AND status='running'""",
                (code, message, int(retryable), time.time(), key, owner),
            )

    def invalidate(self, key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE artifacts SET status='pending', owner=NULL,
                   lease_expires_at=NULL, manifest_json=NULL, error_code=NULL,
                   error_message=NULL, updated_at=? WHERE artifact_key=?""",
                (time.time(), key),
            )

    def get_derivation(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT derived_asset_ref FROM derivations WHERE derivation_key=?",
                (key,),
            ).fetchone()
        return str(row[0]) if row else None

    def put_derivation(
        self, key: str, parent_asset_digest: str, artifact_key: str, asset_ref: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO derivations
                   (derivation_key, parent_asset_digest, artifact_key,
                    derived_asset_ref, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(derivation_key) DO UPDATE SET
                     derived_asset_ref=excluded.derived_asset_ref""",
                (key, parent_asset_digest, artifact_key, asset_ref, time.time()),
            )

    def delete_derivation(self, key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM derivations WHERE derivation_key=?", (key,)
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    owner TEXT,
                    lease_expires_at REAL,
                    attempt INTEGER NOT NULL,
                    manifest_json TEXT,
                    processing_time_s REAL,
                    error_code TEXT,
                    error_message TEXT,
                    retryable INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS derivations (
                    derivation_key TEXT PRIMARY KEY,
                    parent_asset_digest TEXT NOT NULL,
                    artifact_key TEXT NOT NULL,
                    derived_asset_ref TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS derivations_artifact
                    ON derivations(artifact_key);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_key=row["artifact_key"],
            status=row["status"],
            owner=row["owner"],
            lease_expires_at=row["lease_expires_at"],
            attempt=row["attempt"],
            manifest=json.loads(row["manifest_json"]) if row["manifest_json"] else None,
            processing_time_s=row["processing_time_s"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            retryable=bool(row["retryable"]),
        )
