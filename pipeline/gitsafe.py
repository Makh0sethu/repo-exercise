"""Validate what we will hand to `git clone`. The URL and ref come from a user,
so they are checked before they reach a subprocess."""
import re
from urllib.parse import urlparse

from django.conf import settings

REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,99}$")


class UnsafeTarget(ValueError):
    pass


def check_git_url(url: str) -> str:
    u = urlparse(url)
    allowed = getattr(settings, "PIPELINE_ALLOWED_GIT_HOSTS", ["github.com"])
    if u.scheme != "https":
        raise UnsafeTarget("Only https:// git URLs are allowed")
    if u.username or u.password:
        raise UnsafeTarget("Do not put credentials in the URL")
    if (u.hostname or "").lower() not in allowed:
        raise UnsafeTarget(f"Host {u.hostname!r} is not in the allowed list {allowed}")
    return url


def check_ref(ref: str) -> str:
    if ref != "HEAD" and (not REF_RE.match(ref) or ".." in ref):
        raise UnsafeTarget(f"Invalid ref {ref!r}")
    return ref
