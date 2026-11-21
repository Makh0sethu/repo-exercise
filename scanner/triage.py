"""Turn raw advisories into a short, honest, actionable list.

Three jobs, all pure functions so they are easy to test:

1. `spec_bounds`   what range of versions does a requirement like ">=5.2,<7" or "^6.2.0" allow?
2. `could_affect`  can this advisory touch anything inside that range? (An advisory fixed at
                   2.2.4 cannot affect ">=5.2".) Advisories that cannot are hidden, not counted.
3. `group_by_fix`  collapse the rest into "upgrade X to Y clears N" groups.

A package with an exact version (a lockfile, or `==1.2.3`) skips step 2: OSV already matched it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .versions import RANK, compare_versions, version_in_range, version_sort_key

_OPS = r"(?:\^|~=|~|>=|<=|===|==|>|<|=)"
_OP_SPACE = re.compile(rf"({_OPS})\s+")
_TOKEN = re.compile(rf"^({_OPS})?v?(\d[\w.\-+]*)$")


@dataclass(frozen=True)
class Bounds:
    lower: str | None = None
    upper: str | None = None
    upper_inclusive: bool = False

    @property
    def known(self) -> bool:
        return self.lower is not None or self.upper is not None


def _ints(version: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", version.split("-", 1)[0].split("+", 1)[0])] or [0]


def _bump(parts: list[int], index: int) -> str:
    out = parts[:index] + [parts[index] + 1]
    return ".".join(str(x) for x in out)


def _caret_upper(version: str) -> str:
    parts = _ints(version)
    for i, n in enumerate(parts):          # first non-zero part is the one that may not change
        if n:
            return _bump(parts, i)
    return _bump(parts, len(parts) - 1)


def spec_bounds(spec: str | None) -> Bounds:
    """Lowest and highest version a requirement allows. Unknown pieces stay None."""
    text = (spec or "").strip()
    if not text or "||" in text or text in ("*", "latest", "x"):
        return Bounds()
    lower: str | None = None
    upper: str | None = None
    upper_inclusive = False

    def set_lower(v: str) -> None:
        nonlocal lower
        if lower is None or compare_versions(v, lower) > 0:
            lower = v

    def set_upper(v: str, inclusive: bool) -> None:
        nonlocal upper, upper_inclusive
        if upper is None or compare_versions(v, upper) < 0:
            upper, upper_inclusive = v, inclusive

    for token in re.split(r"[,\s]+", _OP_SPACE.sub(r"\1", text)):
        m = _TOKEN.match(token)
        if not m:
            continue
        op, ver = m.group(1) or "==", m.group(2)
        if op in (">=", ">"):
            set_lower(ver)
        elif op == "<":
            set_upper(ver, False)
        elif op == "<=":
            set_upper(ver, True)
        elif op in ("==", "===", "="):
            if "*" not in ver:
                set_lower(ver)
                set_upper(ver, True)
        elif op == "^":
            set_lower(ver)
            set_upper(_caret_upper(ver), False)
        elif op == "~":
            parts = _ints(ver)
            set_lower(ver)
            set_upper(_bump(parts, 1) if len(parts) > 1 else _bump(parts, 0), False)
        elif op == "~=":
            parts = _ints(ver)
            set_lower(ver)
            if len(parts) >= 2:
                set_upper(_bump(parts, len(parts) - 2), False)
    return Bounds(lower, upper, upper_inclusive)


def versions_in_range(vuln: dict[str, Any], bounds: Bounds) -> list[str]:
    """Explicitly listed affected versions that the requirement allows."""
    return [
        v for v in vuln.get("versions") or []
        if version_in_range(
            v,
            at_or_above=bounds.lower,
            below=None if bounds.upper_inclusive else bounds.upper,
            at_or_below=bounds.upper if bounds.upper_inclusive else None,
        )
    ]


def could_affect(vuln: dict[str, Any], bounds: Bounds) -> bool:
    """False only when the advisory provably cannot touch any version inside `bounds`."""
    fixed = vuln.get("fixed") or []
    if vuln.get("versions") and not fixed and not vuln.get("introduced") and bounds.known:
        return bool(versions_in_range(vuln, bounds))     # e.g. malware naming exact bad releases
    if bounds.lower and fixed and compare_versions(fixed[-1], bounds.lower) <= 0:
        return False                     # every affected version is older than the range starts
    introduced = [i for i in (vuln.get("introduced") or []) if i]
    if bounds.upper and introduced and all(i != "0" for i in introduced):
        first = min(introduced, key=version_sort_key)
        c = compare_versions(first, bounds.upper)
        if c > 0 or (c == 0 and not bounds.upper_inclusive):
            return False                 # every affected version is newer than the range ends
    return True


def fixed_within(vuln: dict[str, Any], bounds: Bounds) -> bool:
    """True when a fix exists inside the allowed range, so updating (not editing the range) clears it.

    Actively exploited and malware findings are never demoted: they stay visible either way.
    """
    if vuln.get("exploited") or is_malware(vuln):
        return False
    for f in vuln.get("fixed") or []:
        if bounds.lower and compare_versions(f, bounds.lower) <= 0:
            continue
        if bounds.upper:
            c = compare_versions(f, bounds.upper)
            if c > 0 or (c == 0 and not bounds.upper_inclusive):
                continue
        return True
    return False


def is_malware(vuln: dict[str, Any]) -> bool:
    ids = [vuln.get("id", ""), *(vuln.get("aliases") or [])]
    return any(str(i).upper().startswith("MAL-") for i in ids)


def target_version(vuln: dict[str, Any], current: str | None) -> str | None:
    """The nearest fixed version above `current`; the newest fix when current is unknown."""
    fixed = vuln.get("fixed") or []
    if not fixed:
        return None
    if current:
        above = [f for f in fixed if compare_versions(f, current) > 0]
        return above[0] if above else None
    return fixed[-1]


def group_by_fix(vulns: list[dict[str, Any]], current: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """([{to, count, worst, vulns}, ...] highest fix first, the one version that clears them all)."""
    buckets: dict[str | None, list[dict[str, Any]]] = {}
    for v in vulns:
        buckets.setdefault(target_version(v, current), []).append(v)
    groups = [
        {
            "to": to,
            "count": len(rows),
            "worst": max((r["severity"] for r in rows), key=lambda s: RANK.get(s, 0)),
            "vulns": rows,
        }
        for to, rows in buckets.items()
    ]
    with_fix = sorted((g for g in groups if g["to"]), key=lambda g: version_sort_key(g["to"]), reverse=True)
    without = [g for g in groups if not g["to"]]
    ordered = with_fix + without
    return ordered, (with_fix[0]["to"] if with_fix else None)


def sort_key(vuln: dict[str, Any]) -> tuple:
    """Malware first, then actively exploited, then severity, then id."""
    return (not is_malware(vuln), not vuln.get("exploited"), -RANK.get(vuln["severity"], 0), vuln["cve"])
