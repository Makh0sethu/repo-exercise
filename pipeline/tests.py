import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from audit.log import read_evidence, verify_chain
from audit.models import AuditEvent, EvidenceArtifact

from .gitsafe import UnsafeTarget, check_git_url, check_ref
from .models import Finding, Target
from .runner import create_run, execute


def make_repo(root: Path) -> str:
    """A real local git repo, so the clone step runs real git."""
    root.mkdir(parents=True)
    (root / "requirements.txt").write_text("django==4.2.1\nrequests\n")
    (root / "package.json").write_text('{"dependencies": {"lodash": "4.17.15"}}')
    (root / "node_modules").mkdir()
    (root / "node_modules" / "package.json").write_text('{"dependencies": {"ignored": "1.0.0"}}')
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"]}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A", "-f"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True, env=env)
    return str(root)


TWO_STEP = [
    {"tool": "git_clone", "required": True, "reason": "Fetch the exact source to test and pin the commit"},
    {"tool": "dependency_scan", "reason": "Look up every dependency on OSV.dev for known vulnerabilities"},
]


def fake_osv(url, body=None, timeout=20):
    if "querybatch" in url:
        return {"results": [{"vulns": [{"id": "GHSA-x"}]} if q["package"]["name"] == "lodash" else {} for q in body["queries"]]}
    return {"id": "GHSA-x", "aliases": ["CVE-2021-23337"], "summary": "Command injection in template",
            "database_specific": {"severity": "HIGH"},
            "affected": [{"package": {"ecosystem": "npm", "name": "lodash"}, "ranges": [{"events": [{"fixed": "4.17.21"}]}]}]}


class GitSafetyTests(TestCase):
    def test_only_https_on_allowed_hosts(self):
        self.assertEqual(check_git_url("https://github.com/Makh0sethu/repo-exercise"), "https://github.com/Makh0sethu/repo-exercise")
        for bad in ["http://github.com/a/b", "git@github.com:a/b.git", "ssh://github.com/a/b", "file:///etc",
                    "https://evil.example/a/b", "https://user:pw@github.com/a/b", "ext::sh -c id"]:
            with self.assertRaises(UnsafeTarget, msg=bad):
                check_git_url(bad)

    def test_refs_cannot_inject_options(self):
        for ok in ["HEAD", "main", "release/1.2", "v1.0.0"]:
            check_ref(ok)
        for bad in ["--upload-pack=evil", "-x", "a..b", "a b", "$(id)", ""]:
            with self.assertRaises(UnsafeTarget, msg=bad):
                check_ref(bad)


