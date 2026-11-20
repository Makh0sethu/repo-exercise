import urllib.parse
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from audit.log import verify_chain
from audit.models import AuditEvent
from pipeline.models import Target

USERS = "alice:admin,bob:operator,carol:auditor"


class _GH:
    def __init__(self, uid, login):
        self.p = [mock.patch("accounts.github.exchange_code", return_value="gho_TOKEN_VALUE"),
                  mock.patch("accounts.github.fetch_user", return_value={"id": uid, "login": login})]

    def __enter__(self):
        for p in self.p: p.start()

    def __exit__(self, *a):
        for p in self.p: p.stop()


@override_settings(SENTINEL_GITHUB_USERS=USERS, SENTINEL_GITHUB_CLIENT_ID="cid", SENTINEL_GITHUB_CLIENT_SECRET="sec",
                   SENTINEL_BASE_URL="http://127.0.0.1:8000")
class SignInTests(TestCase):
    def sign_in(self, uid, login, next_url=None):
        r = self.client.get("/auth/github/login/" + (f"?next={urllib.parse.quote(next_url)}" if next_url else ""))
        state = urllib.parse.parse_qs(urllib.parse.urlparse(r["Location"]).query)["state"][0]
        with _GH(uid, login):
            return self.client.get("/auth/github/callback/", {"code": "abc", "state": state})

    def test_login_sends_user_to_github_with_fixed_callback_and_state(self):
        self.assertEqual(self.client.get("/auth/github/login/", HTTP_HOST="evil.example").status_code, 400)  # rejected outright
        r = self.client.get("/auth/github/login/", HTTP_HOST="localhost")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(r["Location"]).query)
        self.assertTrue(r["Location"].startswith("https://github.com/login/oauth/authorize"))
        self.assertEqual(q["redirect_uri"], ["http://127.0.0.1:8000/auth/github/callback/"])   # not taken from the Host header
        self.assertEqual(q["scope"], ["read:user"])
        self.assertTrue(len(q["state"][0]) >= 32)

    def test_allowed_operator_signs_in_and_is_recorded_by_name(self):
        r = self.sign_in(1001, "Bob")
        self.assertRedirects(r, "/server/", fetch_redirect_response=False)
        u = User.objects.get(username="github-1001")
        self.assertEqual((u.first_name, u.is_staff, u.is_superuser), ("Bob", True, False))
        self.assertEqual(list(u.groups.values_list("name", flat=True)), ["Sentinel operators"])
        ev = AuditEvent.objects.get(action="auth.login")
        self.assertEqual((ev.actor, ev.payload["role"]), ("github:Bob", "operator"))
        self.assertTrue(verify_chain()[0])

    def test_admin_role_is_superuser(self):
        self.sign_in(1, "alice")
        self.assertTrue(User.objects.get(username="github-1").is_superuser)

    def test_unlisted_github_account_is_refused_and_recorded(self):
        r = self.sign_in(666, "mallory")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(User.objects.filter(username="github-666").exists())
        ev = AuditEvent.objects.get(action="auth.denied")
        self.assertEqual(ev.actor, "github:mallory")

    @override_settings(SENTINEL_GITHUB_USERS="")
    def test_nobody_can_sign_in_until_someone_is_listed(self):
        self.assertEqual(self.sign_in(1, "alice").status_code, 403)

    def test_forged_or_missing_state_is_rejected(self):
        self.client.get("/auth/github/login/")
        with _GH(1, "alice"):
            r = self.client.get("/auth/github/callback/", {"code": "abc", "state": "wrong"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(User.objects.exists())
        self.assertEqual(self.client.get("/auth/github/callback/", {"code": "abc"}).status_code, 400)

    def test_state_cannot_be_replayed(self):
        r = self.client.get("/auth/github/login/")
        state = urllib.parse.parse_qs(urllib.parse.urlparse(r["Location"]).query)["state"][0]
        with _GH(1001, "bob"):
            self.assertEqual(self.client.get("/auth/github/callback/", {"code": "a", "state": state}).status_code, 302)
            self.assertEqual(self.client.get("/auth/github/callback/", {"code": "a", "state": state}).status_code, 400)

    def test_next_cannot_redirect_off_site(self):
        r = self.sign_in(1001, "bob", next_url="https://evil.example/phish")
        self.assertEqual(r["Location"], "/server/")

    def test_identity_is_the_github_id_so_a_renamed_user_keeps_one_account(self):
        self.sign_in(1001, "bob")
        self.client.logout()
        with override_settings(SENTINEL_GITHUB_USERS="bobby:operator"):
            self.sign_in(1001, "bobby")
        self.assertEqual(User.objects.filter(username="github-1001").count(), 1)
        self.assertEqual(User.objects.get(username="github-1001").first_name, "bobby")

    def test_someone_who_takes_a_released_name_does_not_inherit_the_old_account(self):
        self.sign_in(1001, "bob")
        self.client.logout()
        self.sign_in(2002, "bob")                       # same name, different GitHub id
        self.assertEqual(User.objects.filter(first_name="bob").count(), 2)
        self.assertNotEqual(User.objects.get(username="github-2002").pk, User.objects.get(username="github-1001").pk)

    def test_github_token_is_never_stored(self):
        self.sign_in(1001, "bob")
        blob = str(list(AuditEvent.objects.values_list("payload", flat=True))) + str(self.client.session.items())
        self.assertNotIn("gho_TOKEN_VALUE", blob)
        self.assertFalse(User.objects.get(username="github-1001").has_usable_password())

    def test_removed_user_is_logged_out_on_their_next_request(self):
        self.sign_in(1001, "bob")
        self.assertEqual(self.client.get("/server/").status_code, 200)
        with override_settings(SENTINEL_GITHUB_USERS="alice:admin"):
            r = self.client.get("/server/")
        self.assertEqual(r.status_code, 302)                           # bounced to sign in
        self.assertTrue(AuditEvent.objects.filter(action="auth.revoked", actor="github:bob").exists())

    def test_demotion_applies_without_signing_in_again(self):
        self.sign_in(1, "alice")
        with override_settings(SENTINEL_GITHUB_USERS="alice:auditor"):
            self.client.get("/server/")
        u = User.objects.get(username="github-1")
        self.assertFalse(u.is_superuser)
        self.assertEqual(list(u.groups.values_list("name", flat=True)), ["Sentinel auditors"])


@override_settings(SENTINEL_GITHUB_USERS=USERS, SENTINEL_GITHUB_CLIENT_ID="cid", SENTINEL_BASE_URL="http://127.0.0.1:8000")
class RolesAndRunsTests(TestCase):
    def setUp(self):
        self.target = Target.objects.create(name="demo", git_url="https://github.com/o/demo")

    def sign_in(self, uid, login):
        r = self.client.get("/auth/github/login/")
        state = urllib.parse.parse_qs(urllib.parse.urlparse(r["Location"]).query)["state"][0]
        with _GH(uid, login):
            self.client.get("/auth/github/callback/", {"code": "a", "state": state})

    def test_anonymous_is_sent_to_sign_in(self):
        r = self.client.get("/server/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/auth/github/login/"))

    @mock.patch("pipeline.views.jobs.start_async")
    def test_operator_can_start_a_run_and_it_is_attributed_to_them(self, start):
        self.sign_in(1001, "bob")
        r = self.client.post("/server/start/", {"target": self.target.pk})
        self.assertEqual(r.status_code, 302)
        start.assert_called_once()
        self.assertEqual(AuditEvent.objects.get(action="run.created").actor, "github:bob")
        self.assertContains(self.client.get("/server/"), "by github:bob")

    @mock.patch("pipeline.views.jobs.start_async")
    def test_auditor_can_read_but_not_start_runs(self, start):
        self.sign_in(1002, "carol")
        self.assertEqual(self.client.get("/server/").status_code, 200)
        self.assertEqual(self.client.post("/server/start/", {"target": self.target.pk}).status_code, 403)
        start.assert_not_called()
        self.assertEqual(AuditEvent.objects.get(action="auth.denied").actor, "github:carol")

    @mock.patch("pipeline.views.jobs.start_async")
    def test_auditor_can_see_audit_trail_in_admin_but_operator_cannot(self, _):
        self.sign_in(1002, "carol")
        self.assertEqual(self.client.get("/admin/audit/auditevent/").status_code, 200)
        self.client.logout()
        self.sign_in(1001, "bob")
        self.assertEqual(self.client.get("/admin/audit/auditevent/").status_code, 403)

    def test_start_requires_post(self):
        self.sign_in(1001, "bob")
        self.assertEqual(self.client.get("/server/start/").status_code, 405)

    def test_sign_out_is_recorded(self):
        self.sign_in(1001, "bob")
        self.client.post("/auth/logout/")
        self.assertTrue(AuditEvent.objects.filter(action="auth.logout", actor="github:bob").exists())
        self.assertEqual(self.client.get("/server/").status_code, 302)
