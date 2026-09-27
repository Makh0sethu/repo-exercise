"""A vault: memories kept as structured folders, with a rebuildable search index.

    <root>/                          default ~/.memlog  (or $MEMLOG_ROOT)
      vault.json                     layout version, retention policy, key salt + verifier (no secrets)
      conversations/<YYYY>/<MM>/<source>/<conversation>.jsonl
      activities/<YYYY>/<MM>/<source>.jsonl
      notes/<name>.md                the notepad: named notes, edited in place
      notes/journal/<YYYY>/<YYYY-MM-DD>.md      quick notes land in the day's journal page
      reports/<YYYY>/<YYYY-MM-DD>_<slug>.md     answers you chose to keep
      index/memlog.db                search index; rebuilt from the folders by ``reindex()``

The folders are the source of truth and stay readable by a human (or by
``grep``). One line of JSON per memory. The index is a cache that makes
questions fast; delete it and ``reindex()`` recreates it.

Everything that applies to the single-file store applies here, per file:
owner-only permissions, encryption with a passphrase (each file sealed
separately, keys derived once per session), retention, forget, wipe.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from . import crypto
from .store import Entry, LockedError, Store, _to_ts, default_conv

DEFAULT_ROOT = Path(os.environ.get("MEMLOG_ROOT", Path.home() / ".memlog"))
CONFIG_NAME = "vault.json"
LAYOUT_VERSION = 2
KINDS = ("conversations", "activities")
NOTES_DIR = "notes"
ACTIVITY_ROLES = frozenset({"activity", "event", "action", "note"})

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str, limit: int = 80) -> str:
    """A filesystem-safe version of a source or conversation id."""
    out = _SAFE.sub("_", name).strip("._") or "unnamed"
    return out[:limit]


def slugify(text: str, limit: int = 48) -> str:
    return safe_name(text.lower().replace(" ", "-"), limit).strip("-") or "report"


@dataclass
class VaultConfig:
    version: int = LAYOUT_VERSION
    retention_days: Optional[int] = None
    salt: Optional[str] = None      # hex; present only when encrypted
    check: Optional[str] = None     # hex verifier for the passphrase
    created: str = ""

    @property
    def encrypted(self) -> bool:
        return bool(self.salt)

    @classmethod
    def load(cls, path: Path) -> "VaultConfig":
        if not path.exists():
            return cls(created=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self, path: Path) -> None:
        crypto.write_private(path, (json.dumps(self.__dict__, indent=2) + "\n").encode("utf-8"))


class Vault:
    def __init__(
        self,
        root: str | os.PathLike[str] = DEFAULT_ROOT,
        passphrase: Optional[str] = None,
        *,
        apply_retention: bool = True,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        self.config = VaultConfig.load(self.root / CONFIG_NAME)
        self.keys: Optional[crypto.Keys] = None

        if self.config.encrypted:
            if not passphrase:
                raise LockedError(f"{self.root} is encrypted; a passphrase is required")
            keys = crypto.derive_keys(passphrase, bytes.fromhex(self.config.salt or ""))
            if keys.verifier().hex() != self.config.check:
                raise crypto.DecryptionError("wrong passphrase")
            self.keys = keys
        elif passphrase:
            self.lock(passphrase)  # a passphrase on an open vault means: lock it now

        self._mkdirs(self.index_path)
        self.index = Store(self.index_path, keys=self.keys, apply_retention=False)
        if not (self.root / CONFIG_NAME).exists():
            self.config.save(self.root / CONFIG_NAME)
        if self.index.count() == 0 and any(self._entry_files()):
            self.reindex()
        if apply_retention:
            self.purge()

    # -- paths ------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.root / "index" / "memlog.db"

    @property
    def encrypted(self) -> bool:
        return self.keys is not None

    @property
    def store(self) -> Store:
        """The search index, for ``recall()`` and friends."""
        return self.index

    def location(self, *, ts: float, source: str, role: str, conv: str) -> Path:
        when = datetime.fromtimestamp(ts, tz=timezone.utc)
        year, month = when.strftime("%Y"), when.strftime("%m")
        if role in ACTIVITY_ROLES:
            return Path("activities") / year / month / f"{safe_name(source)}.jsonl"
        return Path("conversations") / year / month / safe_name(source) / f"{safe_name(conv)}.jsonl"

    def _entry_files(self) -> Iterator[Path]:
        for kind in KINDS:
            base = self.root / kind
            if base.exists():
                yield from sorted(p for p in base.rglob("*.jsonl") if p.is_file())

    # -- sealed file io ----------------------------------------------------

    def read_file(self, path: Path) -> bytes:
        """Read a vault file, unsealing it if the vault is locked. Missing file -> b''."""
        if not path.exists():
            return b""
        raw = path.read_bytes()
        if crypto.is_encrypted(raw):
            if self.keys is None:
                raise LockedError(f"{path} is sealed; open the vault with its passphrase")
            raw = crypto.unseal(raw, self.keys)
        return raw

    def write_file(self, path: Path, data: bytes) -> None:
        """Write a vault file owner-only, sealed if the vault is locked."""
        self._mkdirs(path)
        crypto.write_private(path, crypto.seal(data, self.keys) if self.keys else data)

    def remove_file(self, path: Path) -> None:
        if path.exists():
            crypto.shred(path)
        self._prune_empty_dirs(path.parent)

    def _read_lines(self, path: Path) -> list[dict[str, Any]]:
        raw = self.read_file(path)
        out = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                with contextlib.suppress(json.JSONDecodeError):
                    out.append(json.loads(line))
        return out

    def _write_lines(self, path: Path, records: list[dict[str, Any]]) -> None:
        if not records:
            self.remove_file(path)
            return
        self.write_file(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8"))

    def _mkdirs(self, path: Path) -> None:
        """Create every folder between the root and ``path`` owner-only."""
        rel = path.parent.relative_to(self.root)
        current = self.root
        for part in rel.parts:
            current = current / part
            if not current.exists():
                current.mkdir(mode=0o700)
            with contextlib.suppress(OSError):
                os.chmod(current, 0o700)

    def _prune_empty_dirs(self, directory: Path) -> None:
        while directory != self.root and directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
            directory = directory.parent

    # -- writing -----------------------------------------------------------

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
        conv = conv or default_conv(source, ts)
        rel = self.location(ts=ts, source=source, role=role, conv=conv)
        record = {
            "uid": secrets.token_hex(8),
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds"),
            "source": source,
            "role": role,
            "conv": conv,
            "text": text,
            "meta": meta or {},
        }
        path = self.root / rel
        lines = self._read_lines(path)
        lines.append(record)
        self._write_lines(path, lines)
        return self.index.add(text, when=ts, source=source, role=role, conv=conv, meta=meta,
                              uid=record["uid"], loc=rel.as_posix())

    def add_many(self, records: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self.index.deferred():
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

    def save_report(self, question: str, text: str, when: Optional[datetime] = None) -> Path:
        """Keep an answer as a Markdown note under reports/."""
        when = when or datetime.now(timezone.utc)
        rel = Path("reports") / when.strftime("%Y") / f"{when:%Y-%m-%d}_{slugify(question)}.md"
        body = f"# {question}\n\n_{when:%Y-%m-%d %H:%M} UTC_\n\n```\n{text}\n```\n".encode("utf-8")
        self.write_file(self.root / rel, body)
        return rel

    @property
    def notes(self) -> "Notepad":
        from .notepad import Notepad
        return Notepad(self)

    # -- deleting ----------------------------------------------------------

    def forget(
        self,
        *,
        before: Optional[datetime] = None,
        conv: Optional[str] = None,
        source: Optional[str] = None,
        ids: Optional[Iterable[int]] = None,
        keep_notes: bool = False,
    ) -> int:
        """Delete matching memories from their files and from the index.

        ``keep_notes`` leaves notepad entries alone (retention uses this: notes
        are deliberate and stay until you delete them).
        """
        victims = self.index.matching(before=before, conv=conv, source=source, ids=ids)
        if keep_notes:
            victims = [e for e in victims if not e.loc.startswith(NOTES_DIR + "/")]
        if not victims:
            return 0
        by_file: dict[str, list[Entry]] = {}
        for e in victims:
            by_file.setdefault(e.loc, []).append(e)
        drop_ids = []
        for rel, entries in by_file.items():
            path = self.root / rel
            if rel.startswith(NOTES_DIR + "/"):
                # The notepad rewrites the note and reindexes it, which drops these rows itself.
                self.notes.remove_sections(path, {e.text for e in entries})
                continue
            if rel:
                uids = {e.uid for e in entries}
                keep = [r for r in self._read_lines(path) if r.get("uid") not in uids]
                self._write_lines(path, keep)
            drop_ids += [e.id for e in entries]
        if drop_ids:
            self.index.forget(ids=drop_ids)
        return len(victims)

    @property
    def retention_days(self) -> Optional[int]:
        return self.config.retention_days

    def set_retention(self, days: Optional[int]) -> int:
        if days is not None and days <= 0:
            raise ValueError("retention must be a positive number of days, or None")
        self.config.retention_days = days
        self.config.save(self.root / CONFIG_NAME)
        return self.purge()

    def purge(self, now: Optional[datetime] = None) -> int:
        if not self.config.retention_days:
            return 0
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=self.config.retention_days)
        return self.forget(before=cutoff, keep_notes=True)

    def wipe(self) -> None:
        """Shred every file in the vault and remove the folders."""
        self.index.wipe()
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_file():
                crypto.shred(path)
            elif path.is_dir():
                with contextlib.suppress(OSError):
                    path.rmdir()
        self.config = VaultConfig(created=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.keys = None
        self.index = Store(self.index_path, apply_retention=False)
        self.config.save(self.root / CONFIG_NAME)

    # -- encryption --------------------------------------------------------

    def lock(self, passphrase: str) -> None:
        """Seal every file with keys derived from ``passphrase`` (or change the passphrase)."""
        if not passphrase:
            raise ValueError("passphrase must not be empty")
        salt = crypto.new_salt()
        new_keys = crypto.derive_keys(passphrase, salt)
        self._reseal(new_keys)
        self.config.salt, self.config.check = salt.hex(), new_keys.verifier().hex()
        self.config.save(self.root / CONFIG_NAME)

    def unlock(self) -> None:
        """Write every file back as plaintext (still owner-only)."""
        if not self.encrypted:
            return
        self._reseal(None)
        self.config.salt = self.config.check = None
        self.config.save(self.root / CONFIG_NAME)

    def _reseal(self, new_keys: Optional[crypto.Keys]) -> None:
        files = list(self._entry_files())
        for folder in ("reports", NOTES_DIR):
            if (self.root / folder).exists():
                files += sorted(p for p in (self.root / folder).rglob("*.md") if p.is_file())
        for path in files:
            raw = path.read_bytes()
            if crypto.is_encrypted(raw):
                if self.keys is None:
                    raise LockedError(f"{path} is sealed with keys this vault does not have")
                raw = crypto.unseal(raw, self.keys)
            crypto.shred(path)
            crypto.write_private(path, crypto.seal(raw, new_keys) if new_keys else raw)
        self.keys = new_keys
        if hasattr(self, "index"):
            self.index.change_passphrase(None, keys=new_keys)

    # -- index maintenance -------------------------------------------------

    def reindex(self) -> int:
        """Rebuild the search index from the folders. Returns the number of entries indexed."""
        self.index.conn.execute("DELETE FROM entries")
        self.index.conn.commit()
        n = 0
        with self.index.deferred():
            for path in self._entry_files():
                rel = path.relative_to(self.root).as_posix()
                for rec in self._read_lines(path):
                    if not str(rec.get("text", "")).strip():
                        continue
                    self.index.add(
                        str(rec["text"]),
                        when=rec.get("ts"),
                        source=rec.get("source", "import"),
                        role=rec.get("role", "user"),
                        conv=rec.get("conv", ""),
                        meta=rec.get("meta") or None,
                        uid=rec.get("uid") or secrets.token_hex(8),
                        loc=rel,
                    )
                    n += 1
            n += self.notes.index_all()
        return n

    def tree(self) -> list[tuple[str, int, int]]:
        """(relative path, entries, bytes) for every memory file and note, in folder order."""
        out = []
        for path in self._entry_files():
            out.append((path.relative_to(self.root).as_posix(), len(self._read_lines(path)), path.stat().st_size))
        for note in self.notes.list():
            out.append((note.rel, note.sections, (self.root / note.rel).stat().st_size))
        return out

    def stats(self) -> dict[str, Any]:
        s = self.index.stats()
        files = self.tree()
        s.update({
            "root": str(self.root),
            "layout": f"v{self.config.version}",
            "encrypted": self.encrypted,
            "retention_days": self.retention_days,
            "files": len(files),
            "folders": sorted({f.rsplit("/", 1)[0] for f, _, _ in files}),
        })
        s.pop("path", None)
        return s

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self.index.close()

    def __enter__(self) -> "Vault":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[Entry]:
        return iter(self.index)

    def count(self) -> int:
        return self.index.count()
