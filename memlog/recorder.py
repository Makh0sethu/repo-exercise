"""Capture conversation turns into the store, in-process or from files.

Three ways in:

* ``Recorder.record(role, text)`` from any Python chat loop.
* ``ingest(store, path)`` for a JSONL, plain-text, or Markdown export.
* ``watch(store, path)`` tails a file that another program appends to
  (a chat log, a shell history, a notes file) and stores each new line.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .store import Store

Memory = Any  # a Store or a Vault: anything with add() and add_many()


class Recorder:
    """Records turns of one conversation. Wrap a chat function to remember everything it sees."""

    def __init__(self, store: Memory, source: str = "chat", conv: Optional[str] = None):
        self.store = store
        self.source = source
        self.conv = conv or f"{source}:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"

    def record(self, role: str, text: str, **meta) -> int:
        return self.store.add(text, source=self.source, role=role, conv=self.conv, meta=meta or None)

    def activity(self, text: str, **meta) -> int:
        """Log something you did rather than something you said."""
        return self.record("activity", text, **meta)

    def wrap(self, generate: Callable[[list[dict]], str]) -> Callable[[list[dict]], str]:
        """Return a chat function that stores the latest user message and the reply."""

        def remembered(messages: list[dict]) -> str:
            if messages and messages[-1].get("role") == "user":
                self.record("user", str(messages[-1].get("content", "")))
            reply = generate(messages)
            if reply:
                self.record("assistant", reply)
            return reply

        return remembered


def _iter_file(path: Path, passphrase: Optional[str] = None) -> Iterator[dict]:
    suffix = path.suffix.lower()
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        # An older single-file memlog store (v0.1 layout).
        old = Store(path, passphrase=passphrase, apply_retention=False)
        try:
            for e in old:
                yield {"text": e.text, "when": e.ts, "source": e.source, "role": e.role,
                       "conv": e.conv, "meta": e.meta}
        finally:
            old.conn.close()
    elif suffix in {".jsonl", ".ndjson"}:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        records = data if isinstance(data, list) else data.get("messages") or data.get("entries") or []
        for rec in records:
            if isinstance(rec, dict):
                yield rec
    else:
        # Plain text / markdown: blank-line separated paragraphs become entries.
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        for para in path.read_text(encoding="utf-8", errors="replace").split("\n\n"):
            para = para.strip()
            if para:
                yield {"text": para, "when": mtime, "conv": f"file:{path.name}"}


def ingest(store: Memory, path: str | Path, source: Optional[str] = None, passphrase: Optional[str] = None) -> int:
    path = Path(path)
    records = ({**rec, "source": source or rec.get("source", path.stem)} for rec in _iter_file(path, passphrase))
    return store.add_many(records)


def watch(
    store: Memory,
    path: str | Path,
    source: Optional[str] = None,
    poll_seconds: float = 1.0,
    from_start: bool = False,
    stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Tail ``path`` and store each new non-empty line. Returns the number stored.

    Lines that parse as JSON objects are treated as records; anything else is
    stored verbatim. Runs until ``stop()`` returns True or KeyboardInterrupt.
    """
    path = Path(path)
    source = source or path.stem
    stored = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        if not from_start:
            fh.seek(0, 2)
        try:
            while not (stop and stop()):
                line = fh.readline()
                if not line:
                    time.sleep(poll_seconds)
                    continue
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        stored += store.add_many([{"source": source, **json.loads(line)}])
                        continue
                    except (json.JSONDecodeError, ValueError):
                        pass
                store.add(line, source=source, role="activity", conv=f"{source}:{datetime.now(timezone.utc):%Y-%m-%d}")
                stored += 1
        except KeyboardInterrupt:
            pass
    return stored
