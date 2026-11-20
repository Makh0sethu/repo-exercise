import json
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from .news import match_news
from .parsers import Package, parse_inputs, parse_other, parse_requirements
from .scan import build_result
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


class ViewTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def _patch_secondary_sources(self):
        """Context manager stack mocking secondary sources offline-safely."""
        return [
            mock.patch("scanner.scan.fetch_cisa_kev", return_value=set()),
            mock.patch("scanner.scan.query_retirejs", return_value={}),
            mock.patch("scanner.scan.query_nvd_other", return_value={}),
            mock.patch("scanner.scan.match_news", return_value=([], "ok")),
        ]

    def test_scan_page_and_security_headers(self):
        r = self.client.get("/")
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
            self.assertEqual(self.client.post("/scan/", {"pasted": "django==4.2.1"}).status_code, 200)
            self.assertEqual(self.client.post("/scan/", {"pasted": "django==4.2.1"}).status_code, 200)
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
            r = self.client.post("/scan/", {"pasted": "django==4.2.1"})
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
            r = self.client.post("/scan/", {"pasted": "numpy==2.1.3", "other": "postgresql 15.4\ncustomdb 1.0"})
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
            r = self.client.post("/scan/", {"pasted": "django==4.2.1"})
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
            r = self.client.post("/scan/", {"pasted": "requests==2.32.0"})
        self.assertContains(r, "scanner/logos/python.png")
