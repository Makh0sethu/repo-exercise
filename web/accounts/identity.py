"""Who is acting? GitHub users are stored under their immutable numeric id
(`github-<id>`) so a renamed or re-registered GitHub name can never inherit
someone else's account; the display login is shown in logs as `github:<login>`."""
GITHUB_PREFIX = "github-"
ROLES = ("operator", "auditor", "admin")
ROLE_GROUPS = {"operator": "Sentinel operators", "auditor": "Sentinel auditors"}


def actor_name(user) -> str:
    if user is None or not getattr(user, "is_authenticated", False):
        return "anonymous"
    if user.username.startswith(GITHUB_PREFIX):
        return f"github:{user.first_name or user.username}"
    return user.get_username()
