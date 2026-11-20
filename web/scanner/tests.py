import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from datetime import timedelta

from .models import ScanLog

from .parsers import parse_inputs, parse_other, parse_requirements


class RequirementsTests(SimpleTestCase):
    def test_exact_loose_and_bare(self):
        pkgs, bad = parse_requirements(
            "Django==4.2.1\nrequests\npsycopg2-binary>=2.9  # driver\n"
            "uvicorn[standard]==0.30.0 ; python_version>'3.8'\n# comment\n-r base.txt\n")
        got = {p.name: (p.version, p.spec) for p in pkgs}
        self.assertEqual(got["django"], ("4.2.1", "==4.2.1"))
        self.assertEqual(got["requests"], (None, ""))
        self.assertEqual(got["psycopg2-binary"], (None, ">=2.9"))
        self.assertEqual(got["uvicorn"][0], "0.30.0")
        self.assertEqual(bad, [("requirements.txt", "-r base.txt")])

    def test_names_are_normalised(self):
        pkgs, _ = parse_requirements("PyYAML==5.3\nzope.interface==6.0\n")
        self.assertEqual([p.name for p in pkgs], ["pyyaml", "zope-interface"])


class NpmTests(SimpleTestCase):
    def test_package_json_ranges_are_name_only(self):
        r = parse_inputs([("package.json", json.dumps(
            {"dependencies": {"lodash": "4.17.15", "react": "^18.3.1"}, "devDependencies": {"jest": "~29.0.0"}}))])
        got = {p.name: p.version for p in r.packages}
        self.assertEqual(got, {"lodash": "4.17.15", "react": None, "jest": None})

    def test_lockfile_wins_over_package_json(self):
        pj = json.dumps({"dependencies": {"express": "^4.18.0"}})
        lock = json.dumps({"lockfileVersion": 3, "packages": {
            "": {"name": "app"},
            "node_modules/express": {"version": "4.18.2"},
            "node_modules/express/node_modules/qs": {"version": "6.11.0"}}})
        r = parse_inputs([("package.json", pj), ("package-lock.json", lock)])
        self.assertEqual({(p.name, p.version) for p in r.packages}, {("express", "4.18.2"), ("qs", "6.11.0")})

    def test_lockfile_v1(self):
        lock = json.dumps({"lockfileVersion": 1, "dependencies": {"a": {"version": "1.0.0", "dependencies": {"b": {"version": "2.0.0"}}}}})
        self.assertEqual(len(parse_inputs([("package-lock.json", lock)]).packages), 2)

    def test_bad_json_is_reported(self):
        r = parse_inputs([("package.json", "{nope")])
        self.assertEqual(r.packages, [])
        self.assertEqual(len(r.unreadable), 1)


class OtherSoftwareTests(SimpleTestCase):
    def test_name_version_lines(self):
        pkgs, bad = parse_other("postgresql 15.4\nPostGIS 3.3.2\njust-a-name\n")
        self.assertEqual([(p.name, p.version) for p in pkgs], [("postgresql", "15.4"), ("postgis", "3.3.2")])
        self.assertEqual(bad, [("Other software", "just-a-name")])


