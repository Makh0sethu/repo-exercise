import subprocess
import tempfile
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
            return execute(create_run(self.target, requested_by="alice"))

    def test_full_run_clones_pins_commit_and_finds_vulnerabilities(self):
        run = self.run_it()
        self.assertEqual(run.status, "passed")
        self.assertEqual(len(run.commit_sha), 40)
        f = Finding.objects.get()
        self.assertEqual((f.severity, f.location), ("high", "lodash@4.17.15"))
        self.assertIn("4.17.21", f.detail["fixed"])

    def test_node_modules_are_not_scanned(self):
        self.run_it()
        self.assertNotIn("ignored", read_evidence(EvidenceArtifact.objects.get(sha256=Finding.objects.get().evidence_sha256)).decode())

    def test_every_step_is_on_the_audit_chain_with_its_reason(self):
        run = self.run_it()
        actions = list(AuditEvent.objects.values_list("action", flat=True))
        self.assertEqual(actions, ["run.created", "step.started", "step.finished", "step.started",
                                   "finding.recorded", "step.finished", "run.finished"])
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
            run = execute(create_run(self.target))
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
            run = execute(create_run(target))
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
            run = execute(create_run(target))
        self.assertEqual(run.status, "failed")