@override_settings(PIPELINE_WORKDIR=tempfile.gettempdir())
class PipelineTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ev = override_settings(AUDIT_EVIDENCE_DIR=self.tmp.name + "/ev"); self.ev.enable(); self.addCleanup(self.ev.disable)
        self.repo = make_repo(Path(self.tmp.name) / "repo")
        self.target = Target.objects.create(name="demo", git_url="https://github.com/o/demo")
        # Point the real clone step at the local repo; URL validation has its own tests above.
        p = mock.patch("pipeline.adapters.check_git_url", return_value=self.repo); p.start(); self.addCleanup(p.stop)

    def run_it(self):
        with mock.patch("scanner.scan._get_json", side_effect=fake_osv):
            return execute(create_run(self.target, requested_by="alice", pipeline=TWO_STEP))

    def test_full_run_clones_pins_commit_and_finds_vulnerabilities(self):
        run = self.run_it()
        self.assertEqual(run.status, "passed")
        self.assertEqual(len(run.commit_sha), 40)
        f = Finding.objects.get(severity="high")
        self.assertEqual(f.location, "lodash@4.17.15")
        self.assertIn("4.17.21", f.detail["fixed"])

    def test_unpinned_packages_are_flagged_not_scored(self):
        """A bare name or version range can't be matched to advisories, so it is one 'unrated'
        finding saying so, never a list of vulnerabilities."""
        self.run_it()
        u = Finding.objects.get(severity="unrated")
        self.assertEqual(u.location, "requests@*")
        self.assertIn("version not pinned", u.title)
        self.assertEqual(Finding.objects.filter(severity="high").count(), 1)   # only the pinned lodash

    def test_node_modules_are_not_scanned(self):
        self.run_it()
        self.assertNotIn("ignored", read_evidence(EvidenceArtifact.objects.get(sha256=Finding.objects.get(severity="high").evidence_sha256)).decode())

    def test_every_step_is_on_the_audit_chain_with_its_reason(self):
        run = self.run_it()
        actions = list(AuditEvent.objects.values_list("action", flat=True))
        self.assertEqual(actions, ["run.created", "step.started", "step.finished", "step.started",
                                   "finding.recorded", "finding.recorded", "step.finished", "run.finished"])
        started = AuditEvent.objects.filter(action="step.started").first()
        self.assertIn("pin the commit", started.payload["reason"])
        clone = AuditEvent.objects.filter(action="step.finished").first()
        self.assertEqual(clone.payload["argv"][:2], ["git", "clone"])      # the exact command is on record
        self.assertEqual(AuditEvent.objects.get(action="run.created").actor, "alice")
        self.assertTrue(verify_chain()[0])

    def test_raw_output_is_kept_as_evidence_and_checkout_is_deleted(self):
        run = self.run_it()
        step = run.steps.get(tool="dependency_scan")
        self.assertIn("lodash@4.17.15", read_evidence(EvidenceArtifact.objects.get(sha256=step.stdout_sha256)).decode())
        self.assertFalse((Path(tempfile.gettempdir()) / f"sentinel-run-{run.pk}").exists())

    def test_failed_clone_stops_the_pipeline_and_is_recorded(self):
        with mock.patch("pipeline.adapters.check_git_url", return_value=self.tmp.name + "/does-not-exist"):
            run = self.run_it()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.steps.count(), 1)                 # dependency step never ran
        self.assertEqual(AuditEvent.objects.last().payload["status"], "failed")

    def test_unsafe_target_is_refused_and_recorded(self):
        self.target.git_url = "ssh://github.com/o/demo"
        self.target.save()
        p = mock.patch("pipeline.adapters.check_git_url", side_effect=UnsafeTarget("Only https")); p.start(); self.addCleanup(p.stop)
        run = self.run_it()
        self.assertEqual(run.status, "failed")
        self.assertIn("Refused", read_evidence(EvidenceArtifact.objects.get(sha256=run.steps.get().stderr_sha256)).decode())

    def test_osv_outage_fails_the_step_instead_of_reporting_clean(self):
        with mock.patch("scanner.scan._get_json", side_effect=OSError("offline")):
            run = execute(create_run(self.target, pipeline=TWO_STEP))
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.findings.count(), 0)
        self.assertEqual(run.steps.get(tool="dependency_scan").status, "failed")

    def test_unknown_tool_in_pipeline_is_rejected(self):
        with self.assertRaises(ValueError):
            create_run(self.target, pipeline=[{"tool": "rm_rf", "reason": "x"}])