class ViewTests(TestCase):
    def test_scan_page(self):
        r = self.client.get("/")
        self.assertContains(r, "Scan dependencies")

    def test_theme_defaults_to_light_with_switch(self):
        r = self.client.get("/")
        self.assertContains(r, '<html lang="en" data-theme="light">')
        self.assertContains(r, 'id="theme-toggle"')
        self.assertEqual(self.client.get("/demo/").status_code, 404)

    def test_preview_lists_packages(self):
        r = self.client.post("/parse/", {"pasted": "django==4.2.1\nrequests", "other": "postgresql 15.4"})
        names = [p["name"] for p in r.json()["packages"]]
        self.assertEqual(sorted(names), ["django", "postgresql", "requests"])

    def test_empty_scan_is_rejected(self):
        r = self.client.post("/scan/", {"pasted": "", "other": ""})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "No packages found", status_code=400)

    def test_upload_too_large(self):
        big = SimpleUploadedFile("requirements.txt", b"a==1\n" * 250_000)
        r = self.client.post("/scan/", {"files": big})
        self.assertEqual(r.status_code, 413)

    def test_scan_uses_osv_and_marks_unreachable(self):
        with mock.patch("scanner.scan._get_json", side_effect=OSError("offline")):
            r = self.client.post("/scan/", {"pasted": "django==4.2.1"}, follow=True)
        self.assertContains(r, "OSV.dev could not be reached")
        self.assertContains(r, "Not checked · 1")
        self.assertNotContains(r, "✓ clean")

    def test_other_software_is_not_called_clean(self):
        def fake(url, body=None, timeout=20):
            return {"results": [{} for _ in body["queries"]]}
        with mock.patch("scanner.scan._get_json", side_effect=fake):
            r = self.client.post("/scan/", {"pasted": "numpy==2.1.3", "other": "postgresql 15.4"}, follow=True)
        self.assertContains(r, "Needs NVD, not connected yet")
        self.assertContains(r, "1 of 1 checked")

    def test_scan_with_osv_results(self):
        def fake(url, body=None, timeout=20):
            if "querybatch" in url:
                return {"results": [{"vulns": [{"id": "GHSA-test"}]}]}
            return {"id": "GHSA-test", "aliases": ["CVE-2023-36053"], "summary": "ReDoS",
                    "database_specific": {"severity": "HIGH"},
                    "affected": [{"package": {"ecosystem": "PyPI", "name": "django"},
                                  "ranges": [{"events": [{"introduced": "0"}, {"fixed": "4.2.3"}]}]}]}
        with mock.patch("scanner.scan._get_json", side_effect=fake):
            r = self.client.post("/scan/", {"pasted": "django==4.2.1"}, follow=True)
        self.assertContains(r, "CVE-2023-36053")
        self.assertContains(r, "4.2.3")
        self.assertContains(r, "scanner/logos/django.png")
        self.assertContains(r, 'id="scan-data"')

    def test_package_without_logo_gets_ecosystem_logo(self):
        def fake(url, body=None, timeout=20):
            return {"results": [{} for _ in body["queries"]]}
        with mock.patch("scanner.scan._get_json", side_effect=fake):
            r = self.client.post("/scan/", {"pasted": "requests==2.32.0"}, follow=True)
        self.assertContains(r, "scanner/logos/python.png")


def _no_vulns(url, body=None, timeout=20):
    return {"results": [{} for _ in body["queries"]]}


