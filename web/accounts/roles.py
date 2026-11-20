from django.contrib.auth.models import Group, User

from .identity import GITHUB_PREFIX, ROLE_GROUPS


def configured_roles() -> dict:
    """{lowercase github login: role} from SENTINEL_GITHUB_USERS ('alice:admin,bob:operator').
    Empty by default: with nobody listed, nobody can sign in (fails closed)."""
    from django.conf import settings
    roles = {}
    for item in settings.SENTINEL_GITHUB_USERS.split(","):
        if ":" in item:
            login, role = item.rsplit(":", 1)
            roles[login.strip().lower()] = role.strip().lower()
    return {k: v for k, v in roles.items() if v in ("operator", "auditor", "admin")}


def sync_user(user: User, role: str) -> None:
    """Make the account match its role. Writes only when something differs."""
    want_super = role == "admin"
    want_group = ROLE_GROUPS.get(role)
    managed = set(ROLE_GROUPS.values())
    have = set(user.groups.filter(name__in=managed).values_list("name", flat=True))
    wanted = {want_group} if want_group else set()
    if (user.is_staff, user.is_superuser) != (True, want_super):
        user.is_staff, user.is_superuser = True, want_super
        user.save(update_fields=["is_staff", "is_superuser"])
    if have != wanted:
        user.groups.remove(*Group.objects.filter(name__in=have - wanted))
        user.groups.add(*Group.objects.filter(name__in=wanted - have))


def role_for(user: User):
    if not user.username.startswith(GITHUB_PREFIX):
        return None
    return configured_roles().get(user.first_name.lower())
