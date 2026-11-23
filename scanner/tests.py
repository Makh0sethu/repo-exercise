import json
import time
from datetime import datetime
from unittest import mock

from .http_client import RateLimited, RecentFailure, cache_peek, cached_fetch, clear_cache, stale_since
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from datetime import timedelta

from .models import ScanLog

from .news import match_news
from .parsers import Package, parse_inputs, parse_other, parse_requirements
from .scan import build_result, run_scan
from .sources import _NVD_BUDGET, _nvd_get, query_osv
from .sources import query_retirejs
from .versions import compare_versions, cvss3_bucket, cvss4_bucket, version_in_range, version_sort_key


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

    def test_inline_comment_and_hash_continuations(self):
        text = "django==4.2.10#inline\nrequests==2.32.3 \\\n    --hash=sha256:abc123\n"
        pkgs, bad = parse_requirements(text)
        self.assertEqual(bad, [])
        self.assertEqual({p.name: p.version for p in pkgs}, {"django": "4.2.10", "requests": "2.32.3"})


class ModernLockfileParserTests(SimpleTestCase):
    def test_pyproject_toml_pep621_and_poetry(self):
        toml_text = (
            '[project]\n'
            'name = "demo"\n'
            'dependencies = ["Django==5.1.2", "requests>=2.31"]\n'
            '[project.optional-dependencies]\n'
            'dev = ["pytest==8.3.0"]\n'
            '[tool.poetry.dependencies]\n'
            'python = "^3.12"\n'
            'httpx = "0.27.2"\n'
        )
        r = parse_inputs([("pyproject.toml", toml_text)])
        got = {p.name: p.version for p in r.packages}
        self.assertEqual(got, {"django": "5.1.2", "requests": None, "pytest": "8.3.0", "httpx": "0.27.2"})

    def test_poetry_and_uv_lock(self):
        lock_text = (
            '[[package]]\nname = "Django"\nversion = "5.1.2"\n'
            '[[package]]\nname = "urllib3"\nversion = "2.2.3"\n'
        )
        r = parse_inputs([("uv.lock", lock_text)])
        self.assertEqual({(p.name, p.version) for p in r.packages}, {("django", "5.1.2"), ("urllib3", "2.2.3")})

    def test_yarn_lock_v1_and_v2(self):
        yarn_text = (
            '# yarn lockfile v1\n\n'
            'lodash@^4.17.20:\n'
            '  version "4.17.21"\n\n'
            '"@types/node@npm:^20.0.0":\n'
            '  version: 20.11.5\n'
        )
        r = parse_inputs([("yarn.lock", yarn_text)])
        self.assertEqual({(p.name, p.version) for p in r.packages}, {("lodash", "4.17.21"), ("@types/node", "20.11.5")})

    def test_pnpm_lock_yaml(self):
        pnpm_text = (
            "lockfileVersion: '9.0'\n\n"
            "packages:\n"
            "  lodash@4.17.21:\n"
            "    resolution: {integrity: sha512-xxx}\n"
            "  '@types/node@20.11.0':\n"
            "    resolution: {integrity: sha512-yyy}\n"
            "  /axios@1.6.7:\n"
            "    resolution: {integrity: sha512-zzz}\n"
        )
        r = parse_inputs([("pnpm-lock.yaml", pnpm_text)])
        self.assertEqual(
            {(p.name, p.version) for p in r.packages},
            {("lodash", "4.17.21"), ("@types/node", "20.11.0"), ("axios", "1.6.7")},
        )


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


class VersionAndCvssTests(SimpleTestCase):
    def test_semantic_version_sorting(self):
        versions = ["4.2.10", "4.2.2", "4.2.9", "4.2.10rc1"]
        self.assertEqual(sorted(versions, key=version_sort_key), ["4.2.2", "4.2.9", "4.2.10rc1", "4.2.10"])

    def test_version_range_matching(self):
        self.assertTrue(version_in_range("1.9.1", at_or_above="1.0.0", below="1.12.0"))
        self.assertFalse(version_in_range("1.12.0", at_or_above="1.0.0", below="1.12.0"))
        self.assertEqual(compare_versions("1.2", "1.2.0"), 0)

    def test_cvss3_and_cvss4_bucketing(self):
        self.assertEqual(cvss3_bucket("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"), "CRITICAL")
        self.assertEqual(cvss3_bucket("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N"), "MODERATE")
        self.assertEqual(cvss4_bucket("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:H/SI:H/SA:H"), "CRITICAL")


class PipelineLogicTests(SimpleTestCase):
    def test_deduplication_merges_aliases_sources_and_sorts_fixes(self):
        pkg = Package("PyPI", "django", "4.2.1")
        vulns = {
            (pkg.key, pkg.version): [
                {
                    "id": "GHSA-aaaa",
                    "aliases": ["CVE-2023-36053"],
                    "summary": "ReDoS",
                    "severity": "MODERATE",
                    "fixed": ["4.2.9"],
                    "published": "2023-07-03",
                    "url": "https://osv.dev/vulnerability/GHSA-aaaa",
                    "sources": ["OSV.dev"],
                    "exploited": False,
                },
                {
                    "id": "PYSEC-2023-100",
                    "aliases": ["CVE-2023-36053"],
                    "summary": "ReDoS",
                    "severity": "HIGH",
                    "fixed": ["4.2.10"],
                    "published": "",
                    "url": "https://osv.dev/vulnerability/PYSEC-2023-100",
                    "sources": ["OSV.dev", "CISA KEV"],
                    "exploited": True,
                },
            ]
        }
        result = build_result([pkg], vulns, {"OSV.dev": "ok"}, news=[])
        self.assertEqual(result["stats"]["problems"], 1)
        self.assertEqual(result["stats"]["exploited"], 1)
        merged = result["vulnerable"][0]["vulns"][0]
        self.assertEqual(merged["cve"], "CVE-2023-36053")
        self.assertEqual(merged["severity"], "HIGH")
        self.assertTrue(merged["exploited"])
        self.assertEqual(merged["fixed"], ["4.2.9", "4.2.10"])

    def test_retirejs_matches_vulnerable_version_only(self):
        repo = {
            "jquery": {
                "vulnerabilities": [
                    {
                        "atOrAbove": "1.0.0",
                        "below": "3.5.0",
                        "severity": "medium",
                        "identifiers": {"CVE": ["CVE-2020-11022"], "summary": "XSS in jQuery"},
                        "info": ["https://blog.jquery.com/2020/04/10/jquery-3-5-0-released/"],
                    }
                ]
            }
        }
        old_jq = Package("npm", "jquery", "3.4.1")
        new_jq = Package("npm", "jquery", "3.7.1")
        out = query_retirejs([old_jq, new_jq], repo=repo)
        self.assertIn((old_jq.key, "3.4.1"), out)
        self.assertNotIn((new_jq.key, "3.7.1"), out)
        self.assertEqual(out[(old_jq.key, "3.4.1")][0]["severity"], "MODERATE")

    def test_news_matching_skips_generic_names_and_matches_cves(self):
        stories = [
            {
                "source": "The Hacker News",
                "title": "Critical Django bug CVE-2024-99999 patched",
                "url": "https://example.com/1",
                "text": "Also click here for six tips.",
                "date": "2026-11-18T10:00:00+00:00",
            }
        ]
        pkgs = [Package("PyPI", "django", "4.2.1"), Package("PyPI", "six", "1.16.0")]
        with mock.patch("scanner.news.fetch_all_rss_stories", return_value=(stories, True)):
            matched, status = match_news(pkgs, {"CVE-2024-99999"}, include_hn=False)
        self.assertEqual(status, "ok")
        self.assertEqual(len(matched), 1)
        self.assertIn("django", matched[0]["matches"])
        self.assertIn("CVE-2024-99999", matched[0]["matches"])
        self.assertNotIn("six", matched[0]["matches"])


