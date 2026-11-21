from django.conf import settings
from django.urls import reverse

from accounts.identity import actor_name
from accounts.roles import role_for


def _active(path, url, exact=False):
    return path == url if exact else path.startswith(url)


def sentinel(request):
    user = getattr(request, "user", None)
    path = request.path
    signed_in = bool(user and user.is_authenticated)
    items = [
        {"label": "Home", "url": reverse("scanner:home"), "active": path == "/"},
        {"label": "Scan", "url": reverse("scanner:scan"), "active": path.startswith("/scan/")},
        {"label": "Results", "url": reverse("scanner:results"), "active": _active(path, "/results/"),
         "count": len(request.session.get("scan_history", [])) if hasattr(request, "session") else 0},
    ]
    if signed_in and user.has_perm("pipeline.view_run"):
        items.append({"label": "Security runs", "url": reverse("pipeline:runs"), "active": _active(path, "/server/")})
    if signed_in and (user.is_superuser or user.has_perm("audit.view_auditevent") or user.has_perm("scanner.view_scanlog")):
        items.append({"label": "Admin", "url": reverse("admin:index"), "active": _active(path, "/" + settings.SENTINEL_ADMIN_PATH)})
    role = None
    if signed_in:
        role = "admin" if user.is_superuser else (role_for(user) or "staff")
    return {
        "retention_days": settings.SENTINEL_LOG_RETENTION_DAYS,
        "nav_items": items,
        "nav_user": {"name": actor_name(user), "role": role} if signed_in else None,
        "login_next": path if path.startswith("/") else "/server/",
    }
