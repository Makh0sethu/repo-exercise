"""SQLite-backed store for conversation turns and activities.

One table, one FTS5 index. Entries are timestamped text with a source
(which app or channel it came from), a role (user, assistant, activity ...),
and a conversation id so turns can be analysed per conversation.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

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
"""


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
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

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
        self.conn.commit()
        return int(cur.lastrowid)

    def add_many(self, records: Iterable[dict[str, Any]]) -> int:
        n = 0
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
            "entries": row["n"],
            "conversations": row["convs"],
            "first": fmt(row["first"]),
            "last": fmt(row["last"]),
            "sources": {r["source"]: r["n"] for r in sources},
        }

    def __iter__(self) -> Iterator[Entry]:
        for row in self.conn.execute("SELECT * FROM entries ORDER BY ts"):
            yield _row_to_entry(row)

    def close(self) -> None:
        self.conn.close()

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
