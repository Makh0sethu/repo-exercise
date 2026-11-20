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
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, TypeVar

from django.core.cache import cache

T = TypeVar("T")

DEFAULT_HEADERS = {
    "User-Agent": "sentinel/0.2 (+https://github.com/sentinel)",
    "Accept": "application/json",
    "Connection": "keep-alive",
}

TTL_VULN = 60 * 60 * 24       # 24 hours for OSV, CVE.org, NVD records
TTL_CATALOG = 60 * 60 * 24    # 24 hours for CISA KEV & RetireJS definitions
TTL_NEWS = 60 * 60            # 1 hour for RSS feeds & Hacker News

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


def cached_fetch(prefix: str, identifier: str, ttl: int, fetcher: Callable[[], T]) -> T:
    """Return cached value for (prefix, identifier), or call fetcher and cache it."""
    key = _cache_key(prefix, identifier)
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = fetcher()
    if value is not None:
        cache.set(key, value, ttl)
    return value
