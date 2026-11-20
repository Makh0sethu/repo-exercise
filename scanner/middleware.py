"""Request tracing, no-store caching, security headers and per-IP rate limiting for Sentinel.

Each request gets an ID (also sent back as the X-Request-ID header and stored
on its ScanLog row) so a line in logs/access.jsonl can be matched to a scan.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Callable

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render

from accounts.identity import actor_name

log = logging.getLogger("sentinel.access")

CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def _safe_int(value):
    """A client can send any text as Content-Length; logging must not crash on it."""
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def client_ip(request):
    """The visitor's real IP. X-Forwarded-For is only used behind your own proxy."""
    if settings.SENTINEL_TRUST_X_FORWARDED_FOR:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            # Your proxy appends the address it saw, so the last entry is the one to trust.
            return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR")


class NoStoreMiddleware:
    """Pages are per-visitor and change (results, run status, who is signed in), so browsers
    must not reuse a saved copy, e.g. when pressing Back after signing out. Static files
    are served by their own handler and keep normal caching."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        ctype = response.get("Content-Type", "")
        if ("text/html" in ctype or "application/json" in ctype) and "Cache-Control" not in response:
            response["Cache-Control"] = "no-store, max-age=0"
        return response


class RequestTraceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = str(uuid.uuid4())
        request.client_ip = client_ip(request)
        start = time.monotonic()
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        user = getattr(request, "user", None)
        log.info(json.dumps({
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": request.request_id,
            "ip": request.client_ip,
            "user": actor_name(user) if user is not None and user.is_authenticated else None,
            "method": request.method,
            "path": request.path,
            "query": request.META.get("QUERY_STRING", "")[:500],
            "status": response.status_code,
            "ms": round((time.monotonic() - start) * 1000),
            "bytes_in": _safe_int(request.META.get("CONTENT_LENGTH")),
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
            "referer": request.META.get("HTTP_REFERER", "")[:300],
        }))
        return response


class SecurityHeadersMiddleware:
    """Attach strict CSP and Permissions-Policy headers to every response."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", CSP_POLICY)
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response


class RateLimitMiddleware:
    """Sliding-window per-IP rate limit for /scan/ and /parse/ POST endpoints."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method == "POST":
            limit = self._limit_for_path(request.path)
            if limit and not self._allow(getattr(request, "client_ip", None) or client_ip(request), request.path, limit):
                return self._too_many_requests(request)
        return self.get_response(request)

    def _limit_for_path(self, path: str) -> int:
        if path.rstrip("/") == "/scan":
            return int(getattr(settings, "SENTINEL_RATE_LIMIT", 10))
        if path.rstrip("/") == "/parse":
            return int(getattr(settings, "SENTINEL_PREVIEW_RATE_LIMIT", 30))
        return 0

    def _allow(self, ip: str, path: str, limit: int, window: int = 60) -> bool:
        bucket = int(time.time() // window)
        key = f"sentinel:rl:{path}:{ip}:{bucket}"
        count = cache.get(key, 0) + 1
        cache.set(key, count, timeout=window + 5)
        return count <= limit

    def _too_many_requests(self, request: HttpRequest) -> HttpResponse:
        msg = "Too many requests. Please wait a minute before scanning again."
        if request.path.rstrip("/") == "/parse":
            resp: HttpResponse = JsonResponse({"error": msg}, status=429)
        else:
            resp = render(request, "scanner/scan.html", {"error": msg}, status=429)
        resp["Retry-After"] = "60"
        return resp
