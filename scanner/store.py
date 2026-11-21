"""Persistent store for PUBLIC lookup results (advisories, catalogs, feeds).

Kept in its own small SQLite file, separate from the Django database, so it:
  * survives restarts and is shared by every server worker,
  * works the same on a laptop (SQLite) and on the server (PostgreSQL for Django),
  * can never take part in, or be locked by, a request's database transaction.

Only data fetched from public sources is stored here. Uploaded files, pasted text and
scan results are never written to it. Every function swallows storage errors: a broken
or read-only cache file makes scans slower, never wrong or failed.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings

log = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()
_READY: set[str] = set()
_WARNED = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key         TEXT PRIMARY KEY,
    value       TEXT,                 -- JSON; NULL when only a recent failure is remembered
    fetched_at  REAL NOT NULL DEFAULT 0,
    expires_at  REAL NOT NULL DEFAULT 0,
    retry_after REAL NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT ''
)
"""


@dataclass
class Entry:
    value: Any            # None when no good copy is held
    fetched_at: float
    expires_at: float
    retry_after: float
    error: str

    def fresh(self, now: float) -> bool:
        return self.value is not None and now < self.expires_at

    def age(self, now: float) -> float:
        return max(0.0, now - self.fetched_at)


def _warn(exc: Exception) -> None:
    global _WARNED
    if not _WARNED:
        _WARNED = True
        log.warning("Public-data cache unavailable, continuing without it: %r", exc)


def _connect() -> sqlite3.Connection:
    path = Path(settings.SENTINEL_CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    if str(path) not in _READY:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA)
        conn.commit()
        _READY.add(str(path))
    return conn


def get(key: str) -> Entry | None:
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT value, fetched_at, expires_at, retry_after, error FROM cache WHERE key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - cache must never break a scan
        _warn(exc)
        return None
    if row is None:
        return None
    value = json.loads(row[0]) if row[0] is not None else None
    return Entry(value, row[1], row[2], row[3], row[4])


def put(key: str, value: Any, ttl: float) -> None:
    now = time.time()
    try:
        payload = json.dumps(value)
        with _WRITE_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO cache (key, value, fetched_at, expires_at, retry_after, error) "
                    "VALUES (?, ?, ?, ?, 0, '') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, fetched_at = excluded.fetched_at, "
                    "expires_at = excluded.expires_at, retry_after = 0, error = ''",
                    (key, payload, now, now + ttl),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        _warn(exc)


def fail(key: str, error: str, backoff: float) -> None:
    """Remember that a fetch just failed, keeping any older good copy. Retry only after `backoff`."""
    now = time.time()
    try:
        with _WRITE_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO cache (key, value, fetched_at, expires_at, retry_after, error) "
                    "VALUES (?, NULL, 0, 0, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET retry_after = excluded.retry_after, error = excluded.error",
                    (key, now + backoff, error[:300]),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        _warn(exc)


def clear() -> None:
    try:
        with _WRITE_LOCK:
            conn = _connect()
            try:
                conn.execute("DELETE FROM cache")
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        _warn(exc)


def purge(older_than: float) -> int:
    """Delete entries not refreshed for `older_than` seconds (they are too old to serve)."""
    cutoff = time.time() - older_than
    try:
        with _WRITE_LOCK:
            conn = _connect()
            try:
                cur = conn.execute("DELETE FROM cache WHERE fetched_at < ? AND retry_after < ?", (cutoff, time.time()))
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        return 0


def stats() -> dict[str, int]:
    try:
        conn = _connect()
        try:
            total, good = conn.execute("SELECT COUNT(*), COUNT(value) FROM cache").fetchone()
        finally:
            conn.close()
        return {"entries": total, "with_data": good}
    except Exception as exc:  # noqa: BLE001
        _warn(exc)
        return {"entries": 0, "with_data": 0}