class SecretHandlingTests(TestCase):
    def test_github_token_never_reaches_argv_audit_or_evidence(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        ev = override_settings(AUDIT_EVIDENCE_DIR=tmp.name); ev.enable(); self.addCleanup(ev.disable)
        target = Target.objects.create(name="priv", git_url="https://github.com/o/private", private=True)
        secret = "ghp_SUPERSECRETTOKEN123"
        seen = {}

        def fake_run(argv, **kw):
            if argv[:2] == ["git", "clone"]:
                seen["env"] = kw["env"]
                return subprocess.CompletedProcess(argv, 128, "", f"fatal: auth failed for {secret}")
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.dict("os.environ", {"SENTINEL_GITHUB_TOKEN": secret}), \
             mock.patch("pipeline.adapters.subprocess.run", side_effect=fake_run):
            run = execute(create_run(target, pipeline=TWO_STEP))
        self.assertIn("GIT_CONFIG_VALUE_0", seen["env"])           # delivered via env...
        everything = str(list(AuditEvent.objects.values_list("payload", flat=True))) + str(list(run.steps.values_list("argv", flat=True)))
        for art in EvidenceArtifact.objects.all():
            everything += (Path(tmp.name) / art.sha256).read_text()
        self.assertNotIn(secret, everything)                       # ...and never recorded anywhere
        self.assertIn("***", everything)

    def test_private_target_without_credentials_fails_cleanly(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        ev = override_settings(AUDIT_EVIDENCE_DIR=tmp.name); ev.enable(); self.addCleanup(ev.disable)
        target = Target.objects.create(name="priv2", git_url="https://github.com/o/private", private=True)
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("SENTINEL_GITHUB_TOKEN", None)
            run = execute(create_run(target, pipeline=TWO_STEP))
        self.assertEqual(run.status, "failed")


# ---------------------------------------------------------------- scanners
import json as _json
import os

from .adapters import ToolMissing, find_tool, npm_audit, pip_audit, sast_python, secrets_scan


def tool_available(name):
    try:
        find_tool(name)
        return True
    except ToolMissing:
        return False


def ctx_for(src, ws):
    return {"workspace": Path(ws), "values": {"src": str(src)}, "target": None}


def planted_token():
    return "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"[:36]     # built at runtime: no literal secret in this file


class ScannerBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "src"; self.src.mkdir()
        self.ws = Path(self.tmp.name) / "ws"; self.ws.mkdir()
        self.dirs = [d for d in os.environ.get("SENTINEL_TOOL_DIRS", "").split(os.pathsep) if d]
        o = override_settings(PIPELINE_TOOL_DIRS=self.dirs); o.enable(); self.addCleanup(o.disable)


@unittest.skipUnless(tool_available("gitleaks"), "gitleaks not installed")
class SecretsScanTests(ScannerBase):
    def test_finds_a_planted_secret_and_never_keeps_its_value(self):
        token = planted_token()
        (self.src / "config.py").write_text(f'GITHUB_TOKEN = "{token}"\n')
        for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", str(self.src), *cmd], check=True)
        r = secrets_scan({}, ctx_for(self.src, self.ws))
        self.assertTrue(r.ok)
        self.assertEqual(len(r.findings), 1)
        self.assertEqual(r.findings[0]["location"], "config.py:1")
        self.assertEqual(r.findings[0]["severity"], "high")
        self.assertNotIn(token, _json.dumps([r.findings, r.summary]) + r.stdout + r.stderr)   # value never stored
        self.assertEqual(len(r.summary["binary_sha256"]), 64)                                 # which binary ran is recorded

    def test_clean_repo_reports_no_secrets(self):
        (self.src / "a.py").write_text("print('hello')\n")
        for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", str(self.src), *cmd], check=True)
        r = secrets_scan({}, ctx_for(self.src, self.ws))
        self.assertTrue(r.ok); self.assertEqual(r.findings, [])


@unittest.skipUnless(tool_available("bandit"), "bandit not installed")
class SastTests(ScannerBase):
    def test_finds_insecure_code_without_executing_it_or_keeping_the_source_line(self):
        marker = self.tmp.name + "/EXECUTED"
        (self.src / "app.py").write_text(
            f'import subprocess\nopen({marker!r}, "w")   # would exist if the scanner ran this code\n'
            'password = "hunter2hunter2"\nsubprocess.call(input(), shell=True)\n')
        r = sast_python({}, ctx_for(self.src, self.ws))
        self.assertTrue(r.ok)
        ids = {f["detail"]["test_id"] for f in r.findings}
        self.assertTrue({"B602", "B105"} <= ids, ids)
        self.assertFalse(os.path.exists(marker))                                  # parsed, never executed
        self.assertNotIn("hunter2hunter2", _json.dumps(r.findings) + r.stdout)    # source line (and its secret) not kept
        self.assertIn("app.py:", r.findings[0]["location"])

    def test_node_modules_and_git_are_skipped(self):
        (self.src / "node_modules").mkdir(); (self.src / "node_modules" / "x.py").write_text("eval(input())\n")
        r = sast_python({}, ctx_for(self.src, self.ws))
        self.assertEqual(r.findings, [])


class MissingToolTests(ScannerBase):
    def test_a_missing_scanner_fails_the_step_and_is_never_clean(self):
        with override_settings(PIPELINE_TOOL_DIRS=[]), mock.patch("pipeline.adapters.shutil.which", return_value=None):
            for adapter in (secrets_scan, sast_python, pip_audit, npm_audit):
                r = adapter({}, ctx_for(self.src, self.ws))
                self.assertFalse(r.ok, adapter.__name__)
                self.assertEqual(r.summary["error"], "tool missing")
                self.assertEqual(r.findings, [])


class AuditToolTests(ScannerBase):
    def fake_tool(self, stdout, code=1):
        return mock.patch("pipeline.adapters.subprocess.run", return_value=subprocess.CompletedProcess([], code, stdout, ""))

    def test_pip_audit_turns_advisories_into_findings(self):
        (self.src / "requirements.txt").write_text("requests==2.19.0\n")
        out = _json.dumps({"dependencies": [{"name": "requests", "version": "2.19.0", "vulns": [
            {"id": "PYSEC-2018-28", "aliases": ["CVE-2018-18074"], "fix_versions": ["2.20.0"], "description": "leaks Authorization"}]}]})
        with mock.patch("pipeline.adapters.find_tool", return_value="/bin/true"), \
             mock.patch("pipeline.adapters.tool_info", return_value={"binary_sha256": "0" * 64, "reported_version": "x"}), self.fake_tool(out):
            r = pip_audit({}, ctx_for(self.src, self.ws))
        self.assertTrue(r.ok)
        self.assertEqual((r.findings[0]["severity"], r.findings[0]["detail"]["fixed"]), ("unrated", ["2.20.0"]))
        self.assertIn("CVE-2018-18074", r.findings[0]["title"])

    def test_pip_audit_that_could_not_read_anything_is_a_failure_not_clean(self):
        (self.src / "requirements.txt").write_text("requests\n")        # unpinned: pip-audit refuses it
        with mock.patch("pipeline.adapters.find_tool", return_value="/bin/true"), \
             mock.patch("pipeline.adapters.tool_info", return_value={}), self.fake_tool("", 1):
            r = pip_audit({}, ctx_for(self.src, self.ws))
        self.assertFalse(r.ok)

    def test_npm_audit_maps_severity_and_fix(self):
        (self.src / "package-lock.json").write_text("{}")
        out = _json.dumps({"vulnerabilities": {"lodash": {"severity": "high", "isDirect": True, "range": "<4.17.21",
                           "via": [{"title": "Command Injection in lodash", "url": "https://github.com/advisories/GHSA-35jh"}],
                           "fixAvailable": {"name": "lodash", "version": "4.17.21"}}}})
        with mock.patch("pipeline.adapters.find_tool", return_value="/bin/true"), \
             mock.patch("pipeline.adapters.tool_info", return_value={}), self.fake_tool(out):
            r = npm_audit({}, ctx_for(self.src, self.ws))
        self.assertTrue(r.ok)
        self.assertEqual((r.findings[0]["severity"], r.findings[0]["detail"]["fix"]["version"]), ("high", "4.17.21"))

    def test_npm_audit_registry_error_is_a_failure_not_clean(self):
        (self.src / "package-lock.json").write_text("{}")
        out = _json.dumps({"error": {"code": "ENOTFOUND", "summary": "registry unreachable"}})
        with mock.patch("pipeline.adapters.find_tool", return_value="/bin/true"), \
             mock.patch("pipeline.adapters.tool_info", return_value={}), self.fake_tool(out):
            r = npm_audit({}, ctx_for(self.src, self.ws))
        self.assertFalse(r.ok)


class PipelineFlowTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        o = override_settings(AUDIT_EVIDENCE_DIR=self.tmp.name); o.enable(); self.addCleanup(o.disable)
        self.repo = make_repo(Path(self.tmp.name) / "repo")
        self.target = Target.objects.create(name="t", git_url="https://github.com/o/t")
        p = mock.patch("pipeline.adapters.check_git_url", return_value=self.repo); p.start(); self.addCleanup(p.stop)

    def test_one_failing_scanner_does_not_stop_the_others(self):
        steps = [{"tool": "git_clone", "required": True, "reason": "r"}, {"tool": "sast_python", "reason": "r"},
                 {"tool": "dependency_scan", "reason": "r"}]
        with mock.patch("pipeline.adapters.find_tool", side_effect=ToolMissing("bandit not installed")), \
             mock.patch("scanner.scan._get_json", side_effect=fake_osv):
            run = execute(create_run(self.target, pipeline=steps))
        self.assertEqual(run.status, "failed")                                   # the run is not green...
        self.assertEqual(list(run.steps.values_list("status", flat=True)), ["passed", "failed", "passed"])  # ...but the rest ran

    def test_default_pipeline_lists_every_scanner_with_a_reason(self):
        from .runner import DEFAULT_PIPELINE
        self.assertEqual([s["tool"] for s in DEFAULT_PIPELINE],
                         ["git_clone", "secrets_scan", "sast_python", "bug_checks", "pip_audit", "npm_audit", "dependency_scan"])
        self.assertTrue(all(s["reason"] for s in DEFAULT_PIPELINE))


class EnvIsolationTests(TestCase):
    def test_only_network_tools_get_proxy_settings_and_no_credentials(self):
        from .adapters import _scrub_env
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy:3128", "SSL_CERT_FILE": "/ca.pem",
                                          "SENTINEL_GITHUB_TOKEN": "tok", "AWS_SECRET_ACCESS_KEY": "k", "DJANGO_SECRET_KEY": "s"}, clear=True):
            offline = _scrub_env({"workspace": Path("/w")})
            ctx = {"workspace": Path("/w")}
            net = _scrub_env(ctx, network=True)
        self.assertNotIn("HTTPS_PROXY", offline)
        self.assertEqual(net["HTTPS_PROXY"], "http://proxy:3128")
        for secret in ("SENTINEL_GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "DJANGO_SECRET_KEY"):
            self.assertNotIn(secret, offline); self.assertNotIn(secret, net)
        # names only, never values; Windows env names are case-insensitive so https_proxy is the same variable
        self.assertEqual(sorted({n.upper() for n in ctx["network_env_passed"]}), ["HTTPS_PROXY", "SSL_CERT_FILE"])


# ------------------------------------------------------------------ bug checks, local targets, offline mode
import json
import zipfile

from pipeline import bugchecks, osvlocal
from pipeline.adapters import ADAPTERS


class BugCheckTests(TestCase):
    def codes(self, src):
        return [c for _, c in bugchecks.check_source(src)]

    def test_each_rule_fires_on_the_bug_it_names(self):
        cases = {
            "BC001": "try:\n    pass\nexcept:\n    x = 1\n",
            "BC002": "def f(a=[]):\n    return a\n",
            "BC003": "x = 1\nif x == None:\n    pass\n",
            "BC004": "assert (1, 'always true')\n",
            "BC005": "x = 'a'\nif x is 'a':\n    pass\n",
            "BC006": "try:\n    x = 1\nexcept ValueError:\n    pass\n",
            "BC007": "eval('1+1')\n",
            "BC008": "DEBUG = True\n",
            "BC009": "SECRET_KEY = 'a-long-hardcoded-value'\n",
            "BC010": "def f():\n    return 1\n    x = 2\n",
            "BC011": "fs = []\nfor i in range(3):\n    fs.append(lambda: i)\n",
            "BC000": "def broken(:\n",
        }
        for code, src in cases.items():
            self.assertIn(code, self.codes(src), msg=code)

    def test_clean_code_has_no_findings(self):
        self.assertEqual(self.codes("def f(a=None):\n    if a is None:\n        return 1\n    return 2\n"), [])

    def test_pipeline_step_reports_file_and_line(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "m.py").write_text("def f(a=[]):\n    return a\n")
            res = ADAPTERS["bug_checks"]({}, {"values": {"src": d}})
        self.assertEqual((res.findings[0]["location"], res.findings[0]["detail"]["rule"]), ("m.py:1", "BC002"))


class LocalTargetTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_refused_unless_a_root_is_configured(self):
        with override_settings(PIPELINE_LOCAL_ROOTS=[]), self.assertRaises(UnsafeTarget):
            check_git_url(str(self.root))

    def test_inside_root_ok_outside_and_escapes_refused(self):
        (self.root / "app").mkdir()
        with override_settings(PIPELINE_LOCAL_ROOTS=[str(self.root)]):
            self.assertEqual(check_git_url(str(self.root / "app")), str(self.root / "app"))
            with self.assertRaises(UnsafeTarget):
                check_git_url(str(self.root / "app" / ".." / ".."))
            with self.assertRaises(UnsafeTarget):
                check_git_url(str(self.root / "missing"))

    def test_local_folder_is_scanned_offline_and_not_modified(self):
        (self.root / "proj").mkdir()
        (self.root / "proj" / "m.py").write_text("def f(a=[]):\n    return a\n")
        (self.root / "proj" / "node_modules").mkdir()
        (self.root / "proj" / "node_modules" / "x.py").write_text("eval('1')\n")
        target = Target.objects.create(name="local", git_url=str(self.root / "proj"))
        pl = [{"tool": "git_clone", "required": True, "reason": "copy"}, {"tool": "bug_checks", "reason": "bugs"}]
        with override_settings(PIPELINE_LOCAL_ROOTS=[str(self.root)], PIPELINE_WORKDIR=self.tmp.name, AUDIT_EVIDENCE_DIR=self.tmp.name + "/ev"):
            run = execute(create_run(target, pipeline=pl))
        self.assertEqual(run.status, "passed")
        self.assertEqual([f.location for f in run.findings.all()], ["m.py:1"])          # node_modules ignored
        self.assertTrue((self.root / "proj" / "node_modules" / "x.py").exists())          # original untouched
        self.assertFalse((self.root / f"sentinel-run-{run.pk}").exists())


def _osv_zip(path):
    adv = {"id": "GHSA-lodash", "summary": "Command injection", "database_specific": {"severity": "HIGH"},
           "affected": [{"package": {"ecosystem": "npm", "name": "lodash"},
                         "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.17.21"}]}]}]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("GHSA-lodash.json", json.dumps(adv))
        z.writestr("GHSA-withdrawn.json", json.dumps({**adv, "id": "GHSA-withdrawn", "withdrawn": "2020-01-01T00:00:00Z"}))


class OfflineTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.o = override_settings(SENTINEL_OSV_DIR=Path(self.tmp.name) / "osv", SENTINEL_OFFLINE=True)
        self.o.enable(); self.addCleanup(self.o.disable)
        (Path(self.tmp.name) / "osv").mkdir()
        _osv_zip(Path(self.tmp.name) / "osv" / "npm.zip")
        self.n = osvlocal.build_index("npm", Path(self.tmp.name) / "osv" / "npm.zip")
        (Path(self.tmp.name) / "osv" / "meta.json").write_text(json.dumps({"updated_at": "2026-11-01T00:00:00+00:00", "counts": {"npm": self.n}}))

    def pkg(self, version):
        from scanner.parsers import Package
        return Package("npm", "lodash", version, version or "", "package.json")

    def test_withdrawn_advisories_are_dropped(self):
        self.assertEqual(self.n, 1)

    def test_exact_version_matched_by_range(self):
        hit = list(osvlocal.query([self.pkg("4.17.15")]).values())[0]
        miss = list(osvlocal.query([self.pkg("4.17.21")]).values())[0]
        self.assertEqual([h["id"] for h in hit], ["GHSA-lodash"])
        self.assertEqual(miss, [])

    def test_dependency_scan_offline_makes_no_network_call_and_states_data_date(self):
        src = Path(self.tmp.name) / "src"; src.mkdir()
        (src / "package.json").write_text('{"dependencies": {"lodash": "4.17.15"}}')
        with mock.patch("scanner.scan._get_json", side_effect=AssertionError("network used")), \
             mock.patch("pipeline.adapters.query_osv", side_effect=AssertionError("network used")):
            res = ADAPTERS["dependency_scan"]({}, {"values": {"src": str(src)}})
        self.assertEqual(res.exit_code, 0)
        self.assertIn("as of 2026-11-01", res.stdout)
        self.assertEqual([f["severity"] for f in res.findings], ["high"])

    def test_no_database_fails_loudly_instead_of_reporting_clean(self):
        with override_settings(SENTINEL_OSV_DIR=Path(self.tmp.name) / "empty"):
            src = Path(self.tmp.name) / "src2"; src.mkdir()
            (src / "package.json").write_text('{"dependencies": {"lodash": "4.17.15"}}')
            res = ADAPTERS["dependency_scan"]({}, {"values": {"src": str(src)}})
        self.assertNotEqual(res.exit_code, 0)
        self.assertIn("update_osv", res.stderr)

    def test_network_steps_are_recorded_as_skipped_not_passed(self):
        for tool in ("pip_audit", "npm_audit"):
            res = ADAPTERS[tool]({}, {"values": {"src": self.tmp.name}, "workspace": Path(self.tmp.name)})
            self.assertEqual(res.summary, {"skipped": "offline"})
