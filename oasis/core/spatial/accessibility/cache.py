"""Passive durable storage for complete matrices and directed route pairs."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

import numpy as np

if TYPE_CHECKING:
    from .builder import PairKey, PairRequest

log = logging.getLogger(__name__)


class CacheStore:
    """Persist opaque matrix keys and externally constructed pair keys."""

    def __init__(
        self,
        root: Path,
        *,
        ttl_days: float | None = None,
        max_size_gb: float | None = None,
    ):
        self.root = Path(root)
        self.ttl_seconds = (
            float(ttl_days) * 86_400 if ttl_days is not None and ttl_days > 0 else None
        )
        self.max_size_bytes = (
            int(float(max_size_gb) * 1024**3)
            if max_size_gb is not None and max_size_gb > 0
            else None
        )
        self._connection: sqlite3.Connection | None = None

    def load_matrix(self, key: str, expected_shape: tuple[int, int]) -> np.ndarray | None:
        """Return one valid complete matrix for an opaque caller-supplied key."""
        path = self.root / "matrices" / f"matrix_{key}.npz"
        if not path.is_file():
            return None
        if self.ttl_seconds is not None and time.time() - path.stat().st_mtime > self.ttl_seconds:
            path.unlink(missing_ok=True)
            return None
        try:
            with np.load(path, allow_pickle=False) as bundle:
                matrix = np.asarray(bundle["matrix"], dtype=float)
            if matrix.shape != expected_shape:
                raise ValueError(
                    f"cached matrix shape {matrix.shape} does not match {expected_shape}"
                )
            if np.isnan(matrix).any() or (matrix < 0).any():
                raise ValueError("cached matrix contains invalid travel times")
            return matrix
        except (OSError, ValueError, KeyError) as exc:
            log.warning("ignoring invalid matrix cache %s: %s", path, exc)
            return None

    def save_matrix(
        self,
        key: str,
        matrix: np.ndarray,
        eligible_mask: np.ndarray,
        metadata: dict[str, Any],
    ) -> Path:
        """Atomically save one complete matrix under an opaque caller key."""
        path = self.root / "matrices" / f"matrix_{key}.npz"
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".npz"
        )
        os.close(descriptor)
        try:
            np.savez_compressed(
                temporary,
                matrix=np.asarray(matrix, dtype=np.float32),
                eligible_mask=np.asarray(eligible_mask, dtype=bool),
                metadata=np.asarray(
                    json.dumps(metadata, sort_keys=True, default=str),
                    dtype=np.str_,
                ),
            )
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.prune_files(keep=path)
        return path

    def prune_files(self, *, keep: Path | None = None) -> None:
        """Keep complete matrix files inside the configured file budget."""
        if self.max_size_bytes is None or not self.root.exists():
            return
        files = [
            path
            for path in self.root.rglob("*.npz")
            if path.is_file() and path != keep
        ]
        kept_size = keep.stat().st_size if keep is not None and keep.exists() else 0
        total = kept_size + sum(path.stat().st_size for path in files)
        for path in sorted(files, key=lambda item: item.stat().st_mtime):
            if total <= self.max_size_bytes:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError as exc:
                log.warning("could not prune routing matrix %s: %s", path, exc)

    def _db(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.root / "pair_durations.sqlite")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS durations ("
            "version TEXT, profile TEXT, base_url TEXT, "
            "src_lon REAL, src_lat REAL, dst_lon REAL, dst_lat REAL, minutes REAL, "
            "updated_at REAL NOT NULL DEFAULT 0, "
            "PRIMARY KEY (version, profile, base_url, src_lon, src_lat, dst_lon, dst_lat))"
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(durations)").fetchall()
        }
        if "updated_at" not in columns:
            connection.execute(
                "ALTER TABLE durations ADD COLUMN updated_at REAL NOT NULL DEFAULT 0"
            )
            connection.execute("UPDATE durations SET updated_at=?", (time.time(),))
        if self.ttl_seconds is not None:
            connection.execute(
                "DELETE FROM durations WHERE updated_at > 0 AND updated_at < ?",
                (time.time() - self.ttl_seconds,),
            )
        connection.commit()
        self._connection = connection
        return connection

    @staticmethod
    def _pair_row(request: PairRequest) -> tuple[Any, ...]:
        key = request.key
        return (
            request.row,
            request.column,
            key.storage_version,
            key.identity.profile,
            key.identity.endpoint.rstrip("/"),
            *key.source,
            *key.destination,
        )

    def lookup_pairs(
        self,
        requests: Sequence[PairRequest],
        *,
        cancel_check: Callable[[], None] | None = None,
        batch_size: int = 10_000,
    ) -> list[tuple[int, int, float]]:
        """Batch passive pair requests into one SQLite join."""
        if not requests:
            return []
        connection = self._db()
        connection.execute("DROP TABLE IF EXISTS requested_pairs")
        connection.execute(
            "CREATE TEMP TABLE requested_pairs ("
            "row_idx INTEGER NOT NULL, col_idx INTEGER NOT NULL, "
            "version TEXT NOT NULL, profile TEXT NOT NULL, base_url TEXT NOT NULL, "
            "src_lon REAL NOT NULL, src_lat REAL NOT NULL, "
            "dst_lon REAL NOT NULL, dst_lat REAL NOT NULL, "
            "PRIMARY KEY (row_idx, col_idx)) WITHOUT ROWID"
        )
        try:
            for start in range(0, len(requests), batch_size):
                if cancel_check:
                    cancel_check()
                connection.executemany(
                    "INSERT INTO requested_pairs VALUES (?,?,?,?,?,?,?,?,?)",
                    [self._pair_row(item) for item in requests[start:start + batch_size]],
                )
            age_clause = (
                "AND d.updated_at>=? " if self.ttl_seconds is not None else ""
            )
            parameters: tuple[Any, ...] = ()
            if self.ttl_seconds is not None:
                parameters = (time.time() - self.ttl_seconds,)
            cached = connection.execute(
                "SELECT q.row_idx, q.col_idx, d.minutes "
                "FROM requested_pairs q JOIN durations d "
                "ON d.version=q.version AND d.profile=q.profile "
                "AND d.base_url=q.base_url "
                "AND d.src_lon=q.src_lon AND d.src_lat=q.src_lat "
                "AND d.dst_lon=q.dst_lon AND d.dst_lat=q.dst_lat "
                f"{age_clause}",
                parameters,
            ).fetchall()
            return [(int(i), int(j), float(minutes)) for i, j, minutes in cached]
        finally:
            connection.execute("DROP TABLE IF EXISTS requested_pairs")

    def store_pairs(self, values: Sequence[tuple[PairKey, float]]) -> None:
        """Commit caller-keyed pair values as one recovery checkpoint."""
        if not values:
            return
        connection = self._db()
        updated_at = time.time()
        connection.executemany(
            "INSERT OR REPLACE INTO durations "
            "(version,profile,base_url,src_lon,src_lat,dst_lon,dst_lat,minutes,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    key.storage_version,
                    key.identity.profile,
                    key.identity.endpoint.rstrip("/"),
                    *key.source,
                    *key.destination,
                    float(minutes),
                    updated_at,
                )
                for key, minutes in values
            ],
        )
        connection.commit()

    def close(self) -> None:
        """Close the lazily opened pair database."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

