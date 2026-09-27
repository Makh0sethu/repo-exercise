"""Command-line front end.

    memlog log "Refactored the auth module, still fighting the token refresh bug"
    memlog ingest chat_export.jsonl
    memlog ask "what have I been doing over the past month?"
    memlog ask "what did I say about rust about 3 years ago?" --save
    memlog recall "borrow checker" --since "last week"
    memlog watch ~/notes/today.md
    memlog tree                     # the memory folders
    memlog stats

    memlog lock                     # seal every memory file with a passphrase
    memlog retention 90             # keep 90 days, purge older on every open
    memlog forget --before 2024-01-01
    memlog reindex                  # rebuild the search index from the folders
    memlog wipe                     # destroy everything

Memories live in structured folders under ~/.memlog (or $MEMLOG_ROOT, or
--root). Passphrase lookup order: --passphrase-file, MEMLOG_PASSPHRASE, then
an interactive prompt when the vault turns out to be locked.
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
from .store import LockedError, Store
from .summarize import NetworkDisabled, llm_summary, network_allowed, render_report
from .timeframe import parse_timeframe
from .vault import CONFIG_NAME, DEFAULT_ROOT, Vault, VaultConfig


# -- passphrase handling ---------------------------------------------------

def _is_locked(root: str) -> bool:
    return VaultConfig.load(Path(root) / CONFIG_NAME).encrypted


def _passphrase(args: argparse.Namespace, *, prompt: bool = True) -> Optional[str]:
    if getattr(args, "passphrase_file", None):
        return Path(args.passphrase_file).read_text(encoding="utf-8").strip() or None
    env = os.environ.get("MEMLOG_PASSPHRASE")
    if env:
        return env
    if not prompt or not sys.stdin.isatty():
        return None
    return getpass.getpass("memlog passphrase: ") or None


def _new_passphrase(args: argparse.Namespace) -> Optional[str]:
    if getattr(args, "new_passphrase_file", None):
        return Path(args.new_passphrase_file).read_text(encoding="utf-8").strip() or None
    if not sys.stdin.isatty():
        return None
    first = getpass.getpass("new passphrase: ")
    if first != getpass.getpass("again: "):
        print("passphrases do not match", file=sys.stderr)
        raise SystemExit(2)
    return first or None


def _vault(args: argparse.Namespace) -> Vault:
    locked = _is_locked(args.root)
    pw = _passphrase(args, prompt=locked) if locked else None
    try:
        return Vault(args.root, passphrase=pw)
    except LockedError:
        print(f"{args.root} is locked. Give the passphrase via MEMLOG_PASSPHRASE or --passphrase-file.",
              file=sys.stderr)
        raise SystemExit(2)
    except crypto.DecryptionError as exc:
        print(f"cannot open {args.root}: {exc}", file=sys.stderr)
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
    with _vault(args) as vault:
        entry_id = vault.add(text, source=args.source, role=args.role, conv=args.conv or "", when=args.when)
        loc = vault.index.get(entry_id).loc
    print(f"stored #{entry_id} → {loc}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    with _vault(args) as vault:
        n = ingest(vault, args.path, source=args.source, passphrase=_passphrase(args, prompt=False))
    print(f"ingested {n} entries from {args.path}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    question = " ".join(args.question)
    with _vault(args) as vault:
        result = recall(vault.store, question, limit=args.limit)
        if args.json:
            print(json.dumps(_result_to_json(result), indent=2, default=str))
            return 0
        report = render_report(result)
        print(report)
        if args.llm:
            print()
            try:
                model = os.environ.get("MEMLOG_LLM_MODEL", "")
                print(f"Sending the report above to {model or '(no model configured)'} ...", file=sys.stderr)
                prose = llm_summary(result)
            except NetworkDisabled as exc:
                print(f"LLM summary: {exc}")
                prose = None
            else:
                print("LLM summary:" if prose else "LLM summary: unavailable (set MEMLOG_LLM_MODEL and install litellm).")
                if prose:
                    print(prose)
                    report += "\n\nLLM summary:\n" + prose
        if args.save:
            rel = vault.save_report(question, report)
            print(f"\nsaved → {rel}", file=sys.stderr)
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    question = " ".join(args.query)
    if args.since:
        question = f"{question} {args.since}"
    with _vault(args) as vault:
        result = recall(vault.store, question, limit=args.limit)
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
    with _vault(args) as vault:
        print(f"watching {args.path} → {vault.root}  (Ctrl-C to stop)", file=sys.stderr)
        n = watch(vault, args.path, source=args.source, from_start=args.from_start)
    print(f"stored {n} entries", file=sys.stderr)
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    with _vault(args) as vault:
        rows = vault.tree()
        locked = vault.encrypted
    if not rows:
        print(f"{args.root}: no memories yet")
        return 0
    print(f"{args.root}  ({'sealed' if locked else 'plaintext'}, {sum(n for _, n, _ in rows)} entries)")
    last_dir = None
    for rel, n, size in rows:
        directory, name = rel.rsplit("/", 1)
        if directory != last_dir:
            print(f"  {directory}/")
            last_dir = directory
        print(f"      {name:<48} {n:>5} entr{'y' if n == 1 else 'ies'}  {size:>8} B")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with _vault(args) as vault:
        stats = vault.stats()
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


def cmd_reindex(args: argparse.Namespace) -> int:
    with _vault(args) as vault:
        n = vault.reindex()
    print(f"indexed {n} entries from the folders under {args.root}")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    """Seal the vault (or change its passphrase)."""
    with _vault(args) as vault:
        new = _new_passphrase(args)
        if not new:
            print("empty passphrase; nothing changed", file=sys.stderr)
            return 2
        vault.lock(new)
    print(f"{args.root} is sealed: every memory file is encrypted, plaintext only in memory while open")
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    if not _is_locked(args.root):
        print(f"{args.root} is not sealed", file=sys.stderr)
        return 1
    if not _confirm(args, f"Write every memory under {args.root} to disk unencrypted?"):
        return 1
    with _vault(args) as vault:
        vault.unlock()
    print(f"{args.root} is now plaintext (still owner-only permissions)")
    return 0


def cmd_retention(args: argparse.Namespace) -> int:
    with _vault(args) as vault:
        if args.days is None:
            days = vault.retention_days
            print(f"retention: {days} days" if days else "retention: keep forever")
            return 0
        if args.days.lower() in {"off", "forever", "none", "0"}:
            vault.set_retention(None)
            print("retention: keep forever")
            return 0
        n = vault.set_retention(int(args.days))
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
    with _vault(args) as vault:
        n = vault.forget(before=before, conv=args.conv, source=args.source, ids=args.id or None)
    print(f"forgot {n} entries")
    return 0


def cmd_wipe(args: argparse.Namespace) -> int:
    if not _confirm(args, f"Destroy every memory under {args.root}? This cannot be undone."):
        return 1
    root = Path(args.root)
    if _is_locked(args.root) and not _passphrase(args, prompt=False):
        # No passphrase needed to destroy: shred every file directly.
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                crypto.shred(path)
            elif path.is_dir():
                path.rmdir()
        root.mkdir(exist_ok=True)
    else:
        with _vault(args) as vault:
            vault.wipe()
    print(f"wiped {args.root}")
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
            {"id": h.entry.id, "uid": h.entry.uid, "file": h.entry.loc, "ts": h.entry.ts,
             "source": h.entry.source, "role": h.entry.role, "conv": h.entry.conv,
             "score": round(h.score, 3), "text": h.entry.text}
            for h in result.hits
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memlog", description="Remember and query your conversations. Local only.")
    p.add_argument("--root", default=str(DEFAULT_ROOT), help="memory folder (default: %(default)s, or $MEMLOG_ROOT)")
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

    s = sub.add_parser("ingest", help="import a .jsonl/.json/.txt/.md file, or an old memlog .db")
    s.add_argument("path")
    s.add_argument("--source")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("ask", help="ask a question, get a report")
    s.add_argument("question", nargs="+")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--json", action="store_true")
    s.add_argument("--save", action="store_true", help="keep the answer under reports/")
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

    s = sub.add_parser("tree", help="show the memory folders")
    s.set_defaults(func=cmd_tree)

    s = sub.add_parser("stats", help="what's in the vault, and whether anything can go online")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("timeframe", help="show how a time phrase is interpreted")
    s.add_argument("phrase", nargs="+")
    s.set_defaults(func=cmd_timeframe)

    s = sub.add_parser("reindex", help="rebuild the search index from the folders")
    s.set_defaults(func=cmd_reindex)

    s = sub.add_parser("lock", help="seal every memory file with a passphrase (or change it)")
    s.add_argument("--new-passphrase-file", help="file holding the new passphrase (else prompt)")
    s.set_defaults(func=cmd_lock)

    s = sub.add_parser("unlock", help="write every memory file back as plaintext")
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

    s = sub.add_parser("wipe", help="destroy every memory and shred the files")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_wipe)

    s = sub.add_parser("demo", help="run against a built-in sample history (in memory, nothing written)")
    s.add_argument("question", nargs="*")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
