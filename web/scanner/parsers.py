"""Turn uploaded dependency files into a list of packages.

Supported: requirements.txt, package.json, package-lock.json (v1-v3) and the
"Other software" box (`<name> <version>` per line).
"""
import json
import re
from dataclasses import dataclass, field

EXACT_SEMVER = re.compile(r"^v?\d+(\.\d+)*([-+][\w.\-]+)?$")
REQ_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._\-]*)"  # project name
    r"(?:\[[^\]]*\])?"                          # extras
    r"\s*(?P<spec>(?:[=!<>~]=?|===)[^;#]*)?"    # version specifier
)
OTHER_LINE = re.compile(r"^(?P<name>[A-Za-z0-9][\w.\-]*)\s+v?(?P<version>\d[\w.\-]*)$")


@dataclass
class Package:
    ecosystem: str          # "PyPI", "npm" or "Other"
    name: str
    version: str | None     # exact version, or None when unknown
    spec: str = ""          # what the file said, e.g. ">=2.9" or "^4.17.0"
    source: str = ""        # file it came from

    @property
    def key(self):
        return (self.ecosystem, self.name.lower())


@dataclass
class ParseResult:
    packages: list = field(default_factory=list)
    unreadable: list = field(default_factory=list)   # (source, line) pairs
    files: list = field(default_factory=list)        # (source, kind, count)


def normalise_pypi(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text, source="requirements.txt"):
    pkgs, bad = [], []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or "://" in line or line.startswith((".", "/")):
            bad.append((source, raw.strip()))   # -r, -e, URLs, local paths
            continue
        m = REQ_LINE.match(line)
        if not m:
            bad.append((source, raw.strip()))
            continue
        spec = (m["spec"] or "").strip()
        exact = re.fullmatch(r"===?\s*([\w.\-+!]+)", spec)
        pkgs.append(Package("PyPI", normalise_pypi(m["name"]),
                            exact.group(1) if exact and "*" not in spec else None, spec, source))
    return pkgs, bad


def parse_package_json(data, source="package.json"):
    pkgs = []
    for section in ("dependencies", "devDependencies"):
        for name, spec in (data.get(section) or {}).items():
            spec = str(spec).strip()
            version = spec.lstrip("=v") if EXACT_SEMVER.match(spec.lstrip("=")) else None
            pkgs.append(Package("npm", name, version, spec, source))
    return pkgs


def parse_package_lock(data, source="package-lock.json"):
    pkgs = []
    if "packages" in data:                       # lockfile v2 / v3
        for path, info in data["packages"].items():
            if not path or "node_modules/" not in path or info.get("link"):
                continue
            name = info.get("name") or path.rsplit("node_modules/", 1)[1]
            if info.get("version"):
                pkgs.append(Package("npm", name, info["version"], info["version"], source))
    else:                                        # lockfile v1
        def walk(deps):
            for name, info in (deps or {}).items():
                if info.get("version"):
                    pkgs.append(Package("npm", name, info["version"], info["version"], source))
                walk(info.get("dependencies"))
        walk(data.get("dependencies"))
    return pkgs


def parse_other(text, source="Other software"):
    pkgs, bad = [], []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = OTHER_LINE.match(line)
        if m:
            pkgs.append(Package("Other", m["name"].lower(), m["version"], m["version"], source))
        else:
            bad.append((source, raw.strip()))
    return pkgs, bad


def parse_file(text, filename=""):
    """Parse one file (or pasted text), guessing its kind from name or content."""
    name = filename.lower()
    stripped = text.lstrip()
    if name.endswith(".json") or stripped.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [], [(filename or "pasted text", "not valid JSON")], "json"
        if "lockfileVersion" in data:
            return parse_package_lock(data, filename or "package-lock.json"), [], "package-lock.json"
        return parse_package_json(data, filename or "package.json"), [], "package.json"
    pkgs, bad = parse_requirements(text, filename or "pasted text")
    return pkgs, bad, "requirements.txt"


def merge(packages):
    """One entry per package; an exact version (e.g. from a lockfile) wins."""
    out = {}
    for p in packages:
        key = (p.key, p.version) if p.version else (p.key, None)
        have_exact = any(k[0] == p.key and k[1] for k in out)
        if p.version is None and have_exact:
            continue
        if p.version:
            out.pop((p.key, None), None)
        out.setdefault(key, p)
    return list(out.values())


def parse_inputs(uploads, pasted="", other=""):
    """uploads: iterable of (filename, text)."""
    result = ParseResult()
    allpkgs = []
    for filename, text in uploads:
        pkgs, bad, kind = parse_file(text, filename)
        allpkgs += pkgs
        result.unreadable += bad
        result.files.append((filename, kind, len(pkgs)))
    if pasted.strip():
        pkgs, bad, kind = parse_file(pasted, "")
        allpkgs += pkgs
        result.unreadable += bad
        result.files.append(("pasted text", kind, len(pkgs)))
    if other.strip():
        pkgs, bad = parse_other(other)
        allpkgs += pkgs
        result.unreadable += bad
        result.files.append(("Other software", "other", len(pkgs)))
    result.packages = sorted(merge(allpkgs), key=lambda p: (p.ecosystem, p.name.lower()))
    return result
