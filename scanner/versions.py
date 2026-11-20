"""Version comparison, range matching, and CVSS severity bucketing."""
from __future__ import annotations

import math
import re

SEVERITIES = ["CRITICAL", "HIGH", "MODERATE", "LOW", "UNKNOWN"]
RANK = {s: 4 - i for i, s in enumerate(SEVERITIES)}

_TOKEN_RE = re.compile(r"(\d+|[a-zA-Z]+)")
_PRERELEASE_ORDER = {
    "dev": -5,
    "alpha": -4,
    "a": -4,
    "beta": -3,
    "b": -3,
    "rc": -2,
    "c": -2,
    "pre": -2,
    "preview": -2,
    "post": 1,
}


def _parse_version_parts(version: str) -> tuple[tuple[int, ...], tuple[tuple[int, int, str], ...]]:
    """Split a version string into numeric release tuple and prerelease/post tokens."""
    cleaned = version.strip().lstrip("vV")
    # Strip build metadata (+...)
    cleaned = cleaned.split("+", 1)[0]
    tokens = _TOKEN_RE.findall(cleaned)
    release: list[int] = []
    suffix: list[tuple[int, int, str]] = []
    in_suffix = False
    for tok in tokens:
        if tok.isdigit() and not in_suffix:
            release.append(int(tok))
        else:
            in_suffix = True
            if tok.isdigit():
                suffix.append((0, int(tok), ""))
            else:
                low = tok.lower()
                order = _PRERELEASE_ORDER.get(low, -1)
                suffix.append((order, 0, low))
    # Trim trailing zeros on release so 1.2 == 1.2.0
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    return tuple(release), tuple(suffix)


def version_sort_key(version: str) -> tuple:
    """Sort key so 4.2.9 < 4.2.10 and 1.0.0rc1 < 1.0.0."""
    release, suffix = _parse_version_parts(version)
    if not suffix:
        # Final release sorts after prereleases (order < 0) and before post releases (order > 0)
        stage = 0
    else:
        stage = -1 if suffix[0][0] < 0 else (1 if suffix[0][0] > 0 else 0)
    return (release, stage, suffix, version)


def compare_versions(a: str, b: str) -> int:
    """Compare two version strings: -1 if a < b, 0 if equal, 1 if a > b."""
    ra, sa = _parse_version_parts(a)
    rb, sb = _parse_version_parts(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if sa == sb:
        return 0
    stage_a = 0 if not sa else (-1 if sa[0][0] < 0 else (1 if sa[0][0] > 0 else 0))
    stage_b = 0 if not sb else (-1 if sb[0][0] < 0 else (1 if sb[0][0] > 0 else 0))
    if stage_a != stage_b:
        return -1 if stage_a < stage_b else 1
    return -1 if sa < sb else 1


def version_in_range(
    version: str,
    *,
    at_or_above: str | None = None,
    above: str | None = None,
    below: str | None = None,
    at_or_below: str | None = None,
) -> bool:
    """Return True if version falls inside the given bounds."""
    if at_or_above and compare_versions(version, at_or_above) < 0:
        return False
    if above and compare_versions(version, above) <= 0:
        return False
    if below and compare_versions(version, below) >= 0:
        return False
    if at_or_below and compare_versions(version, at_or_below) > 0:
        return False
    return True


def _cvss_roundup(value: float) -> float:
    """Official CVSS v3.1 Roundup function (nearest tenth, rounding up)."""
    int_input = round(value * 100_000)
    if int_input % 10_000 == 0:
        return int_input / 100_000.0
    return (math.floor(int_input / 10_000) + 1) / 10.0


def cvss_score_bucket(score: float | int | None) -> str:
    """Map a numeric CVSS score (0.0-10.0) to a Sentinel severity bucket."""
    if score is None:
        return "UNKNOWN"
    try:
        val = float(score)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if val >= 9.0:
        return "CRITICAL"
    if val >= 7.0:
        return "HIGH"
    if val >= 4.0:
        return "MODERATE"
    if val > 0.0:
        return "LOW"
    return "UNKNOWN"


def cvss3_bucket(vector: str) -> str:
    """Compute CVSS v3.0/v3.1 base score from vector and return its severity bucket."""
    try:
        parts = vector.split("/")[1:]
        w = dict(part.split(":", 1) for part in parts)
        changed = w["S"] == "C"
        av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}[w["AV"]]
        ac = {"L": 0.77, "H": 0.44}[w["AC"]]
        pr = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[w["PR"]]
        ui = {"N": 0.85, "R": 0.62}[w["UI"]]
        cia = {"H": 0.56, "L": 0.22, "N": 0.0}
        iss = 1 - (1 - cia[w["C"]]) * (1 - cia[w["I"]]) * (1 - cia[w["A"]])
    except (KeyError, ValueError, IndexError):
        return "UNKNOWN"
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    if impact <= 0:
        return "UNKNOWN"
    raw = min((1.08 if changed else 1.0) * (impact + 8.22 * av * ac * pr * ui), 10.0)
    return cvss_score_bucket(_cvss_roundup(raw))


def cvss4_bucket(vector: str) -> str:
    """Estimate severity bucket from a CVSS v4.0 vector string."""
    try:
        w = dict(part.split(":", 1) for part in vector.split("/")[1:])
        vc, vi, va = w["VC"], w["VI"], w["VA"]
        sc, si, sa = w.get("SC", "N"), w.get("SI", "N"), w.get("SA", "N")
        if all(x == "N" for x in (vc, vi, va, sc, si, sa)):
            return "UNKNOWN"
        av = {"N": 1.0, "A": 0.85, "L": 0.7, "P": 0.5}[w["AV"]]
        ac = {"L": 1.0, "H": 0.8}[w["AC"]]
        at = {"N": 1.0, "P": 0.85}.get(w.get("AT", "N"), 0.85)
        pr = {"N": 1.0, "L": 0.85, "H": 0.7}[w["PR"]]
        ui = {"N": 1.0, "P": 0.85, "A": 0.75}.get(w["UI"], 0.85)
        imp_map = {"H": 1.0, "L": 0.5, "N": 0.0}
        vuln_imp = max(imp_map[vc], imp_map[vi], imp_map[va])
        sub_imp = max(imp_map[sc], imp_map[si], imp_map[sa])
        base = (0.7 * vuln_imp + 0.3 * sub_imp) * 10.0 * av * ac * at * pr * ui
        return cvss_score_bucket(min(base, 10.0))
    except (KeyError, ValueError, IndexError):
        return "UNKNOWN"


def cvss_vector_bucket(vector: str) -> str:
    """Return severity bucket for CVSS v3.x or v4.0 vectors."""
    if not vector:
        return "UNKNOWN"
    if vector.startswith("CVSS:3"):
        return cvss3_bucket(vector)
    if vector.startswith("CVSS:4"):
        return cvss4_bucket(vector)
    return "UNKNOWN"
