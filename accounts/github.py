"""GitHub OAuth web flow, standard library only. Three small functions so the
whole exchange is readable in one screen and easy to mock in tests."""
import json
import urllib.parse
import urllib.request

from django.conf import settings

AUTHORIZE = "https://github.com/login/oauth/authorize"
TOKEN = "https://github.com/login/oauth/access_token"
API_USER = "https://api.github.com/user"
HEADERS = {"Accept": "application/json", "User-Agent": "sentinel-server"}


def callback_url() -> str:
    # Fixed from settings, never from the request, so it can't be steered by a Host header.
    return settings.SENTINEL_BASE_URL.rstrip("/") + "/auth/github/callback/"


def authorize_url(state: str) -> str:
    q = urllib.parse.urlencode({"client_id": settings.SENTINEL_GITHUB_CLIENT_ID, "redirect_uri": callback_url(),
                                "state": state, "scope": "read:user", "allow_signup": "false"})
    return f"{AUTHORIZE}?{q}"


def _open(req, timeout=15):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def exchange_code(code: str) -> str:
    body = urllib.parse.urlencode({"client_id": settings.SENTINEL_GITHUB_CLIENT_ID,
                                   "client_secret": settings.SENTINEL_GITHUB_CLIENT_SECRET,
                                   "code": code, "redirect_uri": callback_url()}).encode()
    data = _open(urllib.request.Request(TOKEN, data=body, headers=HEADERS))
    if "access_token" not in data:
        raise ValueError(data.get("error_description") or data.get("error") or "no access token")
    return data["access_token"]


def fetch_user(token: str) -> dict:
    """Returns {"id": int, "login": str}. The token is used once and then discarded."""
    data = _open(urllib.request.Request(API_USER, headers={**HEADERS, "Authorization": f"Bearer {token}"}))
    return {"id": int(data["id"]), "login": data["login"]}