class TracingTests(TestCase):
    def scan(self, **extra):
        with mock.patch("scanner.scan._get_json", side_effect=_no_vulns):
            return self.client.post("/scan/", {"pasted": "django==4.2.1\nrequests"},
                                    HTTP_USER_AGENT="pentest-tool/1.0", **extra)

    def test_scan_is_logged_with_ip_time_and_packages(self):
        r = self.scan(REMOTE_ADDR="203.0.113.7")
        row = ScanLog.objects.get()
        self.assertEqual(row.ip, "203.0.113.7")
        self.assertEqual(row.outcome, "ok")
        self.assertEqual(row.user_agent, "pentest-tool/1.0")
        self.assertEqual(sorted(row.packages), ["PyPI:django@4.2.1", "PyPI:requests@*"])
        self.assertEqual(row.sources["OSV.dev"], "ok")
        self.assertEqual(row.request_id, r["X-Request-ID"])
        self.assertEqual(r.status_code, 302)
        self.assertContains(self.client.get(r["Location"]), "Ref " + row.request_id[:8])

    def test_failed_scans_are_logged_too(self):
        self.client.post("/scan/", {"pasted": ""})
        self.client.post("/scan/", {"files": SimpleUploadedFile("r.txt", b"a==1\n" * 250_000)})
        self.assertEqual(sorted(ScanLog.objects.values_list("outcome", flat=True)), ["empty", "too_large"])

    def test_forwarded_for_is_ignored_unless_trusted(self):
        self.scan(REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="1.2.3.4")
        self.assertEqual(ScanLog.objects.get().ip, "10.0.0.1")

    @override_settings(SENTINEL_TRUST_X_FORWARDED_FOR=True)
    def test_forwarded_for_behind_own_proxy_uses_last_hop(self):
        self.scan(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="6.6.6.6, 198.51.100.4")
        self.assertEqual(ScanLog.objects.get().ip, "198.51.100.4")

    def test_every_request_gets_an_access_log_line(self):
        with self.assertLogs("sentinel.access", "INFO") as cm:
            r = self.client.get("/", REMOTE_ADDR="192.0.2.9")
        line = json.loads(cm.output[0].split(":", 2)[2])
        self.assertEqual((line["ip"], line["path"], line["status"]), ("192.0.2.9", "/", 200))
        self.assertEqual(line["request_id"], r["X-Request-ID"])

    def test_purge_removes_old_logs(self):
        self.scan()
        ScanLog.objects.update(created_at=timezone.now() - timedelta(days=31))
        self.scan()
        call_command("purge_scan_logs", stdout=mock.MagicMock())
        self.assertEqual(ScanLog.objects.count(), 1)

    def test_privacy_notice_is_shown(self):
        self.assertContains(self.client.get("/"), "your IP address and the package names are logged")


