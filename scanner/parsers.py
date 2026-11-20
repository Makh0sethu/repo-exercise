"""Turn uploaded dependency files into a list of packages.

Supported: requirements.txt, pyproject.toml, poetry.lock, uv.lock,
package.json, package-lock.json (v1-v3), yarn.lock, pnpm-lock.yaml,
and the "Other software" box (`<name> <version>` per line).
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field

EXACT_SEMVER = re.compile(r"^v?\d+(\.\d+)*([-+][\w.\-]+)?$")
REQ_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._\-]*)"  # project name
    r"(?:\[[^\]]*\])?"                          # extras
    r"\s*(?P<spec>(?:[=!<>~]=?|===)[^;#\\]*)?"  # version specifier
)
OTHER_LINE = re.compile(r"^(?P<name>[A-Za-z0-9][\w.\-]*)\s+v?(?P<version>\d[\w.\-]*)$")
YARN_V1_VERSION = re.compile(r'^\s+version\s+"([^"]+)"')
YARN_V2_VERSION = re.compile(r'^\s+version:\s*"?([^\s"]+)"?')
PNPM_PKG_LINE = re.compile(r"^\s{2}['\"]?/?(?P<entry>@?[^@'\":\s]+@[^'\":(\s]+)")


@dataclass
class Package:
    ecosystem: str          # "PyPI", "npm" or "Other"
    name: str
    version: str | None     # exact version, or None when unknown
    spec: str = ""          # what the file said, e.g. ">=2.9" or "^4.17.0"
    source: str = ""        # file it came from

    @property
    def key(self) -> tuple[str, str]:
        return (self.ecosystem, self.name.lower())


@dataclass
class ParseResult:
    packages: list[Package] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)   # (source, line) pairs
    files: list[tuple[str, str, int]] = field(default_factory=list)   # (source, kind, count)


def normalise_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_req_spec(line: str, source: str) -> Package | None:
    """Parse one PEP 508 requirement string (without comments/continuations)."""
    # Strip trailing --hash=... / --global-option=... flags
    cleaned = re.split(r"\s+--[a-z-]+", line, maxsplit=1)[0].strip()
    m = REQ_LINE.match(cleaned)
    if not m:
        return None
    spec = (m["spec"] or "").strip()
    exact = re.fullmatch(r"===?\s*([\w.\-+!]+)", spec)
    return Package(
        "PyPI",
        normalise_pypi(m["name"]),
        exact.group(1) if exact and "*" not in spec else None,
        spec,
        source,
    )


def parse_requirements(text: str, source: str = "requirements.txt") -> tuple[list[Package], list[tuple[str, str]]]:
    pkgs: list[Package] = []
    bad: list[tuple[str, str]] = []
    # Join backslash line continuations (common with --hash=... lines)
    joined = re.sub(r"\\\r?\n", " ", text)
    for raw in joined.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-") or "://" in line or line.startswith((".", "/")):
            bad.append((source, raw.strip()))   # -r, -e, URLs, local paths
            continue
        pkg = _parse_req_spec(line, source)
        if pkg is None:
            bad.append((source, raw.strip()))
            continue
        pkgs.append(pkg)
    return pkgs, bad


def parse_pyproject(text: str, source: str = "pyproject.toml") -> tuple[list[Package], list[tuple[str, str]]]:
    """Parse PEP 621 [project] dependencies and [tool.poetry.dependencies]."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return [], [(source, "not valid TOML")]

    pkgs: list[Package] = []
    bad: list[tuple[str, str]] = []

    project = data.get("project") or {}
    dep_lists = [project.get("dependencies") or []]
    for opt in (project.get("optional-dependencies") or {}).values():
        if isinstance(opt, list):
            dep_lists.append(opt)
    for dep_list in dep_lists:
        for item in dep_list:
            if not isinstance(item, str):
                continue
            pkg = _parse_req_spec(item.strip(), source)
            if pkg:
                pkgs.append(pkg)
            else:
                bad.append((source, item.strip()))

    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for section in ("dependencies", "dev-dependencies"):
        for name, spec_val in (poetry.get(section) or {}).items():
            if name.lower() == "python":
                continue
            spec = spec_val if isinstance(spec_val, str) else (
                spec_val.get("version", "") if isinstance(spec_val, dict) else ""
            )
            spec = str(spec).strip()
            exact = spec.lstrip("=v") if EXACT_SEMVER.match(spec.lstrip("=")) else None
            pkgs.append(Package("PyPI", normalise_pypi(name), exact, spec, source))

    return pkgs, bad


