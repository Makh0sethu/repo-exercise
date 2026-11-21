"""Latest security headlines for the home page.

The home page must never wait on the network. It renders from the saved feed copies (the same
ones scans use) and, when some are missing or out of date, refreshes them in the background so
the next visit is current. Only the very first visit, with nothing saved at all, waits briefly.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any

from django.conf import settings

from .http_client import STALE_LIMIT_NEWS, cache_peek
from .news import FEEDS, fetch_all_rss_stories, story_kind

FIRST_VISIT_WAIT = 4.0       # seconds, only when no feed has ever been saved
_refresh_lock = threading.Lock()
_refreshing = threading.Event()


def _refresh_in_background() -> threading.Thread | None:
    """Start one refresh at a time; return the thread so a first visit can wait on it."""
    if not getattr(settings, "SENTINEL_HOME_LIVE_REFRESH", True):
        return None
    with _refresh_lock:
        if _refreshing.is_set():
            return None
        _refreshing.set()

    def run() -> None:
        try:
            fetch_all_rss_stories(days=30)
        except Exception:
            pass                     # each feed already records its own failure; nothing to add
        finally:
            _refreshing.clear()

    thread = threading.Thread(target=run, name="sentinel-headlines", daemon=True)
    thread.start()
    return thread


def _saved_stories() -> tuple[list[dict[str, Any]], int, float]:
    """(stories, feeds that need a refresh, age in seconds of the oldest copy used)."""
    stories: list[dict[str, Any]] = []
    needs_refresh = 0
    oldest = 0.0
    for name in FEEDS:
        items, fresh, age = cache_peek("rss_feed", name, stale_limit=STALE_LIMIT_NEWS)
        if items is None or not fresh:
            needs_refresh += 1
        if items:
            oldest = max(oldest, age)
            stories += [{**i, "source": name} for i in items]
    return stories, needs_refresh, oldest


def _when(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ago(dt: datetime, now: datetime) -> str:
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 3600:
        return f"{max(1, secs // 60)} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    days = secs // 86400
    return "yesterday" if days == 1 else f"{days} days ago"


def _excerpt(text: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:")
    return cut + "…"


def _slug(source: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")


def _initials(source: str) -> str:
    # "BleepingComputer" -> BC, "The Hacker News" -> HN, "CISA Advisories" -> CA
    words = [w for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+", source)
             if w.lower() not in ("on", "the", "of")]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def latest(limit_articles: int = 9, limit_reports: int = 6, limit_ticker: int = 16) -> dict[str, Any]:
    """Everything the home page shows: a lead story, more articles, official reports, a ticker."""
    stories, needs_refresh, oldest = _saved_stories()
    if needs_refresh:
        thread = _refresh_in_background()
        if not stories and thread is not None:
            thread.join(FIRST_VISIT_WAIT)          # first visit ever: worth a short wait
            stories, needs_refresh, oldest = _saved_stories()

    now = datetime.now(timezone.utc)
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    cleaned = []
    for s in stories:
        dt = _when(s.get("date", ""))
        title = (s.get("title") or "").strip()
        url = (s.get("url") or "").strip()
        key = re.sub(r"\W+", " ", title.lower()).strip()
        if not (dt and title and url.startswith(("https://", "http://"))) or url in seen_urls or key in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(key)
        cleaned.append({
            "title": title,
            "url": url,
            "source": s["source"],
            "source_slug": _slug(s["source"]),
            "initials": _initials(s["source"]),
            "kind": story_kind(s["source"]),
            "excerpt": _excerpt(s.get("text", "")),
            "date": dt.strftime("%d %b %Y"),
            "iso": dt.isoformat(),
            "ago": _ago(dt, now),
            "_dt": dt,
        })
    cleaned.sort(key=lambda s: s["_dt"], reverse=True)
    for s in cleaned:
        s.pop("_dt")

    articles = [s for s in cleaned if s["kind"] == "article"]
    reports = [s for s in cleaned if s["kind"] == "report"]
    return {
        "lead": articles[0] if articles else None,
        "articles": articles[1:limit_articles + 1],
        "reports": reports[:limit_reports],
        "ticker": cleaned[:limit_ticker],
        "total": len(cleaned),
        "feeds": len(FEEDS),
        "updating": bool(needs_refresh) and getattr(settings, "SENTINEL_HOME_LIVE_REFRESH", True),
        "hours_old": round(oldest / 3600) if oldest else 0,
    }
