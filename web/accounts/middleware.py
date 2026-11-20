"""Removing someone from SENTINEL_GITHUB_USERS (or changing their role) takes
effect on their very next request, not at their next login."""
from django.contrib.auth import logout

from audit.log import record

from .identity import GITHUB_PREFIX, actor_name
from .roles import configured_roles, sync_user


class GitHubAllowlistMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.username.startswith(GITHUB_PREFIX):
            role = configured_roles().get(user.first_name.lower())
            if role is None:
                record("auth.revoked", actor=actor_name(user), payload={"reason": "no longer in SENTINEL_GITHUB_USERS"})
                logout(request)
            else:
                sync_user(user, role)
        return self.get_response(request)