class PermissionTests(TestCase):
    def setUp(self):
        with mock.patch("scanner.scan._get_json", side_effect=_no_vulns):
            self.client.post("/scan/", {"pasted": "django==4.2.1"})
        self.url = "/admin/scanner/scanlog/"

    def login(self, auditor=False, superuser=False):
        u = User.objects.create_user("u", password="pw-123456", is_staff=True, is_superuser=superuser)
        if auditor:
            u.groups.add(Group.objects.get(name="Sentinel auditors"))
        self.client.login(username="u", password="pw-123456")

    def test_anonymous_is_sent_to_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_staff_without_group_cannot_see_logs(self):
        self.login()
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_auditor_can_read_but_not_change_or_delete(self):
        self.login(auditor=True)
        self.assertContains(self.client.get(self.url), "PyPI:django@4.2.1")
        row = ScanLog.objects.get()
        self.assertEqual(self.client.get(f"{self.url}{row.pk}/change/").status_code, 200)
        self.assertEqual(self.client.post(f"{self.url}{row.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.assertTrue(ScanLog.objects.exists())

    def test_auditor_can_export_csv(self):
        self.login(auditor=True)
        r = self.client.post(self.url, {"action": "export_csv", "_selected_action": [ScanLog.objects.get().pk]})
        self.assertEqual(r["Content-Type"], "text/csv")
        self.assertIn("PyPI:django@4.2.1", r.content.decode())

    def test_superuser_can_delete(self):
        self.login(superuser=True)
        row = ScanLog.objects.get()
        self.client.post(f"{self.url}{row.pk}/delete/", {"post": "yes"})
        self.assertFalse(ScanLog.objects.exists())


class NavigationAndSessionTests(TestCase):
    def scan(self, client=None, pasted="django==4.2.1"):
        client = client or self.client
        with mock.patch("scanner.scan._get_json", side_effect=_no_vulns):
            return client.post("/scan/", {"pasted": pasted})

    def test_scan_redirects_to_its_own_result_address(self):
        r = self.scan()
        self.assertEqual(r.status_code, 302)
        self.assertRegex(r["Location"], r"^/results/[\w-]+/$")
        page = self.client.get(r["Location"])
        self.assertContains(page, "Vulnerability")
        self.assertEqual(self.client.get(r["Location"]).status_code, 200)       # Refresh and Back are safe

    def test_refreshing_a_result_does_not_run_or_log_the_scan_again(self):
        loc = self.scan()["Location"]
        for _ in range(3):
            self.client.get(loc)
        self.assertEqual(ScanLog.objects.count(), 1)

    def test_results_belong_to_the_session_that_made_them(self):
        loc = self.scan()["Location"]
        self.assertEqual(Client().get(loc).status_code, 404)                    # another visitor cannot open it
        self.assertEqual(self.client.get("/results/not-a-real-id/").status_code, 404)

    def test_results_list_shows_recent_scans_newest_first_and_keeps_five(self):
        locs = [self.scan(pasted=f"pkg{i}==1.0")["Location"] for i in range(7)]
        page = self.client.get("/results/")
        self.assertEqual(page.content.decode().count('class="iz-card result-row"'), 5)
        self.assertContains(page, locs[-1]); self.assertNotContains(page, locs[0])
        self.assertEqual(self.client.get(locs[0]).status_code, 404)             # oldest dropped

    def test_empty_results_list_explains_itself(self):
        self.assertContains(self.client.get("/results/"), "No results in this session yet")

    def test_failed_scan_keeps_what_the_person_typed(self):
        r = self.client.post("/scan/", {"pasted": "-r other.txt\n# nothing usable", "other": "not a valid line"})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "-r other.txt", status_code=400)
        self.assertContains(r, "not a valid line", status_code=400)

    def test_session_ends_when_the_browser_closes(self):
        from django.conf import settings
        self.assertTrue(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)
        self.assertLessEqual(settings.SESSION_COOKIE_AGE, 8 * 3600)

    def test_dynamic_pages_are_never_cached_but_static_files_are_left_alone(self):
        self.assertEqual(self.client.get("/")["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(self.client.get(self.scan()["Location"])["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(self.client.post("/parse/", {"pasted": "a==1"})["Cache-Control"], "no-store, max-age=0")
        from django.http import HttpResponse
        from django.test import RequestFactory
        from .middleware import NoStoreMiddleware
        css = NoStoreMiddleware(lambda r: HttpResponse("a{}", content_type="text/css"))(RequestFactory().get("/x.css"))
        self.assertNotIn("Cache-Control", css)                                   # static assets keep normal caching

    def test_nav_for_an_anonymous_visitor(self):
        page = self.client.get("/").content.decode()
        self.assertIn('aria-label="Main"', page)
        self.assertIn(">Scan", page); self.assertIn(">Results", page)
        self.assertIn("Sign in with GitHub", page)
        self.assertNotIn("Security runs", page); self.assertNotIn(">Admin", page)

    def test_current_page_is_marked_in_the_nav(self):
        self.assertRegex(self.client.get("/").content.decode(), r'class="nav-link is-active" aria-current="page">Scan')
        self.assertRegex(self.client.get("/results/").content.decode(), r'class="nav-link is-active" aria-current="page">Results')

    def test_nav_counts_saved_results(self):
        self.scan(); self.scan(pasted="flask==3.0.0")
        self.assertContains(self.client.get("/"), 'aria-label="2 saved"')

    def test_missing_page_has_the_nav_and_a_way_back(self):
        r = self.client.get("/results/nope/")
        self.assertContains(r, 'aria-label="Main"', status_code=404)
        self.assertContains(r, "data-back", status_code=404)

    def test_result_page_has_breadcrumb_and_back_to_input(self):
        page = self.client.get(self.scan()["Location"])
        self.assertContains(page, 'aria-label="Breadcrumb"')
        self.assertContains(page, "← Edit input")
        self.assertContains(page, "/?new=1")                                     # "New scan" starts clean

    def test_new_scan_link_and_state_hooks_exist_on_the_form(self):
        page = self.client.get("/")
        self.assertContains(page, "data-keep-state"); self.assertContains(page, 'id="clear-form"')
