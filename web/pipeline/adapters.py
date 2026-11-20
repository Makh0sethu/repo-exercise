"""Tool adapters. Each takes a step spec and a workspace and returns an
AdapterResult: the exact argv that ran, its exit code, raw stdout/stderr (kept as
evidence), and normalised findings. Adapters never run anything that is not
spelled out in the pipeline definition."""
import base64
import os
import re
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
    ok: bool = None                                # did the tool run properly? default: exit_code == 0

    def __post_init__(self):
        if self.ok is None:
            self.ok = self.exit_code == 0


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
    depth = int(spec.get("depth", 1))
    if not 1 <= depth <= 1000:
        raise UnsafeTarget("clone depth must be 1..1000")
    argv = ["git", "clone", "--depth", str(depth), "--no-tags", "--single-branch"]
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



# ---------------------------------------------------------------- local scanners
import hashlib
import json
import shutil


class ToolMissing(Exception):
    pass


def find_tool(name):
    """Locate a scanner binary: configured tool dirs first, then PATH."""
    dirs = os.pathsep.join([*map(str, getattr(settings, "PIPELINE_TOOL_DIRS", [])), os.environ.get("PATH", "")])
    path = shutil.which(name, path=dirs)
    if not path:
        raise ToolMissing(f"{name} is not installed on this server (looked in {dirs or 'PATH'})")
    return path


def tool_info(path, version_args=("--version",)):
    """What exactly ran: the binary's sha256 and the version it reports."""
    sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    try:
        v = subprocess.run([path, *version_args], capture_output=True, text=True, timeout=30)
        version = (v.stdout or v.stderr).strip().splitlines()[0][:120] if (v.stdout or v.stderr).strip() else ""
    except Exception:
        version = ""
    return {"binary_sha256": sha, "reported_version": version}


# Variables that only say how to reach the network. Forwarded to scanners that must call
# an advisory service (pip-audit, npm audit); never to the others, and never anything else.
NETWORK_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")


