import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

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


class ViewTests(SimpleTestCase):
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
            r = self.client.post("/scan/", {"pasted": "django==4.2.1"})
        self.assertContains(r, "OSV.dev could not be reached")
        self.assertContains(r, "Not checked · 1")
        self.assertNotContains(r, "✓ clean")

    def test_other_software_is_not_called_clean(self):
        def fake(url, body=None, timeout=20):
            return {"results": [{} for _ in body["queries"]]}
        with mock.patch("scanner.scan._get_json", side_effect=fake):
            r = self.client.post("/scan/", {"pasted": "numpy==2.1.3", "other": "postgresql 15.4"})
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
            r = self.client.post("/scan/", {"pasted": "django==4.2.1"})
        self.assertContains(r, "CVE-2023-36053")
        self.assertContains(r, "4.2.3")
        self.assertContains(r, "scanner/logos/django.png")
        self.assertContains(r, 'id="scan-data"')

    def test_package_without_logo_gets_ecosystem_logo(self):
        def fake(url, body=None, timeout=20):
            return {"results": [{} for _ in body["queries"]]}
        with mock.patch("scanner.scan._get_json", side_effect=fake):
            r = self.client.post("/scan/", {"pasted": "requests==2.32.0"})
        self.assertContains(r, "scanner/logos/python.png")
