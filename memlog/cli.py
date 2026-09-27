"""Command-line front end.

    memlog log "Refactored the auth module, still fighting the token refresh bug"
    memlog ingest chat_export.jsonl
    memlog ask "what have I been doing over the past month?"
    memlog ask "what did I say about rust about 3 years ago?"
    memlog recall "borrow checker" --since "last week"
    memlog watch ~/notes/today.md
    memlog stats

    memlog lock                     # encrypt the memory file with a passphrase
    memlog retention 90             # keep 90 days, purge older on every open
    memlog forget --before 2024-01-01
    memlog wipe                     # destroy everything

Passphrase lookup order: --passphrase-file, MEMLOG_PASSPHRASE, then an
interactive prompt when the file turns out to be encrypted.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import __version__, crypto
from .recorder import ingest, watch
from .retrieve import recall
from .store import DEFAULT_DB, LockedError, Store
from .summarize import NetworkDisabled, llm_summary, network_allowed, render_report
from .timeframe import parse_timeframe


# -- passphrase handling ---------------------------------------------------

def _passphrase(args: argparse.Namespace, *, prompt: bool = True, confirm: bool = False) -> Optional[str]:
    if getattr(args, "passphrase_file", None):
        return Path(args.passphrase_file).read_text(encoding="utf-8").strip() or None
    env = os.environ.get("MEMLOG_PASSPHRASE")
    if env:
        return env
    if not prompt or not sys.stdin.isatty():
        return None
    first = getpass.getpass("memlog passphrase: ")
    if confirm and first and getpass.getpass("again: ") != first:
        print("passphrases do not match", file=sys.stderr)
        raise SystemExit(2)
    return first or None


def _store(args: argparse.Namespace) -> Store:
    path = args.db
    needs = path != ":memory:" and crypto.is_encrypted_file(path)
    pw = _passphrase(args, prompt=needs)
    try:
        return Store(path, passphrase=pw if needs else pw)
    except LockedError:
        print(f"{path} is encrypted. Give the passphrase via MEMLOG_PASSPHRASE or --passphrase-file.", file=sys.stderr)
        raise SystemExit(2)
    except crypto.DecryptionError as exc:
        print(f"cannot open {path}: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _confirm(args: argparse.Namespace, question: str) -> bool:
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        print("refusing without --yes when not interactive", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


# -- commands ----------------------------------------------------------------

def cmd_log(args: argparse.Namespace) -> int:
    text = " ".join(args.text) if args.text else sys.stdin.read()
    if not text.strip():
        print("nothing to log", file=sys.stderr)
        return 1
    with _store(args) as store:
        entry_id = store.add(text, source=args.source, role=args.role, conv=args.conv or "", when=args.when)
    print(f"stored #{entry_id}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    with _store(args) as store:
        n = ingest(store, args.path, source=args.source)
    print(f"ingested {n} entries from {args.path}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    with _store(args) as store:
        result = recall(store, " ".join(args.question), limit=args.limit)
    if args.json:
        print(json.dumps(_result_to_json(result), indent=2, default=str))
        return 0
    print(render_report(result))
    if args.llm:
        print()
        try:
            model = os.environ.get("MEMLOG_LLM_MODEL", "")
            print(f"Sending the report above to {model or '(no model configured)'} ...", file=sys.stderr)
            prose = llm_summary(result)
        except NetworkDisabled as exc:
            print(f"LLM summary: {exc}")
            return 0
        print("LLM summary:" if prose else "LLM summary: unavailable (set MEMLOG_LLM_MODEL and install litellm).")
        if prose:
            print(prose)
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    question = " ".join(args.query)
    if args.since:
        question = f"{question} {args.since}"
    with _store(args) as store:
        result = recall(store, question, limit=args.limit)
    print(f"{result.timeframe.describe()} · focus: {', '.join(result.terms) or 'everything'}")
    for h in result.hits[: args.limit]:
        e = h.entry
        text = e.text.replace("\n", " ")
        if len(text) > 110:
            text = text[:107] + "..."
        print(f"[{h.score:.2f}] #{e.id} {e.ts:%Y-%m-%d %H:%M} {e.source}/{e.role}  {text}")
    if not result.hits:
        print("(no matches)")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    with _store(args) as store:
        print(f"watching {args.path} → {store.path}  (Ctrl-C to stop)", file=sys.stderr)
        n = watch(store, args.path, source=args.source, from_start=args.from_start)
    print(f"stored {n} entries", file=sys.stderr)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with _store(args) as store:
        stats = store.stats()
    stats["network"] = "blocked by MEMLOG_NO_NETWORK" if not network_allowed() else (
        f"LLM summary allowed with --llm (model: {os.environ['MEMLOG_LLM_MODEL']})"
        if os.environ.get("MEMLOG_LLM_MODEL") else "no model configured; nothing leaves this machine"
    )
    print(json.dumps(stats, indent=2))
    return 0


def cmd_timeframe(args: argparse.Namespace) -> int:
    frame = parse_timeframe(" ".join(args.phrase))
    print(f"{frame.describe()}\nremaining query: {frame.remainder!r}")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    """Encrypt the memory file (or change its passphrase)."""
    current = _passphrase(args, prompt=crypto.is_encrypted_file(args.db))
    try:
        store = Store(args.db, passphrase=current if crypto.is_encrypted_file(args.db) else None)
    except (LockedError, crypto.DecryptionError) as exc:
        print(f"cannot open {args.db}: {exc}", file=sys.stderr)
        return 2
    if args.new_passphrase_file:
        new = Path(args.new_passphrase_file).read_text(encoding="utf-8").strip()
    elif sys.stdin.isatty():
        new = getpass.getpass("new passphrase: ")
        if new != getpass.getpass("again: "):
            print("passphrases do not match", file=sys.stderr)
            return 2
    else:
        new = current or ""
    if not new:
        print("empty passphrase; nothing changed", file=sys.stderr)
        return 2
    with store:
        store.change_passphrase(new)
    print(f"{args.db} is encrypted (owner-only permissions, plaintext only in memory while open)")
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    """Decrypt the memory file back to plaintext SQLite."""
    if not crypto.is_encrypted_file(args.db):
        print(f"{args.db} is not encrypted", file=sys.stderr)
        return 1
    if not _confirm(args, f"Write {args.db} to disk unencrypted?"):
        return 1
    with _store(args) as store:
        store.change_passphrase(None)
    print(f"{args.db} is now plaintext (still owner-only permissions)")
    return 0


def cmd_retention(args: argparse.Namespace) -> int:
    with _store(args) as store:
        if args.days is None:
            days = store.retention_days
            print(f"retention: {days} days" if days else "retention: keep forever")
            return 0
        if args.days.lower() in {"off", "forever", "none", "0"}:
            store.set_retention(None)
            print("retention: keep forever")
            return 0
        n = store.set_retention(int(args.days))
    print(f"retention: {int(args.days)} days ({n} older entries purged now; purge runs on every open)")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    before = None
    if args.before:
        before = parse_timeframe(args.before).start if not args.before[:4].isdigit() else datetime.fromisoformat(args.before)
        if before is None:
            print(f"could not read a date from {args.before!r}", file=sys.stderr)
            return 2
        if before.tzinfo is None:
            before = before.replace(tzinfo=timezone.utc)
    if args.older_than:
        before = datetime.now(timezone.utc) - timedelta(days=int(args.older_than))
    if before is None and not args.conv and not args.source and not args.id:
        print("give at least one of --before, --older-than, --conv, --source, --id", file=sys.stderr)
        return 2
    with _store(args) as store:
        n = store.forget(before=before, conv=args.conv, source=args.source, ids=args.id or None)
    print(f"forgot {n} entries")
    return 0


def cmd_wipe(args: argparse.Namespace) -> int:
    if not _confirm(args, f"Destroy every memory in {args.db}? This cannot be undone."):
        return 1
    if crypto.is_encrypted_file(args.db):
        # No passphrase needed to destroy: shred the file directly.
        for suffix in ("", "-journal", "-wal", "-shm"):
            crypto.shred(args.db + suffix)
    else:
        store = Store(args.db, apply_retention=False)
        store.wipe()
    print(f"wiped {args.db}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Seed an in-memory store with a fake history and answer a few questions against it."""
    store = Store(":memory:")
    now = datetime.now(timezone.utc)
    sample = [
        (2, "chat", "user", "How do I get the borrow checker to accept a struct that holds a reference to its own field?"),
        (2, "chat", "assistant", "You generally can't; self-referential structs need Pin or an index instead of a reference."),
        (2, "notes", "activity", "Spent the evening reading the Rust book chapter on lifetimes."),
        (5, "chat", "user", "Plan a 5 day hiking trip in the Drakensberg with a budget of about 4000 rand."),
        (5, "chat", "assistant", "Start at Royal Natal, walk the Amphitheatre, then Cathedral Peak; budget mostly on huts and food."),
        (9, "notes", "activity", "Applied for the data engineering role, tailored the CV around the airflow pipelines project."),
        (12, "chat", "user", "Why does my airflow DAG run twice when I set catchup to true?"),
        (12, "chat", "assistant", "Catchup schedules a run for every missed interval since start_date; set catchup=False or move start_date."),
        (20, "notes", "activity", "Gym three times this week, finally back to a 100kg deadlift."),
        (26, "chat", "user", "Summarise the differences between Postgres logical and physical replication."),
        (40, "chat", "user", "Help me write a cover letter for the analytics job at the bank."),
        (400, "chat", "user", "What's the fastest way to learn Rust coming from Python?"),
        (1100, "chat", "user", "I'm thinking of switching from mechanical engineering to software. Where do I start?"),
    ]
    for days_ago, source, role, text in sample:
        store.add(text, when=now - timedelta(days=days_ago, hours=3), source=source, role=role)

    questions = args.question and [" ".join(args.question)] or [
        "what have I been doing over the past month?",
        "what did I ask about rust in the past weeks?",
        "what was I thinking about 3 years ago?",
        "cover letter and job stuff over the past 2 months",
    ]
    for q in questions:
        print("=" * 80)
        print(f"Q: {q}")
        print("-" * 80)
        print(render_report(recall(store, q, now=now)))
        print()
    return 0