def _scrub_env(ctx, network=False, **extra):
    """Scanners get a minimal environment: no credentials, nothing inherited but PATH.
    network=True also forwards proxy/CA settings; their names are kept in ctx for the audit record."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(ctx["workspace"]), "NO_COLOR": "1"}
    if network:
        passed = [k for k in NETWORK_ENV if os.environ.get(k)]
        env.update({k: os.environ[k] for k in passed})
        ctx.setdefault("network_env_passed", sorted(set(passed)))
    return {**env, **extra}


def _missing(name, exc):
    return AdapterResult([name], 127, "", str(exc), summary={"error": "tool missing"})


def secrets_scan(spec, ctx) -> AdapterResult:
    """Gitleaks over the checkout (and its git history, as deep as it was cloned).
    Secret values are redacted by the tool and dropped here; only where they are is kept."""
    try:
        binary = find_tool("gitleaks")
    except ToolMissing as exc:
        return _missing("gitleaks", exc)
    src, report = ctx["values"]["src"], ctx["workspace"] / "gitleaks.json"
    argv = ["gitleaks", "detect", "--source", src, "--report-format", "json", "--report-path", str(report),
            "--redact", "--no-banner", "--exit-code", "2"]
    p = subprocess.run([binary, *argv[1:]], capture_output=True, text=True, timeout=900, env=_scrub_env(ctx))
    info = {**tool_info(binary, ("version",)), "pinned_release": spec.get("version", "")}
    if p.returncode not in (0, 2):
        return AdapterResult(argv, p.returncode, "", p.stderr[-2000:], summary={"error": "gitleaks failed", **info})
    leaks = json.loads(report.read_text() or "[]") if report.exists() else []
    findings, lines = [], []
    for l in leaks:
        loc = f"{l['File']}:{l['StartLine']}"
        findings.append({"severity": "high", "title": f"{l['Description']} ({l['RuleID']})", "location": loc,
                         "detail": {"rule": l["RuleID"], "commit": l.get("Commit", "")[:12], "fingerprint": l.get("Fingerprint", ""),
                                    "entropy": l.get("Entropy"), "note": "value redacted by the scanner and never stored"}})
        lines.append(f"HIGH  {loc}  {l['RuleID']}  {l['Description']}")
    return AdapterResult(argv, 0, "\n".join(lines) or "No secrets found", p.stderr[-500:], findings=findings,
                         summary={"findings": len(findings), "history_commits_scanned": spec.get("history", "as cloned"), **info})


BANDIT_SEV = {"HIGH": "high", "MEDIUM": "moderate", "LOW": "low"}
# Bandit quotes the offending value in these messages ("Possible hardcoded password: 'x'"),
# so the quoted part is removed before anything is stored.
BANDIT_QUOTES_SECRET = {"B105", "B106", "B107"}


def scrub_bandit_text(test_id, text):
    return re.sub(r"'[^']*'", "'<redacted>'", text) if test_id in BANDIT_QUOTES_SECRET else text


def sast_python(spec, ctx) -> AdapterResult:
    """Bandit: static analysis of Python source. It parses code, it never runs it."""
    try:
        binary = find_tool("bandit")
    except ToolMissing as exc:
        return _missing("bandit", exc)
    src = ctx["values"]["src"]
    argv = ["bandit", "-r", src, "-f", "json", "-q", "-x", ",".join(f"{src}/{d}" for d in sorted(SKIP_DIRS))]
    p = subprocess.run([binary, *argv[1:]], capture_output=True, text=True, timeout=900, env=_scrub_env(ctx))
    info = tool_info(binary)
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return AdapterResult(argv, p.returncode or 1, "", p.stderr[-2000:], summary={"error": "no bandit output", **info})
    findings, lines = [], []
    for r in data.get("results", []):
        sev = BANDIT_SEV.get(r["issue_severity"], "low")
        loc = f"{Path(r['filename']).relative_to(src)}:{r['line_number']}"
        text = scrub_bandit_text(r["test_id"], r["issue_text"])
        findings.append({"severity": sev, "title": f"{r['test_id']} {r['test_name']}: {text}", "location": loc,
                         "detail": {"test_id": r["test_id"], "confidence": r["issue_confidence"],
                                    "cwe": (r.get("issue_cwe") or {}).get("id"), "more_info": r.get("more_info")}})   # 'code' omitted on purpose
        lines.append(f"{sev.upper():9} {loc}  {r['test_id']}  {text}")
    errs = data.get("errors", [])
    return AdapterResult(argv, 0, "\n".join(lines) or "No issues found", findings=findings,
                         summary={"files_scanned": len(data.get("metrics", {})) - 1, "findings": len(findings),
                                  "unparseable_files": len(errs), **info})


def _find(src, pattern):
    return [p for p in sorted(src.rglob(pattern)) if not (set(p.relative_to(src).parts) & SKIP_DIRS)]


def pip_audit(spec, ctx) -> AdapterResult:
    """pip-audit on pinned requirements files. --no-deps --disable-pip means nothing is installed or executed."""
    try:
        binary = find_tool("pip-audit")
    except ToolMissing as exc:
        return _missing("pip-audit", exc)
    src = Path(ctx["values"]["src"])
    files = _find(src, "requirements*.txt")
    info = tool_info(binary)
    if not files:
        return AdapterResult(["pip-audit"], 0, "No requirements files found", summary={"files": 0, **info})
    findings, lines, skipped, ran = [], [], [], 0
    for f in files:
        rel = str(f.relative_to(src))
        argv = [binary, "-r", str(f), "--no-deps", "--disable-pip", "-f", "json", "--progress-spinner", "off"]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=900, env=_scrub_env(ctx, network=True))
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError:
            skipped.append({"file": rel, "why": (p.stderr.strip().splitlines() or ["no output"])[-1][:200]})
            continue
        ran += 1
        for dep in data["dependencies"]:
            for v in dep.get("vulns", []):
                cve = next((a for a in v.get("aliases", []) if a.startswith("CVE-")), v["id"])
                where = f"{dep['name']}=={dep['version']} ({rel})"
                findings.append({"severity": "unrated", "title": f"{cve}: {v.get('description', '')[:160]}", "location": where,
                                 "detail": {"id": v["id"], "aliases": v.get("aliases", []), "fixed": v.get("fix_versions", []), "file": rel}})
                lines.append(f"{where}  {cve}  fix: {', '.join(v.get('fix_versions', [])) or 'none listed'}")
    summary = {"files": len(files), "checked": ran, "skipped": skipped, "findings": len(findings),
               "env_names_forwarded": ctx.get("network_env_passed", []), **info}
    if ran == 0:                                      # nothing could be checked: not a clean result
        return AdapterResult(["pip-audit"], 2, "", json.dumps(skipped), summary={**summary, "error": "no requirements file could be checked"})
    return AdapterResult(["pip-audit", "-r", "<each requirements file>", "--no-deps", "--disable-pip"], 0,
                         "\n".join(lines) or "No known vulnerabilities", findings=findings, summary=summary)


NPM_SEV = {"critical": "critical", "high": "high", "moderate": "moderate", "low": "low", "info": "low"}


def npm_audit(spec, ctx) -> AdapterResult:
    """npm audit against package-lock.json only. Nothing is installed and no scripts run."""
    try:
        binary = find_tool("npm")
    except ToolMissing as exc:
        return _missing("npm", exc)
    src = Path(ctx["values"]["src"])
    locks = _find(src, "package-lock.json")
    info = tool_info(binary)
    if not locks:
        return AdapterResult(["npm", "audit"], 0, "No package-lock.json found", summary={"files": 0, **info})
    findings, lines, errors = [], [], []
    for lock in locks:
        rel = str(lock.parent.relative_to(src)) or "."
        env = _scrub_env(ctx, network=True, npm_config_ignore_scripts="true", npm_config_audit_level="none", npm_config_update_notifier="false")
        p = subprocess.run([binary, "audit", "--package-lock-only", "--json"], cwd=lock.parent, capture_output=True,
                           text=True, timeout=900, env=env)
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError:
            errors.append({"dir": rel, "why": p.stderr.strip()[-200:]})
            continue
        if "error" in data:
            errors.append({"dir": rel, "why": str(data["error"].get("summary") or data["error"])[:200]})
            continue
        for name, v in data.get("vulnerabilities", {}).items():
            via = next((x for x in v.get("via", []) if isinstance(x, dict)), {})
            sev = NPM_SEV.get(v.get("severity"), "low")
            fix = v.get("fixAvailable")
            findings.append({"severity": sev, "title": via.get("title") or f"Vulnerable dependency chain through {name}",
                             "location": f"{name} ({rel}/package-lock.json)",
                             "detail": {"range": v.get("range"), "direct": v.get("isDirect"), "url": via.get("url"),
                                        "fix": fix if isinstance(fix, bool) else {"name": fix.get("name"), "version": fix.get("version")}}})
            lines.append(f"{sev.upper():9} {name}  {via.get('title', '')}")
    summary = {"lockfiles": len(locks), "findings": len(findings), "errors": errors,
               "env_names_forwarded": ctx.get("network_env_passed", []), **info}
    if errors and len(errors) == len(locks):          # nothing could be checked: not a clean result
        return AdapterResult(["npm", "audit"], 2, "", json.dumps(errors), summary={**summary, "error": "npm audit could not run"})
    return AdapterResult(["npm", "audit", "--package-lock-only", "--json"], 0, "\n".join(lines) or "No known vulnerabilities",
                         findings=findings, summary=summary)


ADAPTERS = {"git_clone": git_clone, "dependency_scan": dependency_scan, "secrets_scan": secrets_scan,
            "sast_python": sast_python, "pip_audit": pip_audit, "npm_audit": npm_audit}
