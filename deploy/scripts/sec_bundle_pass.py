"""sec-bundle pass for release packagers (decision 0006, layer 2).

A packager calls token() with its artifact files and inject() on the release script. The
script then carries SEC_PASS / SEC_BIND / SEC_AT, and its step 1 runs sec_bundle_check
(see sec-bundle-check.snippet.sh) before touching anything live. Packaging fails if there is
no fresh pass, so a release cannot be built without one.

Proves a release was packaged after a pass; it does not prove the pass was truthful.
"""
import hashlib
import os
import re
import time

MAX_AGE = 4 * 3600  # a pass is good for 4 hours (same as the Claude hook and the wrappers)
BLOCK = re.compile(r"(# SEC_BUNDLE_BEGIN[^\n]*\n).*?(# SEC_BUNDLE_END)", re.S)


def sha256_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def find_pass(start):
    d = os.path.abspath(start)
    for _ in range(8):
        p = os.path.join(d, ".claude", "sec-bundle.pass")
        if os.path.isfile(p):
            return p
        if os.path.exists(os.path.join(d, ".git")):
            return None  # project boundary: a pass is per app, as in the gate script
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def token(artifacts, start):
    """Return (pass_hash, bind, packaged_at) or exit with an instruction."""
    p = find_pass(start)
    if not p:
        raise SystemExit("No .claude/sec-bundle.pass found. Run the sec-bundle agent for this project first.")
    age = time.time() - os.path.getmtime(p)
    if age > MAX_AGE:
        raise SystemExit(f"The sec-bundle pass is {age / 3600:.1f}h old (limit 4h). Run the sec-bundle agent again.")
    pass_hash = sha256_file(p)
    bind = hashlib.sha256((pass_hash + "".join(sha256_file(a) for a in artifacts)).encode()).hexdigest()
    return pass_hash, bind, int(time.time())


def inject(script_text, tok):
    """Fill the SEC_BUNDLE block of a release script; fail if the script has no block or no check."""
    if not BLOCK.search(script_text):
        raise SystemExit("Release script has no '# SEC_BUNDLE_BEGIN ... # SEC_BUNDLE_END' block. "
                         "Add it from sec-bundle-check.snippet.sh (decision 0006).")
    if "sec_bundle_check " not in script_text:
        raise SystemExit("Release script never calls sec_bundle_check. Add the call to its checks step.")
    pass_hash, bind, at = tok
    filled = f'SEC_PASS="{pass_hash}" SEC_BIND="{bind}" SEC_AT={at}\n'
    return BLOCK.sub(lambda m: m.group(1) + filled + m.group(2), script_text, count=1)
