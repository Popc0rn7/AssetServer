"""Persistent metadata catalog for immutable, HTTP-downloadable artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    logical_key: str
    kind: str
    media_type: str
    path: Path
    sha256: str
    size_bytes: int
    created_at: float
    provenance: dict[str, Any]
    metadata: dict[str, Any]
    gone: bool

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": "artifact/v1",
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_url": f"/v2/artifacts/{self.artifact_id}/content",
            "created_at": datetime.fromtimestamp(self.created_at, UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "provenance": self.provenance,
            "metadata": self.metadata,
        }


class ArtifactCatalog:
    """SQLite catalog whose identifiers are never reassigned after retirement."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    logical_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    provenance_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    gone INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS artifacts_active_key
                    ON artifacts(logical_key) WHERE gone=0;
                """
            )

    def publish(
        self,
        logical_key: str,
        path: str | Path,
        *,
        kind: str,
        media_type: str,
        provenance: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        file_path = Path(path).resolve()
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM artifacts WHERE logical_key=? AND gone=0",
                (logical_key,),
            ).fetchone()
            if row is not None:
                current = self._artifact(row)
                if current.path.is_file():
                    current_digest, current_size = _digest(current.path)
                    if (
                        current_digest == current.sha256
                        and current_size == current.size_bytes
                    ):
                        return current
                connection.execute(
                    "UPDATE artifacts SET gone=1 WHERE artifact_id=?",
                    (current.artifact_id,),
                )
            digest, size = _digest(file_path)
            artifact_id = f"art_{uuid.uuid4().hex}"
            created_at = time.time()
            connection.execute(
                """INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    artifact_id,
                    logical_key,
                    kind,
                    media_type,
                    str(file_path),
                    digest,
                    size,
                    created_at,
                    json.dumps(provenance or {}, sort_keys=True),
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
        return self.get(artifact_id)

    def publish_many(self, items: list[dict[str, Any]]) -> list[Artifact]:
        """Publish a set of files in one catalog transaction.

        Every input is validated and digested before the transaction.  Thus a
        missing render can never leave a partially visible artifact set.
        """
        prepared = []
        for item in items:
            path = Path(item["path"]).resolve()
            if not path.is_file():
                raise FileNotFoundError(path)
            digest, size = _digest(path)
            prepared.append((item, path, digest, size))
        ids: list[str] = []
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for item, path, digest, size in prepared:
                    row = connection.execute(
                        "SELECT * FROM artifacts WHERE logical_key=? AND gone=0",
                        (item["logical_key"],),
                    ).fetchone()
                    if row is not None:
                        current = self._artifact(row)
                        if (current.path.is_file() and current.sha256 == digest and
                            current.size_bytes == size):
                            ids.append(current.artifact_id)
                            continue
                        connection.execute(
                            "UPDATE artifacts SET gone=1 WHERE artifact_id=?",
                            (current.artifact_id,),
                        )
                    artifact_id = f"art_{uuid.uuid4().hex}"
                    connection.execute(
                        "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                        (artifact_id, item["logical_key"], item["kind"],
                         item["media_type"], str(path), digest, size, time.time(),
                         json.dumps(item.get("provenance") or {}, sort_keys=True),
                         json.dumps(item.get("metadata") or {}, sort_keys=True)),
                    )
                    ids.append(artifact_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return [self.get(artifact_id) for artifact_id in ids]

    def get(self, artifact_id: str) -> Artifact:
        if not artifact_id.startswith("art_"):
            raise KeyError(artifact_id)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise KeyError(artifact_id)
            artifact = self._artifact(row)
            if not artifact.gone:
                valid = artifact.path.is_file()
                if valid:
                    digest, size = _digest(artifact.path)
                    valid = digest == artifact.sha256 and size == artifact.size_bytes
                if not valid:
                    connection.execute(
                        "UPDATE artifacts SET gone=1 WHERE artifact_id=?",
                        (artifact_id,),
                    )
                    artifact = replace(artifact, gone=True)
            return artifact

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(
            artifact_id=row["artifact_id"],
            logical_key=row["logical_key"],
            kind=row["kind"],
            media_type=row["media_type"],
            path=Path(row["path"]),
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            created_at=row["created_at"],
            provenance=json.loads(row["provenance_json"]),
            metadata=json.loads(row["metadata_json"]),
            gone=bool(row["gone"]),
        )


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
