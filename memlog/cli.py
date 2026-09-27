"""Command-line front end.

    memlog log "Refactored the auth module, still fighting the token refresh bug"
    memlog ingest chat_export.jsonl
    memlog ask "what have I been doing over the past month?"
    memlog ask "what did I say about rust about 3 years ago?"
    memlog recall "borrow checker" --since "last week"
    memlog watch ~/notes/today.md
    memlog stats
    memlog demo
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import __version__
from .recorder import ingest, watch
from .retrieve import recall
from .store import DEFAULT_DB, Store
from .summarize import llm_summary, render_report
from .timeframe import parse_timeframe


def _store(args: argparse.Namespace) -> Store:
    return Store(args.db)


def cmd_log(args: argparse.Namespace) -> int:
    text = " ".join(args.text) if args.text else sys.stdin.read()
    if not text.strip():
        print("nothing to log", file=sys.stderr)
        return 1
    store = _store(args)
    entry_id = store.add(text, source=args.source, role=args.role, conv=args.conv or "", when=args.when)
    print(f"stored #{entry_id}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    store = _store(args)
    n = ingest(store, args.path, source=args.source)
    print(f"ingested {n} entries from {args.path}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    store = _store(args)
    result = recall(store, " ".join(args.question), limit=args.limit)
    if args.json:
        print(json.dumps(_result_to_json(result), indent=2, default=str))
        return 0
    print(render_report(result))
    if args.llm:
        prose = llm_summary(result)
        print()
        print("LLM summary:" if prose else "LLM summary: unavailable (set MEMLOG_LLM_MODEL and install litellm).")
        if prose:
            print(prose)
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    store = _store(args)
    question = " ".join(args.query)
    if args.since:
        question = f"{question} {args.since}"
    result = recall(store, question, limit=args.limit)
    print(f"{result.timeframe.describe()} · focus: {', '.join(result.terms) or 'everything'}")
    for h in result.hits[: args.limit]:
        e = h.entry
        text = e.text.replace("\n", " ")
        if len(text) > 110:
            text = text[:107] + "..."
        print(f"[{h.score:.2f}] {e.ts:%Y-%m-%d %H:%M} {e.source}/{e.role}  {text}")
    if not result.hits:
        print("(no matches)")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    store = _store(args)
    print(f"watching {args.path} → {store.path}  (Ctrl-C to stop)", file=sys.stderr)
    n = watch(store, args.path, source=args.source, from_start=args.from_start)
    print(f"stored {n} entries", file=sys.stderr)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    print(json.dumps(_store(args).stats(), indent=2))
    return 0


def cmd_timeframe(args: argparse.Namespace) -> int:
    frame = parse_timeframe(" ".join(args.phrase))
    print(f"{frame.describe()}\nremaining query: {frame.remainder!r}")
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
    p = argparse.ArgumentParser(prog="memlog", description="Remember and query your conversations.")
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (default: %(default)s, or $MEMLOG_DB)")
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
    s.add_argument("--llm", action="store_true", help="also produce an LLM prose summary if configured")
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

    s = sub.add_parser("stats", help="what's in the store")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("timeframe", help="show how a time phrase is interpreted")
    s.add_argument("phrase", nargs="+")
    s.set_defaults(func=cmd_timeframe)

    s = sub.add_parser("demo", help="run against a built-in sample history")
    s.add_argument("question", nargs="*")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
