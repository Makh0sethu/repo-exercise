import secrets
import urllib.parse

from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from audit.log import record

from . import github
from .identity import GITHUB_PREFIX, actor_name
from .roles import configured_roles, sync_user


def _deny(request, login_name, reason, status=403):
    record("auth.denied", actor=f"github:{login_name}" if login_name else "anonymous",
           payload={"reason": reason, "ip": getattr(request, "client_ip", None)})
    return render(request, "accounts/denied.html", {"reason": reason}, status=status)


@require_GET
def github_login(request):
    state = secrets.token_urlsafe(32)
    request.session["gh_state"] = state
    nxt = request.GET.get("next", "/server/")
    request.session["gh_next"] = nxt if url_has_allowed_host_and_scheme(nxt, allowed_hosts=None) else "/server/"
    return redirect(github.authorize_url(state))


@require_GET
def github_callback(request):
    expected = request.session.pop("gh_state", "")        # single use
    given = request.GET.get("state", "")
    if not expected or not secrets.compare_digest(expected, given):
        return _deny(request, None, "Login state did not match (possible forged request)", 400)
    if request.GET.get("error") or not request.GET.get("code"):
        return _deny(request, None, "GitHub login was cancelled or failed", 400)
    try:
        info = github.fetch_user(github.exchange_code(request.GET["code"]))
    except Exception:
        return _deny(request, None, "Could not complete the GitHub exchange", 502)

    role = configured_roles().get(info["login"].lower())
    if role is None:
        return _deny(request, info["login"], "This GitHub account is not allowed to use this server")
    user, created = User.objects.get_or_create(username=f"{GITHUB_PREFIX}{info['id']}")
    if created:
        user.set_unusable_password()
    user.first_name = info["login"]                        # display only; the id is the identity
    user.save()
    sync_user(user, role)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    record("auth.login", actor=actor_name(user),
           payload={"role": role, "github_id": info["id"], "new_account": created, "ip": getattr(request, "client_ip", None)})
    return HttpResponseRedirect(request.session.pop("gh_next", "/server/"))


@require_POST
def github_logout(request):
    record("auth.logout", actor=actor_name(request.user))
    logout(request)
    return redirect("/")