def _result_to_json(result) -> dict:
    return {
        "question": result.question,
        "timeframe": {
            "label": result.timeframe.label,
            "start": result.timeframe.start,
            "end": result.timeframe.end,
        },
        "terms": result.terms,
        "activity": result.activity,
        "conversations": [
            {
                "conv": c.conv,
                "source": c.source,
                "first": c.first,
                "last": c.last,
                "turns_in_frame": c.turns_in_frame,
                "matched": len(c.hits),
                "relevance": c.relevance,
                "keywords": c.keywords,
                "snippet": c.snippet,
            }
            for c in result.conversations
        ],
        "hits": [
            {"id": h.entry.id, "ts": h.entry.ts, "source": h.entry.source, "role": h.entry.role,
             "conv": h.entry.conv, "score": round(h.score, 3), "text": h.entry.text}
            for h in result.hits
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memlog", description="Remember and query your conversations. Local only.")
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (default: %(default)s, or $MEMLOG_DB)")
    p.add_argument("--passphrase-file", help="file holding the passphrase (else $MEMLOG_PASSPHRASE, else prompt)")
    p.add_argument("--version", action="version", version=f"memlog {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("log", help="store one entry (from args or stdin)")
    s.add_argument("text", nargs="*")
    s.add_argument("--source", default="cli")
    s.add_argument("--role", default="user", help="user, assistant, activity ...")
    s.add_argument("--conv", help="conversation id (default: one per source per day)")
    s.add_argument("--when", help="ISO timestamp (default: now)")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("ingest", help="import a .jsonl/.json/.txt/.md file")
    s.add_argument("path")
    s.add_argument("--source")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("ask", help="ask a question, get a report")
    s.add_argument("question", nargs="+")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--json", action="store_true")
    s.add_argument("--llm", action="store_true",
                   help="ALSO send the report to $MEMLOG_LLM_MODEL for a prose summary (the only online feature)")
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("recall", help="raw ranked matches")
    s.add_argument("query", nargs="*")
    s.add_argument("--since", help='time phrase, e.g. "last week"')
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_recall)

    s = sub.add_parser("watch", help="tail a file and store every new line")
    s.add_argument("path")
    s.add_argument("--source")
    s.add_argument("--from-start", action="store_true")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("stats", help="what's in the store, and whether anything can go online")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("timeframe", help="show how a time phrase is interpreted")
    s.add_argument("phrase", nargs="+")
    s.set_defaults(func=cmd_timeframe)

    s = sub.add_parser("lock", help="encrypt the memory file with a passphrase (or change it)")
    s.add_argument("--new-passphrase-file", help="file holding the new passphrase (else prompt)")
    s.set_defaults(func=cmd_lock)

    s = sub.add_parser("unlock", help="decrypt the memory file back to plain SQLite")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_unlock)

    s = sub.add_parser("retention", help="show or set how many days to keep memories")
    s.add_argument("days", nargs="?", help='a number of days, or "off" to keep forever')
    s.set_defaults(func=cmd_retention)

    s = sub.add_parser("forget", help="delete specific memories")
    s.add_argument("--before", help='ISO date or time phrase, e.g. 2024-01-01 or "3 months ago"')
    s.add_argument("--older-than", metavar="DAYS")
    s.add_argument("--conv")
    s.add_argument("--source")
    s.add_argument("--id", type=int, action="append")
    s.set_defaults(func=cmd_forget)

    s = sub.add_parser("wipe", help="destroy every memory and shred the file")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_wipe)

    s = sub.add_parser("demo", help="run against a built-in sample history (in memory, nothing written)")
    s.add_argument("question", nargs="*")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
