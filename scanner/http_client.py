"""HTTP and caching helpers for external vulnerability & news feeds.

Uses thread-local HTTPS keep-alive connections for fast repeated requests
to the same host (e.g. api.osv.dev) and caches only public feed metadata.
User-uploaded dependency files and scan outputs are never written to disk or cache.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, TypeVar

from . import store

T = TypeVar("T")

DEFAULT_HEADERS = {
    "User-Agent": "sentinel/0.2 (+https://github.com/sentinel)",
    "Accept": "application/json",
    "Connection": "keep-alive",
}

TTL_VULN = 60 * 60 * 24       # 24 hours for OSV, CVE.org, NVD records
TTL_CATALOG = 60 * 60 * 24    # 24 hours for CISA KEV & RetireJS definitions
TTL_NEWS = 60 * 60            # 1 hour for RSS feeds & Hacker News

# How long an expired copy may still be shown when the live source is slow or down.
# Better an old answer that says how old it is than no answer.
STALE_LIMIT = 60 * 60 * 24 * 30      # advisories and catalogs: 30 days
STALE_LIMIT_NEWS = 60 * 60 * 24 * 7  # news goes out of date sooner: 7 days
FAIL_BACKOFF = 60                    # after a failure, don't retry that item for a minute
LIMITED_BACKOFF = 60 * 10            # after "too many requests", leave that item alone for 10 minutes

_TLS = threading.local()
_SSL_CTX = ssl.create_default_context()


class HttpError(Exception):
    """Raised when an upstream HTTP request fails."""

    def __init__(self, url: str, reason: str, status_code: int | None = None) -> None:
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason
        self.status_code = status_code


def _get_pool() -> dict[tuple[str, int], http.client.HTTPSConnection]:
    pool = getattr(_TLS, "conns", None)
    if pool is None:
        pool = {}
        _TLS.conns = pool
    return pool


def _pooled_https(
    url: str,
    *,
    body_bytes: bytes | None,
    headers: dict[str, str],
    timeout: int,
) -> bytes | None:
    """Attempt a pooled keep-alive HTTPS request; return None to fall back on redirects."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    port = parsed.port or 443
    host_key = (parsed.hostname, port)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    method = "POST" if body_bytes is not None else "GET"

    pool = _get_pool()
    for attempt in range(2):
        conn = pool.get(host_key)
        if conn is None:
            conn = http.client.HTTPSConnection(parsed.hostname, port, timeout=timeout, context=_SSL_CTX)
            pool[host_key] = conn
        else:
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
        try:
            conn.request(method, path, body=body_bytes, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except (http.client.HTTPException, OSError, TimeoutError) as exc:
            try:
                conn.close()
            except Exception:
                pass
            pool.pop(host_key, None)
            if attempt == 0:
                continue
            raise HttpError(url, str(exc)) from exc

        if resp.status in (301, 302, 303, 307, 308):
            return None  # Let urllib handle redirects cleanly
        if resp.status >= 400:
            raise HttpError(url, f"HTTP {resp.status}", status_code=resp.status)
        return data
    return None


def fetch_bytes(
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 8,
) -> bytes:
    """Perform an HTTP GET or POST request and return the response bytes."""
    req_headers = dict(DEFAULT_HEADERS)
    if headers:
        req_headers.update(headers)
    body_bytes = None
    if body is not None:
        body_bytes = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    pooled = _pooled_https(url, body_bytes=body_bytes, headers=req_headers, timeout=timeout)
    if pooled is not None:
        return pooled

    req = urllib.request.Request(url, headers=req_headers, data=body_bytes)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise HttpError(url, f"HTTP {exc.code}", status_code=exc.code) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise HttpError(url, str(exc)) from exc


def fetch_json(
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 8,
) -> Any:
    """Perform an HTTP request and parse the JSON response."""
    raw = fetch_bytes(url, body=body, headers=headers, timeout=timeout)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HttpError(url, "Invalid JSON response") from exc


def _cache_key(prefix: str, identifier: str) -> str:
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]
    return f"sentinel:{prefix}:{digest}"


class RecentFailure(Exception):
    """A fetch failed moments ago and nothing is saved, so we don't make everyone wait again."""


class RateLimited(Exception):
    """Our own request budget for a source is used up for now."""


# Which cached lookups were answered from an expired copy, so a scan can say so on the page.
_STALE_LOCK = threading.Lock()
_STALE_LOG: list[tuple[float, str, float]] = []


def note_stale(prefix: str, age_seconds: float) -> None:
    with _STALE_LOCK:
        _STALE_LOG.append((time.time(), prefix, age_seconds))
        del _STALE_LOG[:-500]


def stale_since(started: float) -> dict[str, float]:
    """{cache prefix: age in seconds of the oldest copy shown} for lookups since `started`."""
    out: dict[str, float] = {}
    with _STALE_LOCK:
        for at, prefix, age in _STALE_LOG:
            if at >= started:
                out[prefix] = max(out.get(prefix, 0.0), age)
    return out


def clear_cache() -> None:
    """Forget every saved copy and the in-memory counters (e.g. the per-IP rate limit)."""
    from django.core.cache import cache

    cache.clear()
    store.clear()
    with _STALE_LOCK:
        _STALE_LOG.clear()


def _backoff_for(exc: Exception) -> int:
    code = getattr(exc, "status_code", None)
    return LIMITED_BACKOFF if code in (403, 429) or isinstance(exc, RateLimited) else FAIL_BACKOFF


def cached_fetch(
    prefix: str,
    identifier: str,
    ttl: int,
    fetcher: Callable[[], T],
    *,
    stale_limit: int = STALE_LIMIT,
) -> T:
    """Return a saved copy while it is fresh, otherwise fetch and save.

    If the fetch fails, an older saved copy is returned (and noted as stale) instead of an
    error. A failure is remembered for a short while, so a source that is down costs the
    first scan its wait and the following scans nothing. Failures are never saved as data.
    """
    key = _cache_key(prefix, identifier)
    now = time.time()
    entry = store.get(key)
    if entry is not None and entry.fresh(now):
        return entry.value  # type: ignore[return-value]

    have_old_copy = entry is not None and entry.value is not None and entry.age(now) <= stale_limit
    if entry is not None and entry.retry_after > now:  # failed recently: do not hammer the source
        if have_old_copy:
            note_stale(prefix, entry.age(now))
            return entry.value  # type: ignore[return-value]
        raise RecentFailure(f"{prefix} failed recently ({entry.error or 'no detail'}); retrying shortly")

    try:
        value = fetcher()
    except Exception as exc:
        store.fail(key, repr(exc), _backoff_for(exc))
        if have_old_copy:
            note_stale(prefix, entry.age(now))  # type: ignore[union-attr]
            return entry.value  # type: ignore[union-attr,return-value]
        raise
    if value is not None:
        store.put(key, value, ttl)
    return value


def cache_peek(prefix: str, identifier: str, stale_limit: int = STALE_LIMIT):
    """(value, is_fresh, age_seconds) for a saved copy, or (None, False, 0) when there is none."""
    entry = store.get(_cache_key(prefix, identifier))
    now = time.time()
    if entry is None or entry.value is None or entry.age(now) > stale_limit:
        return None, False, 0.0
    return entry.value, entry.fresh(now), entry.age(now)


def cache_put(prefix: str, identifier: str, value: Any, ttl: int) -> None:
    store.put(_cache_key(prefix, identifier), value, ttl)


def cache_failed(prefix: str, identifier: str, exc: Exception) -> None:
    store.fail(_cache_key(prefix, identifier), repr(exc), _backoff_for(exc))
