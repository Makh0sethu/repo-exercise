"""Run a pipeline: every step is announced, executed, and its outcome recorded
on the audit chain, with raw output kept as hash-addressed evidence."""
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from audit.log import record, store_evidence

from .adapters import ADAPTERS
from .gitsafe import UnsafeTarget
from .models import Finding, Run, Step

DEFAULT_PIPELINE = [
    {"tool": "git_clone", "required": True, "reason": "Fetch the exact source to test and pin the commit"},
    {"tool": "secrets_scan", "reason": "Find API keys, tokens and passwords committed to the repo (Gitleaks)"},
    {"tool": "sast_python", "reason": "Find insecure patterns in Python code without running it (Bandit)"},
    {"tool": "pip_audit", "reason": "Check pinned Python dependencies for known vulnerabilities (pip-audit, PyPI advisories)"},
    {"tool": "npm_audit", "reason": "Check npm lockfile dependencies for known vulnerabilities (npm audit, lockfile only)"},
    {"tool": "dependency_scan", "reason": "Cross-check every dependency file on OSV.dev"},
]


def create_run(target, requested_by="", pipeline=None) -> Run:
    pipeline = pipeline or DEFAULT_PIPELINE
    for s in pipeline:
        if s["tool"] not in ADAPTERS:
            raise ValueError(f"Unknown tool {s['tool']!r}")
    run = Run.objects.create(target=target, pipeline=pipeline, requested_by=requested_by)
    record("run.created", actor=requested_by or "system", obj=run,
           payload={"target": target.name, "git_url": target.git_url, "ref": target.ref, "pipeline": pipeline})
    return run


def execute(run: Run) -> Run:
    workdir = Path(getattr(settings, "PIPELINE_WORKDIR", tempfile.gettempdir())) / f"sentinel-run-{run.pk}"
    workdir.mkdir(parents=True, exist_ok=True)
    ctx = {"target": run.target, "workspace": workdir, "values": {}}
    run.status = "running"; run.save(update_fields=["status"])
    failed = False
    try:
        for order, spec in enumerate(run.pipeline, start=1):
            step = Step.objects.create(run=run, order=order, tool=spec["tool"], reason=spec["reason"], status="running",
                                       started_at=timezone.now())
            record("step.started", actor="system", obj=step,
                   payload={"run": run.pk, "tool": spec["tool"], "reason": spec["reason"]})
            try:
                res = ADAPTERS[spec["tool"]](spec, ctx)
            except UnsafeTarget as exc:
                from .adapters import AdapterResult
                res = AdapterResult([], 1, "", f"Refused: {exc}", summary={"error": "unsafe target"})
            except Exception as exc:  # a crashing tool is a result, not a lost run
                from .adapters import AdapterResult
                res = AdapterResult([], 3, "", f"Adapter crashed: {exc!r}", summary={"error": "crash"})
            out = store_evidence(res.stdout, label=f"{spec['tool']} stdout")
            err = store_evidence(res.stderr, label=f"{spec['tool']} stderr")
            step.argv, step.exit_code, step.summary = res.argv, res.exit_code, res.summary
            step.stdout_sha256, step.stderr_sha256 = out.sha256, err.sha256
            step.status = "passed" if res.ok else "failed"
            step.finished_at = timezone.now()
            step.save()
            for f in res.findings:
                fi = Finding.objects.create(run=run, step=step, severity=f["severity"], title=f["title"][:300],
                                            location=f["location"][:300], detail=f["detail"], evidence_sha256=out.sha256)
                record("finding.recorded", actor="system", obj=fi,
                       payload={"run": run.pk, "step": step.pk, "severity": fi.severity, "title": fi.title,
                                "location": fi.location, "evidence": out.sha256})
            record("step.finished", actor="system", obj=step,
                   payload={"run": run.pk, "tool": spec["tool"], "argv": res.argv, "exit_code": res.exit_code,
                            "stdout": out.sha256, "stderr": err.sha256, "summary": res.summary, "findings": len(res.findings)})
            if res.values.get("commit_sha"):  # noqa
                run.commit_sha = res.values["commit_sha"]; run.save(update_fields=["commit_sha"])
            ctx["values"].update(res.values)
            if not res.ok:
                failed = True
                if spec.get("required"):      # no source means nothing later can run
                    break
    finally:
        shutil.rmtree(workdir, ignore_errors=True)   # evidence is kept; the checkout is not
    run.status = "failed" if failed else "passed"
    run.finished_at = timezone.now(); run.save(update_fields=["status", "finished_at"])
    record("run.finished", actor="system", obj=run,
           payload={"status": run.status, "commit": run.commit_sha, "findings": run.findings.count()})
    return run
