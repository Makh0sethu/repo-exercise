"""Tool adapters. Each takes a step spec and a workspace and returns an
AdapterResult: the exact argv that ran, its exit code, raw stdout/stderr (kept as
evidence), and normalised findings. Adapters never run anything that is not
spelled out in the pipeline definition."""
import base64
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings

from scanner.parsers import parse_inputs
from scanner.scan import query_osv

from .gitsafe import check_git_url, check_ref


@dataclass
class AdapterResult:
    argv: list
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    findings: list = field(default_factory=list)   # dicts: severity, title, location, detail
    summary: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)     # handed to later steps (e.g. commit_sha)


def _redact(text: str, secrets) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def git_clone(spec, ctx) -> AdapterResult:
    """Shallow-clone the target into the workspace and record the exact commit."""
    target = ctx["target"]
    url, ref = check_git_url(target.git_url), check_ref(target.ref)
    dest = ctx["workspace"] / "src"
    argv = ["git", "clone", "--depth", "1", "--no-tags", "--single-branch"]
    if ref != "HEAD":
        argv += ["--branch", ref]
    argv += ["--", url, str(dest)]

    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(ctx["workspace"]),
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    secrets = []
    token = os.environ.get("SENTINEL_GITHUB_TOKEN", "") if target.private else ""
    if token:
        # Passed through the environment, never argv, so it can't land in the audit log.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
        secrets = [token, basic]
    elif target.private:
        return AdapterResult(argv, 1, "", "Private target but SENTINEL_GITHUB_TOKEN is not set", summary={"error": "no credentials"})

    p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=300)
    out, err = _redact(p.stdout, secrets), _redact(p.stderr, secrets)
    values = {}
    if p.returncode == 0:
        sha = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, env=env)
        values["commit_sha"] = sha.stdout.strip()
        values["src"] = str(dest)
    return AdapterResult(argv, p.returncode, out, err, summary={"commit": values.get("commit_sha", "")}, values=values)


DEP_FILES = ("requirements.txt", "package.json", "package-lock.json")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}


def dependency_scan(spec, ctx) -> AdapterResult:
    """Find dependency files in the checkout and look them up on OSV.dev."""
    src = Path(ctx["values"]["src"])
    uploads = []
    for path in sorted(src.rglob("*")):
        if path.name in DEP_FILES and not (set(path.relative_to(src).parts) & SKIP_DIRS) and path.stat().st_size <= 1_000_000:
            uploads.append((str(path.relative_to(src)), path.read_text(errors="replace")))
    parsed = parse_inputs(uploads)
    argv = ["sentinel.scanner.query_osv", f"files={len(uploads)}", f"packages={len(parsed.packages)}"]
    if not parsed.packages:
        return AdapterResult(argv, 0, "No dependency files found", summary={"files": len(uploads), "packages": 0})
    try:
        vulns = query_osv(parsed.packages)
    except Exception as exc:
        return AdapterResult(argv, 2, "", f"OSV.dev lookup failed: {exc!r}", summary={"error": "osv unreachable"})
    findings, lines = [], []
    for p in parsed.packages:
        for v in vulns.get((p.key, p.version), []):
            sev = "moderate" if v["severity"] == "MODERATE" else v["severity"].lower()
            where = f"{p.name}@{p.version or p.spec or '*'}"
            findings.append({"severity": sev, "title": f"{v['id']}: {v['summary']}", "location": where,
                             "detail": {"ecosystem": p.ecosystem, "fixed": v["fixed"], "url": v["url"],
                                        "aliases": v["aliases"], "files": [f for f, _ in uploads]}})
            lines.append(f"{sev.upper():9} {where}  {v['id']}  fix: {', '.join(v['fixed']) or 'none listed'}")
    return AdapterResult(argv, 0, "\n".join(lines) or "No known vulnerabilities",
                         findings=findings, summary={"files": len(uploads), "packages": len(parsed.packages), "findings": len(findings)})


ADAPTERS = {"git_clone": git_clone, "dependency_scan": dependency_scan}
