"""Trace every request: one JSON line per request in logs/access.jsonl.

Each request gets an ID (also sent back as the X-Request-ID header and stored
on its ScanLog row) so a line in the access log can be matched to a scan.
"""
import json
import logging
import time
import uuid

from django.conf import settings

from accounts.identity import actor_name

log = logging.getLogger("sentinel.access")


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
            "bytes_in": int(request.META.get("CONTENT_LENGTH") or 0),
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
            "referer": request.META.get("HTTP_REFERER", "")[:300],
        }))
        return response
