"""Package a Sentinel release for sentinel.uhambo.co.zw, gated by sec-bundle (decision 0006).

Usage: python deploy/scripts/package_release.py <release-id> <output-dir>
       release ids look like 20261123 or 20261123b

Packs the committed code (git archive HEAD, so nothing uncommitted, ignored or secret goes up) into
<output-dir>/sentinel-app.tgz and writes <output-dir>/sentinel-release.sh from the template with the
release id, commit, checksum and a sec-bundle pass token. Refuses without a fresh .claude/sec-bundle.pass,
with uncommitted changes to tracked files, or if the package holds anything that must not ship.
"""
import hashlib
import os
import re
import subprocess
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sec_bundle_pass  # noqa: E402

if len(sys.argv) != 3:
    raise SystemExit(__doc__)
rel_id, out = sys.argv[1], sys.argv[2]
if not re.fullmatch(r"\d{8}[a-z]?", rel_id):
    raise SystemExit("release ids look like 20261123 or 20261123b")
here = os.path.dirname(os.path.abspath(__file__))
app = os.path.abspath(os.path.join(here, "..", ".."))


def git(*args):
    return subprocess.run(["git", "-C", app, *args], check=True, capture_output=True, text=True).stdout


sec_bundle_pass.token([], app)  # fail fast: no fresh sec-bundle pass, nothing is written
if git("status", "--porcelain", "--untracked-files=no").strip():
    raise SystemExit("Tracked files have uncommitted changes. Commit them first: the package is built from HEAD.")
commit = git("rev-parse", "--short=12", "HEAD").strip()

os.makedirs(out, exist_ok=True)
tgz = os.path.join(out, "sentinel-app.tgz")
subprocess.run(["git", "-C", app, "archive", "--format=tar.gz", "-o", os.path.abspath(tgz), "HEAD"], check=True)

# What must never ship, and what must be there.
with tarfile.open(tgz) as tf:
    names = [m.name for m in tf.getmembers() if m.isfile()]
    for n in names:
        base = n.rsplit("/", 1)[-1]
        if base == ".env" or base.endswith((".sqlite3", ".pem", ".key")) or base == "sec-bundle.pass" or n.startswith(".claude/"):
            raise SystemExit(f"refusing to ship {n}")
        if n.endswith((".sh", ".service", ".timer", ".conf")) and b"\r" in tf.extractfile(n).read():
            raise SystemExit(f"{n} has Windows line endings; bash on the server would fail (see .gitattributes)")
    for must in ("manage.py", "requirements.txt", "config/urls_public.py", "deploy/install.sh", "deploy/rollback.sh",
                 "deploy/env.example", "deploy/sentinel.service", "deploy/sentinel.uhambo.co.zw.nginx.conf"):
        if must not in names:
            raise SystemExit(f"package is missing {must}")
    env_example = tf.extractfile("deploy/env.example").read().decode()
    if "SENTINEL_PUBLIC_ONLY=1" not in env_example:
        raise SystemExit("deploy/env.example must keep SENTINEL_PUBLIC_ONLY=1 for the public test site")
digest = hashlib.sha256(open(tgz, "rb").read()).hexdigest()

s = open(os.path.join(here, "sentinel-release.template.sh"), encoding="utf-8").read()
s = s.replace("@REL@", rel_id).replace("@COMMIT@", commit)
s, n = re.subn(r"(<<'SUMS'\n)(SUMS\n)", lambda m: m.group(1) + f"{digest} *sentinel-app.tgz\n" + m.group(2), s)
if n != 1:
    raise SystemExit("template has no empty SUMS block")
s = sec_bundle_pass.inject(s, sec_bundle_pass.token([tgz], app))
s = s.replace("\r\n", "\n")
open(os.path.join(out, "sentinel-release.sh"), "w", encoding="utf-8", newline="\n").write(s)
print(f"commit {commit}: {len(names)} files, sentinel-app.tgz {os.path.getsize(tgz)} bytes, sha256 {digest[:16]}…")
print(f"wrote {os.path.join(out, 'sentinel-release.sh')} (release {rel_id}, sec-bundle pass embedded)")
