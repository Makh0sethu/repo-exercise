"""TODO(re-enable-login): temporary local-dev bypass for the Security runs pages.

Open access applies only when settings.SENTINEL_OPEN_ACCESS is on (which itself needs DEBUG)
AND the request comes from a loopback address. Anything else uses normal login + permissions.
"""
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required

LOCAL_ADDRS = {"127.0.0.1", "::1"}
LOCAL_ACTOR = "local-dev"


def open_access(request):
    return bool(settings.SENTINEL_OPEN_ACCESS and settings.DEBUG and request.META.get("REMOTE_ADDR") in LOCAL_ADDRS)


def has_perm(request, perm):
    return open_access(request) or request.user.has_perm(perm)


def actor(request, default):
    return LOCAL_ACTOR if open_access(request) and not request.user.is_authenticated else default


def login_unless_open(view):
    protected = login_required(login_url="/auth/github/login/")(view)

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        return view(request, *args, **kwargs) if open_access(request) else protected(request, *args, **kwargs)
    return wrapper


class OpenAccessLoginMiddleware:
    """TODO(re-enable-login): in open-access mode, sign anonymous loopback requests in as the local
    superuser so the Django admin and the Admin tab work without a login. Remove with the rest of the bypass."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and not user.is_authenticated and open_access(request):
            from django.contrib.auth import get_user_model, login

            from audit.log import record
            su = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
            if su is not None:
                login(request, su, backend="django.contrib.auth.backends.ModelBackend")
                record("auth.open_access_login", actor=LOCAL_ACTOR, payload={"as": su.get_username(), "path": request.path})
        return self.get_response(request)
