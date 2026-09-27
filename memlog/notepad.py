"""The notepad: Markdown notes kept inside the vault.

* ``notes/<name>.md`` is a named note you edit in place.
* ``notes/journal/<YYYY>/<YYYY-MM-DD>.md`` is the day's page; ``add()``
  without a name appends there.

Every ``add()`` appends a section headed by its timestamp::

    ## 2026-09-27 14:03
    Buy milk. Ask about the airflow catchup bug.

so "what did I note last week?" is answerable. Notes are indexed section by
section (heading timestamp, else the file's modification time) with role
``note`` and conversation ``note:<name>``, sealed with the rest of the vault
when it is locked, and never expired by retention.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess  # only ever used to launch $EDITOR on a private temp file
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

from . import crypto
from .store import _to_ts

if TYPE_CHECKING:
    from .vault import Vault

NOTES_DIR = "notes"
JOURNAL_DIR = "journal"
_HEADING = re.compile(r"^##\s+(.*?)\s*$", re.M)
_STAMP_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._/-]+")


def _safe_note_name(name: str) -> str:
    raw = name.strip()
    if raw.startswith("/") or any(part == ".." for part in raw.split("/")):
        raise ValueError(f"note names stay inside notes/: {name!r}")
    safe = _SAFE_NAME.sub("-", raw).strip("./-")
    if not safe:
        raise ValueError(f"not a valid note name: {name!r}")
    return safe


@dataclass
class Section:
    when: Optional[datetime]  # from the heading, if it was a timestamp
    heading: str
    text: str


@dataclass
class NoteInfo:
    name: str
    rel: str
    sections: int
    modified: datetime


def parse_sections(text: str) -> list[Section]:
    """Split a note on '## ' headings. Text before the first heading is its own section."""
    out: list[Section] = []
    pos = 0
    heading = ""
    for m in _HEADING.finditer(text):
        body = text[pos : m.start()].strip()
        if body:
            out.append(Section(_parse_stamp(heading), heading, body))
        heading, pos = m.group(1), m.end()
    body = text[pos:].strip()
    if body or heading:
        out.append(Section(_parse_stamp(heading), heading, body))
    return out


def _parse_stamp(heading: str) -> Optional[datetime]:
    for fmt in _STAMP_FORMATS:
        try:
            return datetime.strptime(heading.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class Notepad:
    def __init__(self, vault: "Vault"):
        self.vault = vault

    # -- naming ------------------------------------------------------------

    def path(self, name: str) -> Path:
        return self.vault.root / NOTES_DIR / (_safe_note_name(name) + ".md")

    @staticmethod
    def journal_name(when: Optional[datetime] = None) -> str:
        when = when or datetime.now(timezone.utc)
        return f"{JOURNAL_DIR}/{when:%Y}/{when:%Y-%m-%d}"

    def _name_of(self, path: Path) -> str:
        return path.relative_to(self.vault.root / NOTES_DIR).with_suffix("").as_posix()

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.vault.root).as_posix()

    def _files(self) -> Iterator[Path]:
        base = self.vault.root / NOTES_DIR
        if base.exists():
            yield from sorted(p for p in base.rglob("*.md") if p.is_file())

    # -- reading ------------------------------------------------------------

    def list(self) -> list[NoteInfo]:
        out = []
        for p in self._files():
            text = self.vault.read_file(p).decode("utf-8", errors="replace")
            out.append(NoteInfo(self._name_of(p), self._rel(p), len(parse_sections(text)),
                                datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)))
        return out

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def read(self, name: str) -> str:
        path = self.path(name)
        if not path.exists():
            raise FileNotFoundError(f"no note named {name!r}")
        return self.vault.read_file(path).decode("utf-8", errors="replace")

    # -- writing ------------------------------------------------------------

    def write(self, name: str, text: str) -> Path:
        """Replace a note's whole content (creating it if needed) and reindex it."""
        path = self.path(name)
        text = text.rstrip() + "\n"
        if not text.strip():
            self.delete(name)
            return path
        self.vault.write_file(path, text.encode("utf-8"))
        self.index_note(name)
        return path

    def add(self, text: str, name: Optional[str] = None, when: Optional[datetime] = None) -> str:
        """Append a timestamped section to ``name`` (default: today's journal page). Returns the note name."""
        text = text.strip()
        if not text:
            raise ValueError("refusing to add an empty note")
        when = when or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        name = name or self.journal_name(when)
        current = self.read(name) if self.exists(name) else ""
        section = f"## {when:%Y-%m-%d %H:%M}\n{text}\n"
        self.write(name, (current.rstrip() + "\n\n" + section) if current.strip() else section)
        return name

    def delete(self, name: str) -> bool:
        path = self.path(name)
        existed = path.exists()
        self.vault.remove_file(path)
        self.vault.index.forget(loc=self._rel(path))
        return existed

    def rename(self, old: str, new: str) -> None:
        src, dst = self.path(old), self.path(new)
        if not src.exists():
            raise FileNotFoundError(f"no note named {old!r}")
        if dst.exists():
            raise FileExistsError(f"a note named {new!r} already exists")
        text = self.read(old)
        self.delete(old)
        self.write(new, text)

    def edit(self, name: str, editor: Optional[str] = None) -> bool:
        """Open the note in $EDITOR via a private temp file; re-seal on save. Returns True if it changed."""
        editor = editor or os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            raise RuntimeError("set $EDITOR (or $VISUAL) to edit notes")
        before = self.read(name) if self.exists(name) else ""
        tmpdir = Path(tempfile.mkdtemp(prefix="memlog-note-"))
        os.chmod(tmpdir, 0o700)
        tmp = tmpdir / (Path(name).name + ".md")
        try:
            crypto.write_private(tmp, before.encode("utf-8"))
            subprocess.run([*editor.split(), str(tmp)], check=True)
            after = tmp.read_text(encoding="utf-8")
        finally:
            for p in tmpdir.rglob("*"):
                if p.is_file():
                    crypto.shred(p)
            shutil.rmtree(tmpdir, ignore_errors=True)
        if after.strip() == before.strip():
            return False
        self.write(name, after)
        return True

    def remove_sections(self, path: Path, texts: set[str]) -> None:
        """Drop the sections whose body is in ``texts`` (used by Vault.forget)."""
        current = self.vault.read_file(path).decode("utf-8", errors="replace")
        kept = [s for s in parse_sections(current) if s.text not in texts]
        name = self._name_of(path)
        if not kept:
            self.delete(name)
            return
        self.write(name, "\n\n".join(self._render(s) for s in kept))

    @staticmethod
    def _render(section: Section) -> str:
        return f"## {section.heading}\n{section.text}" if section.heading else section.text

    # -- indexing -----------------------------------------------------------

    def index_note(self, name: str) -> int:
        """Reindex one note: one index entry per section."""
        path = self.path(name)
        rel = self._rel(path)
        self.vault.index.forget(loc=rel)
        if not path.exists():
            return 0
        text = self.vault.read_file(path).decode("utf-8", errors="replace")
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        n = 0
        with self.vault.index.deferred():
            for i, section in enumerate(parse_sections(text)):
                if not section.text:
                    continue
                uid = hashlib.sha1(f"{rel}\0{i}\0{section.text}".encode("utf-8")).hexdigest()[:16]
                self.vault.index.add(section.text, when=section.when or mtime, source="notepad",
                                     role="note", conv=f"note:{name}", uid=uid, loc=rel,
                                     meta={"heading": section.heading} if section.heading else None)
                n += 1
        return n

    def index_all(self) -> int:
        return sum(self.index_note(self._name_of(p)) for p in self._files())