def parse_toml_lock(text: str, source: str) -> tuple[list[Package], list[tuple[str, str]]]:
    """Parse poetry.lock or uv.lock (both use [[package]] tables with name/version)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return [], [(source, "not valid TOML")]

    pkgs: list[Package] = []
    for entry in data.get("package") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        version = entry.get("version")
        if name and version:
            pkgs.append(Package("PyPI", normalise_pypi(str(name)), str(version), str(version), source))
    return pkgs, []


def parse_package_json(data: dict, source: str = "package.json") -> list[Package]:
    pkgs: list[Package] = []
    for section in ("dependencies", "devDependencies"):
        for name, spec in (data.get(section) or {}).items():
            spec = str(spec).strip()
            version = spec.lstrip("=v") if EXACT_SEMVER.match(spec.lstrip("=")) else None
            pkgs.append(Package("npm", name, version, spec, source))
    return pkgs


def parse_package_lock(data: dict, source: str = "package-lock.json") -> list[Package]:
    pkgs: list[Package] = []
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


def _yarn_header_name(header: str) -> str | None:
    """Extract npm package name from a yarn.lock entry header."""
    first = header.split(",", 1)[0].strip().strip('"').strip("'")
    if not first or first.startswith("__"):
        return None
    # Scoped package: @scope/name@npm:^1.0.0 -> @scope/name
    if first.startswith("@"):
        idx = first.find("@", 1)
    else:
        idx = first.find("@")
    return first[:idx] if idx > 0 else None


def parse_yarn_lock(text: str, source: str = "yarn.lock") -> list[Package]:
    """Parse yarn.lock (classic v1 and Berry v2+)."""
    pkgs: list[Package] = []
    current_name: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            if raw.rstrip().endswith(":"):
                current_name = _yarn_header_name(raw.rstrip()[:-1])
            else:
                current_name = None
            continue
        if current_name:
            m = YARN_V1_VERSION.match(raw) or YARN_V2_VERSION.match(raw)
            if m:
                ver = m.group(1)
                pkgs.append(Package("npm", current_name, ver, ver, source))
                current_name = None
    return pkgs


def parse_pnpm_lock(text: str, source: str = "pnpm-lock.yaml") -> list[Package]:
    """Parse pnpm-lock.yaml (v6 and v9) packages/snapshots blocks without PyYAML."""
    pkgs: list[Package] = []
    in_section = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw[0].isspace():
            in_section = stripped in ("packages:", "snapshots:")
            continue
        if not in_section:
            continue
        m = PNPM_PKG_LINE.match(raw)
        if not m:
            continue
        entry = m.group("entry")
        # Split on last '@' to support scoped packages like @types/node@20.11.0
        at_idx = entry.rfind("@")
        if at_idx <= 0:
            continue
        name = entry[:at_idx]
        ver = entry[at_idx + 1:].split("_", 1)[0]
        if name and ver and EXACT_SEMVER.match(ver):
            pkgs.append(Package("npm", name, ver, ver, source))
    return pkgs


def parse_other(text: str, source: str = "Other software") -> tuple[list[Package], list[tuple[str, str]]]:
    pkgs: list[Package] = []
    bad: list[tuple[str, str]] = []
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


def parse_file(text: str, filename: str = "") -> tuple[list[Package], list[tuple[str, str]], str]:
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

    if name.endswith("pyproject.toml") or ("[project]" in text and "dependencies" in text):
        pkgs, bad = parse_pyproject(text, filename or "pyproject.toml")
        return pkgs, bad, "pyproject.toml"

    if name.endswith(("poetry.lock", "uv.lock")) or "[[package]]" in text:
        kind = "uv.lock" if "uv.lock" in name else "poetry.lock"
        pkgs, bad = parse_toml_lock(text, filename or kind)
        return pkgs, bad, kind

    if name.endswith("yarn.lock") or "yarn lockfile" in text[:200].lower():
        return parse_yarn_lock(text, filename or "yarn.lock"), [], "yarn.lock"

    if name.endswith(("pnpm-lock.yaml", "pnpm-lock.yml")) or stripped.startswith("lockfileVersion:"):
        return parse_pnpm_lock(text, filename or "pnpm-lock.yaml"), [], "pnpm-lock.yaml"

    pkgs, bad = parse_requirements(text, filename or "pasted text")
    return pkgs, bad, "requirements.txt"


def merge(packages: list[Package]) -> list[Package]:
    """One entry per package; an exact version (e.g. from a lockfile) wins."""
    out: dict[tuple, Package] = {}
    for p in packages:
        key = (p.key, p.version) if p.version else (p.key, None)
        have_exact = any(k[0] == p.key and k[1] for k in out)
        if p.version is None and have_exact:
            continue
        if p.version:
            out.pop((p.key, None), None)
        out.setdefault(key, p)
    return list(out.values())


def parse_inputs(uploads, pasted: str = "", other: str = "") -> ParseResult:
    """uploads: iterable of (filename, text)."""
    result = ParseResult()
    allpkgs: list[Package] = []
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