class ViewTests(TestCase):
    def setUp(self):
        clear_cache()

    def _patch_secondary_sources(self):
        """Context manager stack mocking secondary sources offline-safely."""
        return [
            mock.patch("scanner.scan.fetch_cisa_kev", return_value=set()),
            mock.patch("scanner.scan.query_retirejs", return_value={}),
            mock.patch("scanner.scan.query_nvd_other", return_value={}),
            mock.patch("scanner.scan.match_news", return_value=([], "ok")),
        ]

    def test_scan_page_and_security_headers(self):
        r = self.client.get("/scan/")
        self.assertContains(r, "Scan dependencies")
        self.assertIn("Content-Security-Policy", r)
        self.assertIn("script-src 'self'", r["Content-Security-Policy"])
        self.assertIn("Permissions-Policy", r)

    def test_theme_defaults_to_light_with_switch(self):
        r = self.client.get("/")
        self.assertContains(r, '<html lang="en" data-theme="light">')
        self.assertContains(r, 'id="theme-toggle"')
        self.assertContains(r, "theme-init.js")
        self.assertEqual(self.client.get("/demo/").status_code, 404)

    def test_preview_lists_packages(self):
        r = self.client.post("/parse/", {"pasted": "django==4.2.1\nrequests", "other": "postgresql 15.4"})
        names = [p["name"] for p in r.json()["packages"]]
        self.assertEqual(sorted(names), ["django", "postgresql", "requests"])

    def test_empty_scan_is_rejected(self):
        r = self.client.post("/scan/", {"pasted": "", "other": ""})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "No packages found", status_code=400)

    def test_malformed_content_length_returns_400(self):
        r = self.client.post("/parse/", {"pasted": "django==4.2.1"}, CONTENT_LENGTH="not-a-number")
        self.assertEqual(r.status_code, 400)

    def test_upload_too_large(self):
        big = SimpleUploadedFile("requirements.txt", b"a==1\n" * 250_000)
        r = self.client.post("/scan/", {"files": big})
        self.assertEqual(r.status_code, 413)

    @override_settings(SENTINEL_RATE_LIMIT=2)
    def test_rate_limiting_returns_429(self):
        with (
            mock.patch("scanner.scan._get_json", return_value={"results": [{}]}),
            mock.patch("scanner.scan.fetch_cisa_kev", return_value=set()),
            mock.patch("scanner.scan.query_retirejs", return_value={}),
            mock.patch("scanner.scan.query_nvd_other", return_value={}),
            mock.patch("scanner.scan.fetch_all_rss_stories", return_value=([], True)),
            mock.patch("scanner.scan.match_news", return_value=([], "ok")),
        ):
            self.assertEqual(self.client.post("/scan/", {"pasted": "django==4.2.1"}).status_code, 302)
            self.assertEqual(self.client.post("/scan/", {"pasted": "django==4.2.1"}).status_code, 302)
            r3 = self.client.post("/scan/", {"pasted": "django==4.2.1"})
            self.assertEqual(r3.status_code, 429)
            self.assertEqual(r3["Retry-After"], "60")

    def test_scan_uses_osv_and_marks_unreachable(self):
        with (
            mock.patch("scanner.scan._get_json", side_effect=OSError("offline")),
            mock.patch("scanner.scan.fetch_cisa_kev", return_value=set()),
            mock.patch("scanner.scan.query_retirejs", return_value={}),
            mock.patch("scanner.scan.query_nvd_other", return_value={}),
            mock.patch("scanner.scan.fetch_all_rss_stories", return_value=([], True)),
            mock.patch("scanner.scan.match_news", return_value=([], "ok")),
        ):
            r = self.client.post("/scan/", {"pasted": "django==4.2.1"}, follow=True)
        self.assertContains(r, "OSV.dev could not be reached")
        self.assertContains(r, "Not checked · 1")
        self.assertNotContains(r, "✓ clean")

    def test_other_software_checked_via_nvd_and_unknown_marked_unchecked(self):
        def fake_osv(url, body=None, timeout=20):
            return {"results": [{} for _ in body["queries"]]}

        pg_key = (("Other", "postgresql"), "15.4")
        fake_nvd = {
            pg_key: [{
                "id": "CVE-2023-5869",
                "aliases": [],
                "summary": "Buffer overflow in PostgreSQL",
                "severity": "HIGH",
                "fixed": ["15.5"],
                "published": "2023-11-09",
                "url": "https://nvd.nist.gov/vuln/detail/CVE-2023-5869",
                "sources": ["NVD"],
                "exploited": False,
            }]
        }
        with (
            mock.patch("scanner.scan._get_json", side_effect=fake_osv),
            mock.patch("scanner.scan.fetch_cisa_kev", return_value={"CVE-2023-5869"}),
            mock.patch("scanner.scan.query_retirejs", return_value={}),
            mock.patch("scanner.scan.query_nvd_other", return_value=fake_nvd),
            mock.patch("scanner.scan.fetch_all_rss_stories", return_value=([], True)),
            mock.patch("scanner.scan.match_news", return_value=([], "ok")),
        ):
            r = self.client.post("/scan/", {"pasted": "numpy==2.1.3", "other": "postgresql 15.4\ncustomdb 1.0"}, follow=True)
        self.assertContains(r, "CVE-2023-5869")
        self.assertContains(r, "exploited in the wild")
        self.assertContains(r, "No CPE mapping for this software yet")

    def test_scan_with_osv_results_and_cve_org_enrichment(self):
        def fake(url, body=None, timeout=20):
            if "querybatch" in url:
                return {"results": [{"vulns": [{"id": "GHSA-test"}]}]}
            return {
                "id": "GHSA-test",
                "aliases": ["CVE-2023-36053"],
                "summary": "ReDoS",
                "affected": [{
                    "package": {"ecosystem": "PyPI", "name": "django"},
                    "ranges": [{"events": [{"introduced": "0"}, {"fixed": "4.2.9"}, {"fixed": "4.2.10"}]}],
                }],
            }

        with (
            mock.patch("scanner.scan._get_json", side_effect=fake),
            mock.patch("scanner.scan.fetch_cisa_kev", return_value=set()),
            mock.patch("scanner.scan.fetch_cve_org_severity", return_value="HIGH"),
            mock.patch("scanner.scan.query_retirejs", return_value={}),
            mock.patch("scanner.scan.query_nvd_other", return_value={}),
            mock.patch("scanner.scan.fetch_all_rss_stories", return_value=([], True)),
            mock.patch("scanner.scan.match_news", return_value=([], "ok")),
        ):
            r = self.client.post("/scan/", {"pasted": "django==4.2.1"}, follow=True)
        self.assertContains(r, "CVE-2023-36053")
        self.assertContains(r, "HIGH")
        self.assertContains(r, "4.2.10")
        self.assertContains(r, "scanner/logos/django.png")
        self.assertContains(r, 'id="scan-data"')

    def test_package_without_logo_gets_ecosystem_logo(self):
        def fake(url, body=None, timeout=20):
            return {"results": [{} for _ in body["queries"]]}

        with (
            mock.patch("scanner.scan._get_json", side_effect=fake),
            mock.patch("scanner.scan.fetch_cisa_kev", return_value=set()),
            mock.patch("scanner.scan.query_retirejs", return_value={}),
            mock.patch("scanner.scan.query_nvd_other", return_value={}),
            mock.patch("scanner.scan.fetch_all_rss_stories", return_value=([], True)),
            mock.patch("scanner.scan.match_news", return_value=([], "ok")),
        ):
            r = self.client.post("/scan/", {"pasted": "requests==2.32.0"}, follow=True)
        self.assertContains(r, "scanner/logos/python.png")


