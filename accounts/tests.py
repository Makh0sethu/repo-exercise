import urllib.parse
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from audit.log import verify_chain
from audit.models import AuditEvent
from pipeline.models import Finding, Run, Step, Target

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


@override_settings(SENTINEL_GITHUB_USERS=USERS, SENTINEL_GITHUB_CLIENT_ID="cid", SENTINEL_BASE_URL="http://127.0.0.1:8000")
class FlowTests(TestCase):
    def setUp(self):
        self.target = Target.objects.create(name="demo", git_url="https://github.com/o/demo")
        self.run = Run.objects.create(target=self.target, pipeline=[], status="passed", requested_by="github:bob", commit_sha="a" * 40)
        step = Step.objects.create(run=self.run, order=1, tool="npm_audit", reason="Check npm lockfile", status="passed", exit_code=0, argv=["npm", "audit"])
        Finding.objects.create(run=self.run, step=step, severity="critical", title="Bad thing in lodash", location="lodash (package-lock.json)", detail={"fixed": ["4.17.21"]})
        Finding.objects.create(run=self.run, step=step, severity="low", title="Minor thing", location="x")

    def sign_in(self, uid, login, next_url=None):
        r = self.client.get("/auth/github/login/" + (f"?next={urllib.parse.quote(next_url)}" if next_url else ""))
        state = urllib.parse.parse_qs(urllib.parse.urlparse(r["Location"]).query)["state"][0]
        with _GH(uid, login):
            return self.client.get("/auth/github/callback/", {"code": "a", "state": state})

    def nav(self):
        return self.client.get("/").content.decode()

    def test_nav_by_role(self):
        self.sign_in(1001, "bob")                                   # operator
        page = self.nav()
        self.assertIn("Security runs", page); self.assertNotIn(">Admin", page)
        self.assertIn("github:bob", page); self.assertIn("operator", page)
        self.assertIn("Sign out", page); self.assertNotIn("Sign in with GitHub", page)
        self.client.logout(); self.sign_in(1002, "carol")           # auditor
        page = self.nav(); self.assertIn("Security runs", page); self.assertIn(">Admin", page)
        self.client.logout(); self.sign_in(1, "alice")              # admin
        self.assertIn(">Admin", self.nav())

    def test_run_detail_shows_steps_findings_and_a_way_back(self):
        self.sign_in(1001, "bob")
        page = self.client.get(f"/server/runs/{self.run.pk}/")
        self.assertContains(page, "Bad thing in lodash"); self.assertContains(page, "Check npm lockfile")
        self.assertContains(page, "4.17.21"); self.assertContains(page, "← All runs"); self.assertContains(page, 'aria-label="Breadcrumb"')
        body = page.content.decode()
        self.assertLess(body.index("Bad thing in lodash"), body.index("Minor thing"))      # worst first

    def test_run_page_for_a_run_in_progress_polls_its_status(self):
        Run.objects.filter(pk=self.run.pk).update(status="running")
        self.sign_in(1001, "bob")
        self.assertContains(self.client.get(f"/server/runs/{self.run.pk}/"), f"/server/runs/{self.run.pk}/status/")
        self.assertContains(self.client.get("/server/"), f"/server/runs/{self.run.pk}/status/")

    def test_status_endpoint(self):
        self.sign_in(1001, "bob")
        j = self.client.get(f"/server/runs/{self.run.pk}/status/").json()
        self.assertEqual((j["status"], j["done"]), ("passed", True))
        Run.objects.filter(pk=self.run.pk).update(status="running")
        self.assertFalse(self.client.get(f"/server/runs/{self.run.pk}/status/").json()["done"])

    def test_run_pages_need_sign_in_and_permission(self):
        self.assertEqual(self.client.get(f"/server/runs/{self.run.pk}/").status_code, 302)
        self.assertEqual(self.client.get(f"/server/runs/{self.run.pk}/status/").status_code, 302)
        User.objects.create_user("plain", password="pw-123456")
        self.client.login(username="plain", password="pw-123456")
        r = self.client.get(f"/server/runs/{self.run.pk}/")
        self.assertContains(r, "permission to view security runs", status_code=403)
        self.assertContains(r, 'aria-label="Main"', status_code=403)                      # friendly page, still has the nav
        self.assertTrue(AuditEvent.objects.filter(action="auth.denied", actor="plain").exists())
        self.assertEqual(self.client.get(f"/server/runs/{self.run.pk}/status/").status_code, 403)

    def test_unknown_run_is_a_404_page(self):
        self.sign_in(1001, "bob")
        self.assertEqual(self.client.get("/server/runs/9999/").status_code, 404)

    def test_sign_in_returns_you_to_the_page_you_wanted(self):
        r = self.client.get(f"/server/runs/{self.run.pk}/")
        self.assertIn("/auth/github/login/", r["Location"]); self.assertIn("next=", r["Location"])
        r = self.sign_in(1001, "bob", next_url=f"/server/runs/{self.run.pk}/")
        self.assertEqual(r["Location"], f"/server/runs/{self.run.pk}/")

    def test_sign_in_button_remembers_the_current_page(self):
        self.assertContains(self.client.get("/results/"), "/auth/github/login/?next=/results/")

    def test_signing_in_keeps_results_but_signing_out_clears_them(self):
        with mock.patch("scanner.scan._get_json", side_effect=lambda u, b=None, timeout=20: {"results": [{} for _ in b["queries"]]}):
            self.client.post("/scan/", {"pasted": "django==4.2.1"})
        self.sign_in(1001, "bob")
        self.assertContains(self.client.get("/results/"), 'class="iz-card result-row"')     # survived sign-in
        self.client.post("/auth/logout/")
        self.assertContains(self.client.get("/results/"), "No results in this session yet")  # gone after sign-out

    def test_after_sign_out_back_cannot_show_a_cached_signed_in_page(self):
        self.sign_in(1001, "bob")
        self.assertEqual(self.client.get("/server/")["Cache-Control"], "no-store, max-age=0")
