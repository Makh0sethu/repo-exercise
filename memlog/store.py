"""SQLite-backed store for conversation turns and activities.

One table, one FTS5 index. Entries are timestamped text with a source
(which app or channel it came from), a role (user, assistant, activity ...),
and a conversation id so turns can be analysed per conversation.

Security model
--------------
* The database file and its directory are created owner-only (0600 / 0700).
* ``PRAGMA secure_delete`` is on, so forgotten entries are overwritten inside
  the file rather than left in free pages.
* With a passphrase, the database is never written to disk in the clear. It is
  loaded into an in-memory SQLite connection and re-encrypted to the file on
  every commit (see ``crypto.py``).
* Retention: ``set_retention(days)`` stores a policy inside the database and
  every open, and every ``purge()``, deletes entries older than that.
* Nothing in this module touches the network.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from . import crypto
from .text import content_terms

DEFAULT_DB = Path(os.environ.get("MEMLOG_DB", Path.home() / ".memlog" / "memlog.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id     INTEGER PRIMARY KEY,
    ts     REAL    NOT NULL,
    source TEXT    NOT NULL DEFAULT 'cli',
    role   TEXT    NOT NULL DEFAULT 'user',
    conv   TEXT    NOT NULL DEFAULT '',
    text   TEXT    NOT NULL,
    meta   TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS entries_ts ON entries(ts);
CREATE INDEX IF NOT EXISTS entries_conv ON entries(conv);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    text, content='entries', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class LockedError(Exception):
    """The file is encrypted and no passphrase was given."""


@dataclass
class Entry:
    id: int
    ts: datetime
    source: str
    role: str
    conv: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def day(self) -> str:
        return self.ts.strftime("%Y-%m-%d")


def _to_ts(when: Optional[datetime | float | str]) -> float:
    if when is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(when, (int, float)):
        return float(when)
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        ts=datetime.fromtimestamp(row["ts"], tz=timezone.utc),
        source=row["source"],
        role=row["role"],
        conv=row["conv"],
        text=row["text"],
        meta=json.loads(row["meta"] or "{}"),
    )


def _fts_query(terms: Iterable[str]) -> str:
    """Build an OR query of quoted terms so any hit ranks, more hits rank higher."""
    quoted = ['"' + t.replace('"', '""') + '"' for t in terms]
    return " OR ".join(quoted)


class Store:
    def __init__(
        self,
        path: str | os.PathLike[str] = DEFAULT_DB,
        passphrase: Optional[str] = None,
        *,
        apply_retention: bool = True,
    ):
        self.path = str(path)
        self.passphrase = passphrase or None
        self._deferred = 0
        self._dirty = False
        in_memory = self.path == ":memory:"

        if not in_memory:
            self._prepare_location()

        on_disk_encrypted = not in_memory and crypto.is_encrypted_file(self.path)
        if on_disk_encrypted and not self.passphrase:
            raise LockedError(f"{self.path} is encrypted; a passphrase is required")

        if in_memory or not self.passphrase:
            if not in_memory:
                self._touch_private(self.path)
            self.conn = sqlite3.connect(self.path)
            self.encrypted = False
        else:
            # Encrypted mode: plaintext lives only in this process's memory.
            self.conn = sqlite3.connect(":memory:")
            self.encrypted = True
            if on_disk_encrypted:
                blob = Path(self.path).read_bytes()
                self.conn.deserialize(crypto.decrypt_bytes(blob, self.passphrase))
            elif Path(self.path).exists() and Path(self.path).stat().st_size > 0:
                # A plaintext database given a passphrase: take it over and encrypt it.
                plain = sqlite3.connect(self.path)
                self.conn.deserialize(plain.serialize())
                plain.close()
                for suffix in ("", "-journal", "-wal", "-shm"):
                    crypto.shred(self.path + suffix)
                self._dirty = True

        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA secure_delete = ON")
        self.conn.executescript(_SCHEMA)
        if self.encrypted:
            self._persist()  # also converts a plaintext file we took over
        elif not in_memory:
            self._chmod_private()
        if apply_retention:
            self.purge()

    # -- security / lifecycle -------------------------------------------

    def _prepare_location(self) -> None:
        parent = Path(self.path).parent
        parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)

    @staticmethod
    def _touch_private(path: str) -> None:
        """Create the file owner-only before SQLite does it with the umask default."""
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    def _chmod_private(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            p = Path(self.path + suffix)
            if p.exists():
                with contextlib.suppress(OSError):
                    os.chmod(p, 0o600)

    def _commit(self) -> None:
        self.conn.commit()
        if self.encrypted:
            self._dirty = True
            if not self._deferred:
                self._persist()

    def _persist(self) -> None:
        if not self.encrypted:
            return
        assert self.passphrase
        crypto.write_private(self.path, crypto.encrypt_bytes(self.conn.serialize(), self.passphrase))
        self._dirty = False

    @contextlib.contextmanager
    def deferred(self) -> Iterator["Store"]:
        """Batch many writes into one encrypted save (cheap for plaintext stores too)."""
        self._deferred += 1
        try:
            yield self
        finally:
            self._deferred -= 1
            if not self._deferred:
                self.flush()

    def flush(self) -> None:
        self.conn.commit()
        if self.encrypted and self._dirty:
            self._persist()

    def close(self) -> None:
        self.flush()
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def change_passphrase(self, new_passphrase: Optional[str]) -> None:
        """Re-encrypt with a new passphrase, or decrypt to plaintext when ``None``."""
        if self.path == ":memory:":
            raise ValueError("an in-memory store has no file to protect")
        self.conn.commit()
        if new_passphrase:
            if not self.encrypted:
                data = self.conn.serialize()
                self.conn.close()
                self.conn = sqlite3.connect(":memory:")
                self.conn.row_factory = sqlite3.Row
                self.conn.deserialize(data)
                self.conn.execute("PRAGMA secure_delete = ON")
                for suffix in ("", "-journal", "-wal", "-shm"):
                    crypto.shred(self.path + suffix)
                self.encrypted = True
            self.passphrase = new_passphrase
            self._persist()
            return
        # Decrypting to plaintext: write a fresh file, private permissions.
        if not self.encrypted:
            return
        self.conn.commit()
        crypto.shred(self.path)
        self._touch_private(self.path)
        plain = sqlite3.connect(self.path)
        self.conn.backup(plain)  # deserialize() would detach from the file; backup() writes it
        plain.close()
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA secure_delete = ON")
        self.encrypted = False
        self.passphrase = None
        self._chmod_private()

    def wipe(self) -> None:
        """Destroy every memory: clear the tables, then shred the file."""
        self.conn.execute("DELETE FROM entries")
        self.conn.execute("DELETE FROM settings")
        self.conn.commit()
        self.conn.close()
        if self.path != ":memory:":
            for suffix in ("", "-journal", "-wal", "-shm"):
                crypto.shred(self.path + suffix)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    # -- retention ------------------------------------------------------

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (key, value))
        self._commit()

    @property
    def retention_days(self) -> Optional[int]:
        raw = self.get_setting("retention_days")
        return int(raw) if raw else None

    def set_retention(self, days: Optional[int]) -> int:
        """Keep entries for ``days`` days (``None`` = forever). Returns how many were purged now."""
        if days is not None and days <= 0:
            raise ValueError("retention must be a positive number of days, or None")
        self.set_setting("retention_days", str(days) if days else None)
        return self.purge()

    def purge(self, now: Optional[datetime] = None) -> int:
        """Apply the retention policy. Returns the number of entries deleted."""
        days = self.retention_days
        if not days:
            return 0
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
        return self.forget(before=cutoff)

    def forget(
        self,
        *,
        before: Optional[datetime] = None,
        conv: Optional[str] = None,
        source: Optional[str] = None,
        ids: Optional[Iterable[int]] = None,
    ) -> int:
        """Delete entries matching every given filter. Returns the number deleted."""
        clauses, args = [], []
        if before is not None:
            clauses.append("ts < ?")
            args.append(_to_ts(before))
        if conv is not None:
            clauses.append("conv = ?")
            args.append(conv)
        if source is not None:
            clauses.append("source = ?")
            args.append(source)
        if ids is not None:
            ids = list(ids)
            if not ids:
                return 0
            clauses.append(f"id IN ({','.join('?' * len(ids))})")
            args.extend(ids)
        if not clauses:
            raise ValueError("refusing to forget everything without a filter; use wipe()")
        cur = self.conn.execute(f"DELETE FROM entries WHERE {' AND '.join(clauses)}", args)
        deleted = cur.rowcount
        self.conn.commit()  # always: an open write transaction would block backup()/serialize()
        if deleted:
            # The FTS index keeps deleted terms until rebuilt; then VACUUM with
            # secure_delete overwrites the freed pages so nothing lingers on disk.
            self.conn.execute("INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')")
            self.conn.commit()
            self.conn.execute("VACUUM")
            self._commit()
        return deleted

    # -- writing ---------------------------------------------------------

    def add(
        self,
        text: str,
        *,
        when: Optional[datetime | float | str] = None,
        source: str = "cli",
        role: str = "user",
        conv: str = "",
        meta: Optional[dict[str, Any]] = None,
    ) -> int:
        text = text.strip()
        if not text:
            raise ValueError("refusing to store an empty entry")
        ts = _to_ts(when)
        if not conv:
            # Default conversation: one per source per day.
            conv = f"{source}:{datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')}"
        cur = self.conn.execute(
            "INSERT INTO entries(ts, source, role, conv, text, meta) VALUES (?,?,?,?,?,?)",
            (ts, source, role, conv, text, json.dumps(meta or {})),
        )
        self._commit()
        return int(cur.lastrowid)

    def add_many(self, records: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self.deferred():
            for rec in records:
                rec = dict(rec)
                text = rec.pop("text", "") or rec.pop("content", "")
                if not str(text).strip():
                    continue
                self.add(
                    str(text),
                    when=rec.pop("when", None) or rec.pop("ts", None) or rec.pop("timestamp", None),
                    source=rec.pop("source", "import"),
                    role=rec.pop("role", "user"),
                    conv=rec.pop("conv", "") or rec.pop("conversation_id", ""),
                    meta=rec.pop("meta", None) or rec or None,
                )
                n += 1
        return n

    # -- reading ---------------------------------------------------------

    def get(self, entry_id: int) -> Optional[Entry]:
        row = self.conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return _row_to_entry(row) if row else None

    def between(
        self, start: Optional[datetime], end: Optional[datetime], limit: int = 1000
    ) -> list[Entry]:
        where, args = self._time_clause(start, end)
        rows = self.conn.execute(
            f"SELECT * FROM entries WHERE {where} ORDER BY ts DESC, id DESC LIMIT ?", (*args, limit)
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def search(
        self,
        query: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[tuple[Entry, float]]:
        """Full-text search. Returns (entry, relevance) with relevance in (0, 1], best first.

        An empty or stopword-only query falls back to a time-ordered listing
        with relevance 1.0 for every entry.
        """
        terms = content_terms(query)
        if not terms:
            return [(e, 1.0) for e in self.between(start, end, limit)]
        where, args = self._time_clause(start, end, alias="e")
        rows = self.conn.execute(
            f"""
            SELECT e.*, bm25(entries_fts) AS rank
            FROM entries_fts f JOIN entries e ON e.id = f.rowid
            WHERE entries_fts MATCH ? AND {where}
            ORDER BY rank LIMIT ?
            """,
            (_fts_query(terms), *args, limit),
        ).fetchall()
        if not rows:
            return []
        # bm25() is "lower is better" and negative; flip and normalise.
        scores = [-float(r["rank"]) for r in rows]
        top = max(scores) or 1.0
        return [(_row_to_entry(r), s / top) for r, s in zip(rows, scores)]

    def conversation(self, conv: str) -> list[Entry]:
        rows = self.conn.execute(
            "SELECT * FROM entries WHERE conv=? ORDER BY ts", (conv,)
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])

    def stats(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) n, MIN(ts) first, MAX(ts) last, COUNT(DISTINCT conv) convs FROM entries"
        ).fetchone()
        sources = self.conn.execute(
            "SELECT source, COUNT(*) n FROM entries GROUP BY source ORDER BY n DESC"
        ).fetchall()
        fmt = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).isoformat() if t else None
        return {
            "path": self.path,
            "encrypted": self.encrypted,
            "retention_days": self.retention_days,
            "entries": row["n"],
            "conversations": row["convs"],
            "first": fmt(row["first"]),
            "last": fmt(row["last"]),
            "sources": {r["source"]: r["n"] for r in sources},
        }

    def __iter__(self) -> Iterator[Entry]:
        for row in self.conn.execute("SELECT * FROM entries ORDER BY ts"):
            yield _row_to_entry(row)

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _time_clause(
        start: Optional[datetime], end: Optional[datetime], alias: str = ""
    ) -> tuple[str, list[float]]:
        col = f"{alias}.ts" if alias else "ts"
        clauses, args = ["1=1"], []
        if start is not None:
            clauses.append(f"{col} >= ?")
            args.append(_to_ts(start))
        if end is not None:
            clauses.append(f"{col} < ?")
            args.append(_to_ts(end))
        return " AND ".join(clauses), args
