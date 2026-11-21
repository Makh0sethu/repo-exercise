"""Validate what we will hand to `git clone`. The URL and ref come from a user,
so they are checked before they reach a subprocess."""
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,99}$")


class UnsafeTarget(ValueError):
    pass


def is_local_target(value: str) -> bool:
    """A filesystem path (absolute) rather than a URL."""
    return bool(value) and "://" not in value and os.path.isabs(value)


def check_local_path(value: str) -> str:
    """A local target must be an existing folder inside one of SENTINEL_LOCAL_ROOTS. It is only ever read."""
    roots = [Path(r).resolve() for r in getattr(settings, "PIPELINE_LOCAL_ROOTS", [])]
    if not roots:
        raise UnsafeTarget("Local folders are turned off. Set SENTINEL_LOCAL_ROOTS to the folder(s) that may be scanned")
    p = Path(value).resolve()
    if not any(p == r or r in p.parents for r in roots):
        raise UnsafeTarget(f"{value!r} is outside the allowed folders: {', '.join(map(str, roots))}")
    if not p.is_dir():
        raise UnsafeTarget(f"{value!r} is not a folder that exists")
    return str(p)


def check_git_url(url: str) -> str:
    if is_local_target(url):
        return check_local_path(url)
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
