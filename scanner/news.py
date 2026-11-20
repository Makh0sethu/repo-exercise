"""Security news feed fetching and matching (6 RSS feeds + Hacker News)."""
from __future__ import annotations

import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .http_client import TTL_NEWS, cached_fetch, fetch_bytes, fetch_json

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=8&query={}"
FEEDS = {
    "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "Krebs on Security": "https://krebsonsecurity.com/feed/",
    "CISA Advisories": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
    "GitHub Security Blog": "https://github.blog/security/feed/",
    "SecurityWeek": "https://feeds.feedburner.com/securityweek",
}

# Skip very common English/programming words that produce false-positive news matches.
GENERIC_NAMES = {
    "six", "attrs", "click", "rich", "wheel", "pip", "idna", "toml", "ply",
    "tqdm", "packaging", "typing", "debug", "color", "chalk", "bytes", "path",
    "glob", "mime", "qs", "ms", "fs", "os", "net", "http", "url", "util",
    "events", "stream", "buffer", "crypto", "assert", "string", "object",
}


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _name_pattern(name: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.I)


def _fetch_single_feed(name: str, url: str) -> tuple[str, list[dict[str, Any]], bool]:
    try:
        raw = fetch_bytes(url, headers={"Accept": "application/rss+xml, application/xml, text/xml"}, timeout=4)
        root = ET.fromstring(raw)
    except Exception:
        return name, [], False

    items: list[dict[str, Any]] = []
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        child = lambda t: next((c for c in item if c.tag.split("}")[-1] == t), None)
        title = child("title")
        link = child("link")
        href = (
            link.get("href")
            if link is not None and link.get("href")
            else (link.text if link is not None else "")
        )
        desc = child("description") or child("summary") or child("content")
        date_el = child("pubDate") or child("published") or child("updated")
        dt = _parse_date(date_el.text if date_el is not None else None)
        items.append({
            "source": name,
            "title": html.unescape((title.text or "") if title is not None else "").strip(),
            "url": (href or "").strip(),
            "text": re.sub(r"<[^>]+>", " ", html.unescape(desc.text or "")) if desc is not None else "",
            "date": dt.isoformat() if dt else "",
        })
    return name, items, True


def fetch_all_rss_stories(days: int = 30) -> tuple[list[dict[str, Any]], bool]:
    """Fetch and cache recent RSS items across all 6 security feeds (1h TTL)."""
    def _load() -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stories: list[dict[str, Any]] = []
        any_ok = False
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = pool.map(lambda kv: _fetch_single_feed(*kv), FEEDS.items())
            for _, items, ok in results:
                any_ok = any_ok or ok
                for item in items:
                    dt = _parse_date(item["date"])
                    if dt and dt >= cutoff and item["url"]:
                        stories.append(item)
        return {"stories": stories, "ok": any_ok}

    payload = cached_fetch("rss_feeds", f"days:{days}", TTL_NEWS, _load)
    return payload["stories"], payload["ok"]


def _fetch_hn_for_package(name: str, cutoff: datetime) -> tuple[list[dict[str, Any]], bool]:
    def _load() -> list[dict[str, Any]]:
        query = urllib.parse.quote(f"{name} vulnerability")
        data = fetch_json(HN_SEARCH.format(query), timeout=3)
        return data.get("hits", [])

    try:
        hits = cached_fetch("hn_pkg", name.lower(), TTL_NEWS, _load)
    except Exception:
        return [], False

    pat = _name_pattern(name)
    out: list[dict[str, Any]] = []
    for hit in hits:
        dt = _parse_date(hit.get("created_at"))
        title = hit.get("title") or ""
        url = hit.get("url") or ""
        if dt and dt >= cutoff and url and pat.search(title):
            out.append({
                "source": "Hacker News",
                "title": title,
                "url": url,
                "date": dt.strftime("%Y-%m-%d"),
                "matches": [name],
            })
    return out, True


def match_news(
    packages: list[Any],
    cve_ids: set[str],
    days: int = 30,
    include_hn: bool = True,
    preloaded_rss: tuple[list[dict[str, Any]], bool] | None = None,
    priority_names: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Match recent security news stories against package names and CVE IDs."""
    if preloaded_rss is not None:
        stories, rss_ok = preloaded_rss
    else:
        stories, rss_ok = fetch_all_rss_stories(days=days)
    names = sorted({p.name for p in packages})
    pkg_pats = {
        n: _name_pattern(n)
        for n in names
        if len(n) > 2 and n.lower() not in GENERIC_NAMES
    }
    cve_pats = {c: re.compile(re.escape(c), re.I) for c in sorted(cve_ids)}

    matched: dict[str, dict[str, Any]] = {}
    for story in stories:
        blob = f"{story['title']} {story.get('text', '')}"
        hits = [n for n, pat in pkg_pats.items() if pat.search(blob)]
        hits += [c for c, pat in cve_pats.items() if pat.search(blob)]
        if hits:
            matched[story["url"]] = {
                "source": story["source"],
                "title": story["title"],
                "url": story["url"],
                "date": story["date"][:10],
                "matches": sorted(set(hits)),
            }

    hn_ok = False
    if include_hn and pkg_pats:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        ordered = [n for n in (priority_names or []) if n in pkg_pats]
        for n in pkg_pats:
            if n not in ordered:
                ordered.append(n)
        hn_targets = ordered[:5]
        with ThreadPoolExecutor(max_workers=5) as pool:
            for items, ok in pool.map(lambda n: _fetch_hn_for_package(n, cutoff), hn_targets):
                hn_ok = hn_ok or ok
                for item in items:
                    if item["url"] in matched:
                        combined = set(matched[item["url"]]["matches"]) | set(item["matches"])
                        matched[item["url"]]["matches"] = sorted(combined)
                    else:
                        matched[item["url"]] = item

    status = "ok" if (rss_ok or hn_ok) else "unreachable"
    ranked = sorted(matched.values(), key=lambda s: s["date"], reverse=True)
    return ranked, status
