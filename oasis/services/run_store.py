"""Small SQLite repository for durable API run state and private artifacts."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


_JSON_COLUMNS = {
    "spec": "spec_json",
    "progress": "progress_json",
    "preview_result": "preview_json",
    "result": "result_json",
}
_PLAIN_COLUMNS = {"status", "phase", "error", "cancel_reason"}


def _now() -> str:
    """Return one sortable UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    """Encode already-public data compactly for SQLite."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class RunStore:
    """Persist run metadata while keeping filesystem paths out of public snapshots."""

    def __init__(self, path: str | Path):
        """Create the database and its compact normalized schema."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    parent_run_id TEXT,
                    revision INTEGER NOT NULL DEFAULT 0,
                    spec_json TEXT NOT NULL,
                    candidate_upload_id TEXT,
                    status TEXT NOT NULL,
                    phase TEXT,
                    progress_json TEXT,
                    preview_json TEXT,
                    result_json TEXT,
                    error TEXT,
                    cancel_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(parent_run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, seq),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS run_artifacts (
                    run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    path TEXT NOT NULL,
                    PRIMARY KEY(run_id, name),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id);
            """)

    def _connect(self) -> sqlite3.Connection:
        """Open one thread-local connection with bounded writer waiting."""
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def create(
        self,
        run_id: str,
        spec: dict[str, Any],
        *,
        parent_run_id: str | None = None,
        revision: int = 0,
        candidate_upload_id: str | None = None,
    ) -> None:
        """Insert one immutable run specification in the queued state."""
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs (run_id,parent_run_id,revision,spec_json,candidate_upload_id,"
                "status,created_at,updated_at) VALUES (?,?,?,?,?,'queued',?,?)",
                (run_id, parent_run_id, revision, _dump(spec), candidate_upload_id, now, now),
            )

    def interrupt_active(self) -> int:
        """Mark work lost with the prior process as safely resumable, never replay it."""
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status='interrupted', progress_json=NULL, "
                "error=COALESCE(error, ?), updated_at=? "
                "WHERE status IN ('queued','running','cancelling')",
                ("Backend restarted; create a revision to resume from durable caches.", now),
            )
            return cursor.rowcount

    def get(self, run_id: str) -> dict[str, Any]:
        """Return one private decoded record for service-layer decisions."""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown run")
            events = [json.loads(item[0]) for item in connection.execute(
                "SELECT payload_json FROM run_events WHERE run_id=? ORDER BY seq", (run_id,)
            )]
        record = dict(row)
        for public, column in _JSON_COLUMNS.items():
            raw = record.pop(column)
            record[public] = json.loads(raw) if raw else None
        record["events"] = events
        return record

    def snapshot(self, run_id: str) -> dict[str, Any]:
        """Return the stable public polling payload without private paths or upload IDs."""
        record = self.get(run_id)
        spec = record["spec"]
        return {
            "run_id": record["run_id"],
            "parent_run_id": record["parent_run_id"],
            "revision": record["revision"],
            "status": record["status"],
            "phase": record["phase"],
            "mode": spec["mode"],
            "spec": spec,
            "task": spec["task"],
            "events": record["events"],
            "progress": record["progress"],
            "preview_result": record["preview_result"],
            "result": record["result"],
            "error": record["error"],
            "cancel_reason": record["cancel_reason"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    def update(self, run_id: str, **changes: Any) -> None:
        """Update mutable execution state while rejecting unknown columns."""
        if not changes:
            return
        assignments, values = [], []
        for name, value in changes.items():
            if name in _JSON_COLUMNS:
                assignments.append(f"{_JSON_COLUMNS[name]}=?")
                values.append(None if value is None else _dump(value))
            elif name in _PLAIN_COLUMNS:
                assignments.append(f"{name}=?")
                values.append(value)
            else:
                raise ValueError(f"Unsupported run field: {name}")
        assignments.append("updated_at=?")
        values.extend((_now(), run_id))
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {','.join(assignments)} WHERE run_id=?", values
            )
            if not cursor.rowcount:
                raise KeyError("Unknown run")

    def transition(self, run_id: str, expected: Iterable[str], status: str, **changes: Any) -> bool:
        """Apply one race-safe state transition and optional payload update."""
        expected = tuple(expected)
        if not expected:
            return False
        assignments, values = ["status=?"], [status]
        for name, value in changes.items():
            if name in _JSON_COLUMNS:
                assignments.append(f"{_JSON_COLUMNS[name]}=?")
                values.append(None if value is None else _dump(value))
            elif name in _PLAIN_COLUMNS - {"status"}:
                assignments.append(f"{name}=?")
                values.append(value)
            else:
                raise ValueError(f"Unsupported run field: {name}")
        placeholders = ",".join("?" for _ in expected)
        assignments.append("updated_at=?")
        values.extend((_now(), run_id, *expected))
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {','.join(assignments)} WHERE run_id=? "
                f"AND status IN ({placeholders})",
                values,
            )
            return bool(cursor.rowcount)

    def append_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Append one monotonically sequenced public event."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                connection.rollback()
                raise KeyError("Unknown run")
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM run_events WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            payload = {"seq": seq, **event}
            connection.execute(
                "INSERT INTO run_events (run_id,seq,payload_json) VALUES (?,?,?)",
                (run_id, seq, _dump(payload)),
            )
            connection.commit()
        return payload

    def put_artifacts(self, run_id: str, phase: str, artifacts: dict[str, Path]) -> None:
        """Persist allow-listed private paths under stable public names."""
        rows = [(run_id, name, phase, str(path)) for name, path in artifacts.items()]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO run_artifacts (run_id,name,phase,path) VALUES (?,?,?,?)", rows
            )

    def artifact(self, run_id: str, name: str) -> Path:
        """Resolve one private artifact path without exposing the manifest."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT path FROM run_artifacts WHERE run_id=? AND name=?", (run_id, name)
            ).fetchone()
        if row is None:
            raise KeyError("Unknown artifact")
        return Path(row[0])
