"""Security headers and per-IP rate limiting middleware for Sentinel."""
from __future__ import annotations

import time
from typing import Callable

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render

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


def _client_ip(request: HttpRequest) -> str:
    """Extract client IP, respecting X-Forwarded-For only when behind a trusted proxy."""
    if getattr(settings, "SENTINEL_TRUST_PROXY", False):
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


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
            if limit and not self._allow(_client_ip(request), request.path, limit):
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