def _no_vulns(url, body=None, timeout=20):
    return {"results": [{} for _ in body["queries"]]}


class OfflineSourcesMixin:
    """Scan tests must not touch the network or share the per-IP rate-limit counter."""

    def setUp(self):
        super().setUp()
        clear_cache()
        for target, value in [
            ("scanner.scan.fetch_cisa_kev", set()),
            ("scanner.scan.query_retirejs", {}),
            ("scanner.scan.query_nvd_other", {}),
            ("scanner.scan.fetch_all_rss_stories", ([], True)),
            ("scanner.scan.match_news", ([], "ok")),
        ]:
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TracingTests(OfflineSourcesMixin, TestCase):
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
        self.assertContains(self.client.get("/scan/"), "your IP address and the package names are logged")


class PermissionTests(OfflineSourcesMixin, TestCase):
    def setUp(self):
        super().setUp()
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


class NavigationAndSessionTests(OfflineSourcesMixin, TestCase):
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

    def test_about_page_shows_the_site_map_without_the_admin_address(self):
        from django.conf import settings
        r = self.client.get("/about/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'aria-label="Site map"')
        self.assertContains(r, 'aria-label="Code architecture"')
        for route in ("/scan/", "/parse/", "/results/", "/server/audit/", "/server/runs/&lt;id&gt;/status/", "/auth/github/login/"):
            self.assertContains(r, f"<code>{route}</code>")
        self.assertNotContains(r, "/" + settings.SENTINEL_ADMIN_PATH)          # a private admin path stays private
        self.assertContains(r, 'aria-current="page">About')                     # nav marks it active

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
        self.assertRegex(self.client.get("/scan/").content.decode(), r'class="nav-link is-active" aria-current="page">Scan')
        self.assertRegex(self.client.get("/").content.decode(), r'class="nav-link is-active" aria-current="page">Home')
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
        self.assertContains(page, "/scan/?new=1")                                     # "New scan" starts clean

    def test_new_scan_link_and_state_hooks_exist_on_the_form(self):
        page = self.client.get("/scan/")
        self.assertContains(page, "data-keep-state"); self.assertContains(page, 'id="clear-form"')


class HealthTests(TestCase):
    def test_health_answers_ok_as_json(self):
        r = self.client.get("/health/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})
        self.assertEqual(self.client.post("/health/").status_code, 405)


@override_settings(SENTINEL_PUBLIC_ONLY=True, ROOT_URLCONF="config.urls_public")
class PublicOnlyModeTests(TestCase):
    """SENTINEL_PUBLIC_ONLY=1: the public test site shows the scanner only, no admin parts."""

    def test_admin_sign_in_and_security_runs_are_not_routed(self):
        from django.conf import settings
        for path in ("/" + settings.SENTINEL_ADMIN_PATH, "/admin/", "/server/", "/server/audit/",
                     "/server/runs/1/", "/auth/github/login/", "/auth/github/callback/"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.post("/server/start/").status_code, 404)

    def test_public_pages_still_work(self):
        for path in ("/", "/scan/", "/results/", "/about/", "/health/"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_nav_has_no_admin_links_or_sign_in(self):
        staff = User.objects.create_superuser("boss", password="x")
        self.client.force_login(staff)                                         # even a superuser sees none of it
        page = self.client.get("/about/").content.decode()
        for hidden in ("Security runs", ">Admin<", "Sign in with GitHub", "Sign out", "/auth/", "/server/"):
            self.assertNotIn(hidden, page, hidden)
        self.assertIn(">Scan<", page)

    def test_about_page_lists_only_public_parts(self):
        r = self.client.get("/about/")
        self.assertContains(r, "<code>/scan/</code>")
        self.assertContains(r, "<code>/health/</code>")
        for hidden in ("<b>pipeline</b>", "<b>accounts</b>", "<b>audit</b>", "tag--role", "tag--staff", "Signed-in operators"):
            self.assertNotContains(r, hidden)


class ScanDeadlineTests(OfflineSourcesMixin, TestCase):
    """A slow source is "timed out", a failing one is "unreachable", and neither is ever "clean"."""

    def _scan(self, osv):
        pkgs = parse_inputs([], "django==3.2.0").packages
        with mock.patch("scanner.scan.query_osv", side_effect=osv):
            return run_scan(pkgs)

    @override_settings(SENTINEL_SCAN_DEADLINE=0.2)
    def test_slow_source_is_reported_as_timed_out_not_unreachable(self):
        import time

        def slow(*a, **k):
            time.sleep(1.5)
            return {}

        started = time.monotonic()
        result = self._scan(slow)
        self.assertLess(time.monotonic() - started, 1.2)  # did not wait for the slow source
        self.assertEqual({s["name"]: s["status"] for s in result["sources"]}["OSV.dev"], "timeout")
        pkg = result["packages"][0]
        self.assertFalse(pkg["checked"])
        self.assertEqual(pkg["unchecked_reason"], "OSV.dev timed out")
        self.assertEqual(result["clean"], [])

    def test_failing_source_is_still_reported_as_unreachable(self):
        result = self._scan(OSError("offline"))
        self.assertEqual({s["name"]: s["status"] for s in result["sources"]}["OSV.dev"], "unreachable")
        self.assertEqual(result["packages"][0]["unchecked_reason"], "OSV.dev could not be reached")

    @override_settings(SENTINEL_SCAN_DEADLINE=0.2)
    def test_results_page_names_the_timed_out_source(self):
        import time
        with mock.patch("scanner.scan.query_osv", side_effect=lambda *a, **k: time.sleep(1.5) or {}):
            r = self.client.post("/scan/", {"pasted": "django==3.2.0"}, follow=True)
        self.assertContains(r, "OSV.dev timed out.")
        self.assertContains(r, "Not checked")
        self.assertNotContains(r, "OSV.dev could not be reached.")


class CachedFetchTests(SimpleTestCase):
    """Saved copies: reused while fresh, served (and noted) when the live source fails."""

    def setUp(self):
        clear_cache()

    def test_fresh_copy_is_reused_without_fetching_again(self):
        calls = []
        fetch = lambda: calls.append(1) or {"a": 1}
        self.assertEqual(cached_fetch("t", "k", 60, fetch), {"a": 1})
        self.assertEqual(cached_fetch("t", "k", 60, fetch), {"a": 1})
        self.assertEqual(len(calls), 1)

    def test_failure_serves_the_old_copy_and_says_so(self):
        cached_fetch("t", "k", -1, lambda: {"old": True})  # saved, but already expired

        def down():
            raise OSError("offline")

        started = time.time() - 1
        self.assertEqual(cached_fetch("t", "k", 60, down), {"old": True})
        self.assertIn("t", stale_since(started))

    def test_failure_with_nothing_saved_raises_then_backs_off(self):
        calls = []

        def down():
            calls.append(1)
            raise OSError("offline")

        with self.assertRaises(OSError):
            cached_fetch("t", "k", 60, down)
        with self.assertRaises(RecentFailure):  # remembered: the source is not hammered again
            cached_fetch("t", "k", 60, down)
        self.assertEqual(len(calls), 1)

    def test_a_failure_is_never_saved_as_data(self):
        with self.assertRaises(OSError):
            cached_fetch("t", "k", 60, mock.Mock(side_effect=OSError("offline")))
        self.assertEqual(cache_peek("t", "k"), (None, False, 0.0))

    def test_recovers_once_the_backoff_has_passed(self):
        with mock.patch("scanner.http_client.FAIL_BACKOFF", 0):
            with self.assertRaises(OSError):
                cached_fetch("t", "k", 60, mock.Mock(side_effect=OSError("offline")))
            self.assertEqual(cached_fetch("t", "k", 60, lambda: {"back": 1}), {"back": 1})

    def test_too_many_requests_backs_off_much_longer(self):
        from . import store
        from .http_client import LIMITED_BACKOFF, HttpError, _cache_key

        with self.assertRaises(HttpError):
            cached_fetch("t", "k", 60, mock.Mock(side_effect=HttpError("u", "HTTP 429", status_code=429)))
        entry = store.get(_cache_key("t", "k"))
        self.assertGreater(entry.retry_after - time.time(), LIMITED_BACKOFF - 5)

    def test_saved_data_survives_a_restart(self):
        from . import store

        cached_fetch("t", "k", 60, lambda: {"kept": 1})
        store._READY.clear()  # a new process knows nothing in memory, only the file
        refetch = mock.Mock(side_effect=AssertionError("refetched"))
        self.assertEqual(cached_fetch("t", "k", 60, refetch), {"kept": 1})


def _feed_xml():
    from datetime import timezone as dt_timezone
    from email.utils import format_datetime

    return (
        "<rss><channel><item><title>Django bug exploited</title><link>https://example.com/1</link>"
        f"<pubDate>{format_datetime(datetime.now(dt_timezone.utc))}</pubDate></item></channel></rss>"
    ).encode()


class FeedCacheTests(SimpleTestCase):
    def setUp(self):
        clear_cache()

    def test_all_feeds_down_is_a_failure_not_an_empty_answer_saved_for_an_hour(self):
        from .news import fetch_all_rss_stories

        with mock.patch("scanner.http_client.FAIL_BACKOFF", 0):
            with mock.patch("scanner.news.fetch_bytes", side_effect=OSError("offline")):
                self.assertEqual(fetch_all_rss_stories(), ([], False))
            # When the network returns, the next look works at once instead of reusing "nothing".
            with mock.patch("scanner.news.fetch_bytes", return_value=_feed_xml()):
                stories, ok = fetch_all_rss_stories()
        self.assertTrue(ok)
        self.assertEqual(len(stories), 6)  # one per feed

    def test_one_failing_feed_does_not_lose_the_others(self):
        from .news import FEEDS, fetch_all_rss_stories

        def fake(url, **kw):
            if "bleepingcomputer" in url:
                raise OSError("offline")
            return _feed_xml()

        with mock.patch("scanner.news.fetch_bytes", side_effect=fake):
            stories, ok = fetch_all_rss_stories()
        self.assertTrue(ok)
        self.assertEqual(len(stories), len(FEEDS) - 1)


class OsvSavedCopyTests(SimpleTestCase):
    def setUp(self):
        clear_cache()

    @staticmethod
    def _fake(url, body=None, timeout=20):
        if "querybatch" in url:
            return {"results": [{"vulns": [{"id": "GHSA-x"}]}]}
        return {"id": "GHSA-x", "aliases": ["CVE-2020-1"], "summary": "bug",
                "affected": [{"package": {"ecosystem": "PyPI", "name": "django"},
                              "ranges": [{"events": [{"introduced": "0"}, {"fixed": "3.2.5"}]}]}]}

    def test_osv_outage_is_answered_from_saved_copies_and_noted(self):
        pkg = Package("PyPI", "django", "3.2.0")
        with mock.patch("scanner.sources.TTL_VULN", -1):  # saved, but already expired
            first = query_osv([pkg], fetcher=self._fake)
        started = time.time() - 1
        again = query_osv([pkg], fetcher=mock.Mock(side_effect=OSError("offline")))
        self.assertEqual(again[(pkg.key, pkg.version)][0]["id"], "GHSA-x")
        self.assertEqual(again, first)
        self.assertIn("osv_pkg", stale_since(started))

    def test_osv_outage_with_nothing_saved_still_raises(self):
        with self.assertRaises(OSError):
            query_osv([Package("PyPI", "django", "3.2.0")], fetcher=mock.Mock(side_effect=OSError("offline")))


class NvdBudgetTests(SimpleTestCase):
    """NVD allows 5 requests per rolling 30 s without an API key, 50 with one."""

    def setUp(self):
        _NVD_BUDGET._stamps.clear()

    def test_stays_inside_the_keyless_limit(self):
        with mock.patch("scanner.sources.fetch_json", return_value={}):
            for _ in range(4):  # 5 allowed, one kept in reserve
                _nvd_get("https://nvd.example/x", {}, 5)
            with self.assertRaises(RateLimited):
                _nvd_get("https://nvd.example/x", {}, 5)

    def test_an_api_key_raises_the_limit(self):
        with mock.patch("scanner.sources.fetch_json", return_value={}):
            for _ in range(49):
                _nvd_get("https://nvd.example/x", {"apiKey": "k"}, 5)
            with self.assertRaises(RateLimited):
                _nvd_get("https://nvd.example/x", {"apiKey": "k"}, 5)


class PartialResultsTests(TestCase):
    """A failed source must not hide what the sources that did answer found."""

    def setUp(self):
        clear_cache()

    def _vuln(self):
        return {"id": "CVE-2020-1", "aliases": [], "summary": "found by another source", "severity": "HIGH",
                "fixed": ["3.2.5"], "published": "2020-01-01", "url": "https://x", "sources": ["RetireJS"],
                "exploited": False}

    def _page(self, packages, vulns, news):
        result = build_result(packages, vulns, {"OSV.dev": "unreachable"}, news=news)
        with mock.patch("scanner.views.run_scan", return_value=result):
            return self.client.post("/scan/", {"pasted": "x==1"}, follow=True)

    def test_findings_from_other_sources_are_shown_even_when_one_source_failed(self):
        pkg = Package("PyPI", "django", "3.2.0")
        r = self._page([pkg], {(pkg.key, pkg.version): [self._vuln()]}, [])
        self.assertContains(r, "found by another source")
        self.assertContains(r, "partly checked")
        self.assertContains(r, "OSV.dev could not be reached")
        self.assertNotContains(r, "Not checked · 1")  # listed once, with its findings, not hidden

    def test_news_from_other_sources_is_shown_for_a_package_that_could_not_be_checked(self):
        pkg = Package("PyPI", "flask", "2.0.0")
        news = [{"source": "Krebs on Security", "title": "Flask apps under attack", "url": "https://k/1",
                 "date": "2026-11-18", "matches": ["flask"]}]
        r = self._page([pkg], {}, news)
        self.assertContains(r, "Not checked · 1")
        self.assertContains(r, "Flask apps under attack")
        self.assertContains(r, "Not checked for vulnerabilities")

    def test_saved_data_notice_names_the_source_and_age(self):
        pkg = Package("PyPI", "django", "3.2.0")
        result = build_result([pkg], {}, {"OSV.dev": "ok"}, news=[], stale={"osv_pkg": 5 * 3600})
        with mock.patch("scanner.views.run_scan", return_value=result):
            r = self.client.post("/scan/", {"pasted": "x==1"}, follow=True)
        self.assertContains(r, "OSV.dev is showing saved data")
        self.assertContains(r, "up to 5 hours old")


class FeedParsingTests(SimpleTestCase):
    def test_dates_and_descriptions_are_read_from_childless_elements(self):
        """Regression: `child("pubDate") or child("published")` dropped every date, so no story ever matched."""
        from .news import _load_feed

        with mock.patch("scanner.news.fetch_bytes", return_value=_feed_xml()):
            item = _load_feed("https://example.com/feed")[0]
        self.assertTrue(item["date"])
        atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title><link href="https://a/1"/>'
                b"<updated>2026-11-18T10:00:00Z</updated><summary>about django</summary></entry></feed>")
        with mock.patch("scanner.news.fetch_bytes", return_value=atom):
            item = _load_feed("https://example.com/atom")[0]
        self.assertEqual(item["date"][:10], "2026-11-18")
        self.assertEqual(item["text"], "about django")


class TriageTests(SimpleTestCase):
    """Ranges are filtered to what they can actually install; the rest is grouped by fix."""

    @staticmethod
    def vuln(vid, fixed=(), introduced=(), severity="HIGH", **extra):
        return {
            "id": vid, "aliases": [], "summary": vid, "severity": severity, "fixed": list(fixed),
            "introduced": list(introduced), "published": "", "url": "", "sources": ["OSV.dev"],
            "exploited": False, **extra,
        }

    def test_spec_bounds(self):
        from .triage import spec_bounds
        b = spec_bounds(">=5.2,<7")
        self.assertEqual((b.lower, b.upper, b.upper_inclusive), ("5.2", "7", False))
        b = spec_bounds("^6.2.0")
        self.assertEqual((b.lower, b.upper), ("6.2.0", "7"))
        b = spec_bounds("^0.35.4")
        self.assertEqual((b.lower, b.upper), ("0.35.4", "0.36"))
        b = spec_bounds("~=2.9")
        self.assertEqual((b.lower, b.upper), ("2.9", "3"))
        b = spec_bounds(">= 43")
        self.assertEqual((b.lower, b.upper), ("43", None))
        self.assertFalse(spec_bounds("").known)
        self.assertFalse(spec_bounds("^1.0.0 || ^2.0.0").known)

    def test_could_affect_drops_advisories_outside_the_range(self):
        from .triage import could_affect, spec_bounds
        bounds = spec_bounds(">=5.2,<7")
        self.assertFalse(could_affect(self.vuln("old", fixed=["2.2.4"]), bounds))
        self.assertFalse(could_affect(self.vuln("equal", fixed=["5.2"]), bounds))
        self.assertTrue(could_affect(self.vuln("live", fixed=["5.2.4"]), bounds))
        self.assertTrue(could_affect(self.vuln("unfixed"), bounds))
        self.assertFalse(could_affect(self.vuln("future", introduced=["8.0"], fixed=["8.1"]), bounds))
        self.assertTrue(could_affect(self.vuln("from-start", introduced=["0"], fixed=["6.0.2"]), bounds))

    def test_range_package_hides_impossible_advisories_and_says_so(self):
        pkg = Package("PyPI", "django", None, ">=5.2,<7")
        vulns = {(pkg.key, None): [
            self.vuln("GHSA-old", fixed=["2.2.4"]),
            self.vuln("GHSA-update", fixed=["6.0.2"]),
            self.vuln("GHSA-unfixed"),
            self.vuln("GHSA-beyond", fixed=["7.0.1"]),
        ]}
        result = build_result([pkg], vulns, {"OSV.dev": "ok"}, news=[])
        row = result["packages"][0]
        self.assertEqual(sorted(v["id"] for v in row["vulns"]), ["GHSA-beyond", "GHSA-unfixed"])
        self.assertEqual(row["hidden"], 1)                                    # cannot affect >=5.2
        self.assertEqual([u["cve"] for u in row["updatable"]], ["GHSA-update"])  # a plain update fixes it
        self.assertEqual(result["stats"]["problems"], 2)
        self.assertEqual((result["stats"]["confirmed"], result["stats"]["possible"]), (0, 2))
        self.assertEqual((result["stats"]["hidden"], result["stats"]["updatable"]), (1, 1))

    def test_exploited_findings_are_never_demoted_to_updatable(self):
        pkg = Package("PyPI", "litellm", None, "")
        vulns = {(pkg.key, None): [self.vuln("GHSA-kev", fixed=["1.5"], exploited=True), self.vuln("GHSA-ok", fixed=["1.4"])]}
        row = build_result([pkg], vulns, {"OSV.dev": "ok"}, news=[])["packages"][0]
        self.assertEqual([v["id"] for v in row["vulns"]], ["GHSA-kev"])
        self.assertEqual(len(row["updatable"]), 1)

    def test_exact_package_is_never_filtered(self):
        pkg = Package("PyPI", "django", "3.2.0", "==3.2.0")
        vulns = {(pkg.key, "3.2.0"): [self.vuln("GHSA-old", fixed=["2.2.4"])]}
        result = build_result([pkg], vulns, {"OSV.dev": "ok"}, news=[])
        self.assertEqual(len(result["packages"][0]["vulns"]), 1)
        self.assertEqual(result["stats"]["confirmed"], 1)

    def test_findings_group_by_fix_and_one_upgrade_clears_all(self):
        pkg = Package("npm", "vite", "6.3.5", "6.3.5")
        vulns = {(pkg.key, "6.3.5"): [
            self.vuln("a", fixed=["7.1.5", "8.0.5"], severity="LOW"),
            self.vuln("b", fixed=["8.3.3"], severity="HIGH"),
            self.vuln("c", fixed=["8.3.3"], severity="MODERATE"),
            self.vuln("d", severity="LOW"),
        ]}
        row = build_result([pkg], vulns, {"OSV.dev": "ok"}, news=[])["packages"][0]
        self.assertEqual(row["upgrade_to"], "8.3.3")
        self.assertEqual([(g["to"], g["count"]) for g in row["fix_groups"]], [("8.3.3", 2), ("7.1.5", 1), (None, 1)])
        self.assertEqual(row["fix_groups"][0]["worst"], "HIGH")

    def test_malware_and_exploited_sort_first_and_surface_in_actions(self):
        pkgs = [Package("PyPI", "plain", "1.0", "1.0"), Package("PyPI", "litellm", None, "")]
        vulns = {
            (pkgs[0].key, "1.0"): [self.vuln("GHSA-x", fixed=["2.0"], severity="CRITICAL")],
            (pkgs[1].key, None): [
                self.vuln("GHSA-y", fixed=["1.5"], severity="HIGH", exploited=True),
                self.vuln("MAL-2026-1", severity="UNKNOWN"),
            ],
        }
        result = build_result(pkgs, vulns, {"OSV.dev": "ok"}, news=[])
        self.assertEqual(result["actions"][0]["name"], "litellm")
        self.assertTrue(result["actions"][0]["malware"])
        self.assertEqual(result["packages"][0]["vulns"][0]["id"], "MAL-2026-1")
        self.assertEqual(result["stats"]["malware"], 1)

    def test_retirejs_rows_carry_range_so_ranges_can_be_gated(self):
        pkg = Package("npm", "react", None, "^19.2.0")
        repo = {"react": {"vulnerabilities": [
            {"atOrAbove": "0.0.1", "below": "0.14.0", "severity": "medium", "identifiers": {"CVE": ["CVE-2013-7035"]}}]}}
        rows = query_retirejs([pkg], repo=repo)[(pkg.key, None)]
        self.assertEqual((rows[0]["introduced"], rows[0]["fixed"]), (["0.0.1"], ["0.14.0"]))
        result = build_result([pkg], {(pkg.key, None): rows}, {"RetireJS": "ok", "OSV.dev": "ok"}, news=[])
        self.assertEqual(result["packages"][0]["vulns"], [])
        self.assertEqual(result["packages"][0]["hidden"], 1)

    def test_osv_git_ranges_are_not_treated_as_versions(self):
        from .sources import _osv_fixed
        vuln = {"affected": [{"package": {"ecosystem": "PyPI", "name": "x"}, "ranges": [
            {"type": "GIT", "events": [{"fixed": "deadbeefcafe"}]},
            {"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "1.2"}]}]}]}
        self.assertEqual(_osv_fixed(vuln, "PyPI", "x"), ["1.2"])

    def test_malware_naming_exact_versions_only_counts_when_the_range_allows_them(self):
        axios = Package("npm", "axios", None, "^1.7.2")
        safe = Package("npm", "react-hook-form", None, "^7.82.0")
        mal = lambda vs: self.vuln("MAL-2026-1", severity="UNKNOWN", versions=vs)
        vulns = {(axios.key, None): [mal(["0.30.4", "1.14.1"])], (safe.key, None): [mal(["7.73.0"])]}
        result = build_result([axios, safe], vulns, {"OSV.dev": "ok"}, news=[])
        rows = {r["name"]: r for r in result["packages"]}
        self.assertEqual(rows["axios"]["vulns"][0]["in_range_versions"], ["1.14.1"])
        self.assertTrue(rows["axios"]["malware"])
        self.assertEqual(rows["react-hook-form"]["vulns"], [])
        self.assertEqual(rows["react-hook-form"]["hidden"], 1)


class EvidenceAndNewsSplitTests(TestCase):
    """Every source that backs a finding stays on it; reports and articles are kept apart."""

    def setUp(self):
        clear_cache()

    @staticmethod
    def row(vid, source, severity="HIGH", aliases=(), **extra):
        return {"id": vid, "aliases": list(aliases), "summary": "s", "severity": severity, "fixed": ["2.0"],
                "published": "2024-01-01", "url": f"https://{source}/{vid}", "sources": [source],
                "exploited": False, **extra}

    def test_duplicate_findings_keep_every_source_as_evidence(self):
        from .scan import _merge_package_vulns
        osv = self.row("GHSA-aaaa", "OSV.dev", aliases=["CVE-2024-1"])
        retire = self.row("CVE-2024-1", "RetireJS", severity="MODERATE")
        merged = _merge_package_vulns([osv, retire])
        self.assertEqual(len(merged), 1)
        v = merged[0]
        self.assertEqual(v["corroboration"], 2)
        self.assertEqual({e["source"]: e["id"] for e in v["evidence"]}, {"OSV.dev": "GHSA-aaaa", "RetireJS": "CVE-2024-1"})
        self.assertTrue(v["disputed"])                       # HIGH vs MODERATE
        self.assertEqual(v["severity"], "HIGH")               # the higher rating wins

    def test_merging_twice_does_not_double_count_sources(self):
        from .scan import _merge_package_vulns
        once = _merge_package_vulns([self.row("GHSA-aaaa", "OSV.dev", aliases=["CVE-2024-1"]), self.row("CVE-2024-1", "RetireJS")])
        twice = _merge_package_vulns(once)
        self.assertEqual(twice[0]["corroboration"], 2)
        self.assertEqual(len(twice[0]["evidence"]), 2)

    def test_kev_flag_counts_as_a_source_with_no_severity_of_its_own(self):
        from .scan import _merge_package_vulns
        row = self.row("GHSA-aaaa", "OSV.dev", aliases=["CVE-2024-1"])
        row["sources"].append("CISA KEV")
        row["exploited"] = True
        v = _merge_package_vulns([row])[0]
        kev = next(e for e in v["evidence"] if e["source"] == "CISA KEV")
        self.assertEqual((kev["id"], kev["severity"]), ("CVE-2024-1", None))
        self.assertEqual(v["corroboration"], 2)
        self.assertFalse(v["disputed"])                       # KEV rates nothing, so it cannot disagree

    def test_stats_count_corroborated_and_single_source(self):
        pkg = Package("npm", "react", "18.0.0", "18.0.0")
        vulns = {(pkg.key, "18.0.0"): [
            self.row("GHSA-aaaa", "OSV.dev", aliases=["CVE-2024-1"]), self.row("CVE-2024-1", "RetireJS", severity="MODERATE"),
            self.row("GHSA-bbbb", "OSV.dev", aliases=["CVE-2024-2"]),
        ]}
        stats = build_result([pkg], vulns, {"OSV.dev": "ok"}, news=[])["stats"]
        self.assertEqual((stats["corroborated"], stats["single_source"], stats["disputed"]), (1, 1, 1))

    def test_news_is_split_into_reports_and_articles_cve_matches_first(self):
        news = [
            {"source": "BleepingComputer", "title": "a", "url": "u1", "date": "2026-11-01", "matches": ["flask"]},
            {"source": "CISA Advisories", "title": "b", "url": "u2", "date": "2026-11-02", "matches": ["flask"]},
            {"source": "Krebs on Security", "title": "c", "url": "u3", "date": "2026-11-03", "matches": ["CVE-2024-1"], "matched_by": "cve"},
            {"source": "Hacker News", "title": "d", "url": "u4", "date": "2026-11-04", "matches": ["flask"]},
        ]
        r = build_result([Package("PyPI", "flask", "3.0", "3.0")], {}, {"OSV.dev": "ok"}, news=news)
        self.assertEqual([n["source"] for n in r["reports"]], ["CISA Advisories"])
        self.assertEqual([n["source"] for n in r["articles"]], ["Krebs on Security", "BleepingComputer", "Hacker News"])
        self.assertEqual((r["stats"]["reports"], r["stats"]["articles"], r["stats"]["news"]), (1, 3, 4))

    def test_matcher_tags_kind_and_how_it_matched(self):
        stories = [
            {"source": "CISA Advisories", "title": "Flask flaw CVE-2024-99999", "url": "u1", "date": "2026-11-01T00:00:00+00:00", "text": ""},
            {"source": "Krebs on Security", "title": "flask apps attacked", "url": "u2", "date": "2026-11-01T00:00:00+00:00", "text": ""},
            {"source": "Krebs on Security", "title": "which one is it, three ways", "url": "u3", "date": "2026-11-01T00:00:00+00:00", "text": ""},
        ]
        pkgs = [Package("PyPI", "flask", "3.0"), Package("npm", "which", "1.0"), Package("npm", "three", "0.1")]
        with mock.patch("scanner.news.fetch_all_rss_stories", return_value=(stories, True)):
            matched, _ = match_news(pkgs, {"CVE-2024-99999"}, include_hn=False)
        by = {m["url"]: m for m in matched}
        self.assertEqual((by["u1"]["kind"], by["u1"]["matched_by"]), ("report", "cve"))
        self.assertEqual((by["u2"]["kind"], by["u2"]["matched_by"]), ("article", "name"))
        self.assertNotIn("u3", by)                            # "which" and "three" are common words

    def test_page_shows_sources_and_separate_sections(self):
        pkg = Package("npm", "react", "18.0.0", "18.0.0")
        vulns = {(pkg.key, "18.0.0"): [self.row("GHSA-aaaa", "OSV.dev", aliases=["CVE-2024-1"]), self.row("CVE-2024-1", "RetireJS", severity="MODERATE")]}
        news = [{"source": "CISA Advisories", "title": "Report title", "url": "https://r/1", "date": "2026-11-02", "matches": ["react"]},
                {"source": "BleepingComputer", "title": "Article title", "url": "https://a/1", "date": "2026-11-03", "matches": ["react"]}]
        result = build_result([pkg], vulns, {"OSV.dev": "ok", "RetireJS": "ok"}, news=news)
        with mock.patch("scanner.views.run_scan", return_value=result):
            r = self.client.post("/scan/", {"pasted": "x==1"}, follow=True)
        self.assertContains(r, "Backed by 2 sources")
        self.assertContains(r, "ratings disagree")
        self.assertContains(r, 'id="reports"')
        self.assertContains(r, 'id="articles"')
        html = r.content.decode()
        reports_at, articles_at = html.index('id="reports"'), html.index('id="articles"')
        self.assertLess(reports_at, html.index("Report title", reports_at))
        self.assertLess(html.index("Report title", reports_at), articles_at)      # report sits in its own section
        self.assertLess(articles_at, html.index("Article title", articles_at))
        self.assertNotIn("Article title", html[reports_at:articles_at])


class HomePageTests(TestCase):
    """The landing page: what Sentinel does, the latest articles, reports and a headline ticker."""

    def setUp(self):
        clear_cache()

    @staticmethod
    def save_feed(name, items):
        from .http_client import cache_put
        cache_put("rss_feed", name, items, 3600)

    def story(self, title, hours_ago, url=None, text="Body text."):
        when = (timezone.now() - timedelta(hours=hours_ago)).isoformat()
        return {"source": "", "title": title, "url": url or f"https://news.example/{abs(hash(title))}", "text": text, "date": when}

    def test_home_renders_carousel_articles_reports_and_ticker_from_saved_feeds(self):
        self.save_feed("The Hacker News", [self.story("Newest article", 1), self.story("Older article", 30)])
        self.save_feed("BleepingComputer", [self.story("Middle article", 5)])
        self.save_feed("CISA Advisories", [self.story("CISA releases advisory", 2)])
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        page = r.content.decode()
        self.assertIn('aria-roledescription="carousel"', page)
        self.assertEqual(page.count('aria-roledescription="slide"'), 6)
        self.assertIn('class="iz-card lead-story"', page)
        lead = page.index('class="iz-card lead-story"')
        self.assertLess(lead, page.index("Newest article"))                    # newest article leads
        self.assertLess(page.index("Newest article"), page.index("Middle article"))
        self.assertLess(page.index("Middle article"), page.index("Older article"))
        rail = page.index('id="reports-title"')
        self.assertLess(rail, page.index("CISA releases advisory", rail))      # reports sit in their own rail
        self.assertNotIn("CISA releases advisory", page[lead:rail])
        self.assertIn('aria-label="Latest headlines"', page)
        self.assertIn("home.css", page)
        self.assertIn("4 stories from", page)

    def test_duplicate_stories_and_unsafe_links_are_dropped(self):
        self.save_feed("The Hacker News", [self.story("Same headline", 1, url="https://a/1"), self.story("javascript link", 1, url="javascript:alert(1)")])
        self.save_feed("SecurityWeek", [self.story("Same headline!", 2, url="https://b/2"), self.story("Repeat url", 3, url="https://a/1")])
        from .headlines import latest
        h = latest()
        self.assertEqual(h["total"], 1)
        self.assertEqual(h["lead"]["title"], "Same headline")

    def test_home_never_waits_on_the_network_and_explains_an_empty_feed(self):
        with mock.patch("scanner.news.fetch_bytes", side_effect=AssertionError("network used")):
            r = self.client.get("/")
        self.assertContains(r, "No headlines available right now.")
        self.assertContains(r, "Scan dependencies")                           # the carousel still sells the scanner

    def test_stale_feeds_are_refreshed_in_the_background_when_enabled(self):
        self.save_feed("The Hacker News", [self.story("Saved article", 1)])
        from . import headlines
        started = []
        with override_settings(SENTINEL_HOME_LIVE_REFRESH=True),                 mock.patch("scanner.headlines.threading.Thread") as thread:
            thread.return_value.start.side_effect = lambda: started.append(True)
            headlines._refreshing.clear()
            h = headlines.latest()
            headlines._refreshing.clear()
        self.assertEqual(h["lead"]["title"], "Saved article")                 # shown at once from the saved copy
        self.assertTrue(h["updating"])
        self.assertEqual(started, [True])

    def test_excerpts_are_trimmed_on_a_word(self):
        from .headlines import _excerpt
        self.assertEqual(_excerpt("one two three four", limit=10), "one two…")
        self.assertEqual(_excerpt("  short   text "), "short text")

    def test_scan_form_moved_to_scan_and_still_posts_there(self):
        page = self.client.get("/scan/").content.decode()
        self.assertIn('id="scan-form"', page)
        self.assertIn('action="/scan/"', page)
        self.assertIn('href="/" aria-label="Sentinel home"', page)
