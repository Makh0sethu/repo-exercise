"""Recent scan results, kept for this browser session only.

They live in the visitor's own Django session (never shared between visitors) and
disappear when the browser closes, when the session expires, or on sign-out. Only the
last few are kept. They hold the same package list the scan log already records;
uploaded file contents are never part of them.
"""
import secrets
from datetime import datetime, timezone

from .scan import hydrate

KEY = "scan_history"
MAX_KEPT = 5
DERIVED = ("vulnerable", "clean", "unchecked")      # rebuilt on load, so not stored twice


def save(request, result: dict) -> str:
    rid = secrets.token_urlsafe(9)
    compact = {k: v for k, v in result.items() if k not in DERIVED}
    entry = {"id": rid, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "result": compact}
    items = [entry] + [e for e in request.session.get(KEY, []) if e["id"] != rid]
    request.session[KEY] = items[:MAX_KEPT]
    return rid


def load(request, rid: str):
    for e in request.session.get(KEY, []):
        if e["id"] == rid:
            return hydrate(e["result"])
    return None


def recent(request):
    out = []
    for e in request.session.get(KEY, []):
        r = e["result"]
        out.append({"id": e["id"], "at": datetime.fromisoformat(e["at"]), "inputs": r.get("inputs", []),
                    "stats": r["stats"], "sources": r.get("sources", [])})
    return out
