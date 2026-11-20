"""Vulnerability data sources: OSV.dev, CISA KEV, CVE.org, NVD 2.0, and RetireJS."""
from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from django.conf import settings

from .http_client import TTL_CATALOG, TTL_VULN, cached_fetch, fetch_json
from .versions import (
    RANK,
    cvss_score_bucket,
    cvss_vector_bucket,
    version_in_range,
    version_sort_key,
)

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{}"
CISA_KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
CVE_ORG_URL = "https://cveawg.mitre.org/api/cve/{}"
NVD_API_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
RETIREJS_URL = "https://raw.githubusercontent.com/RetireJS/retire.js/master/repository/jsrepository-v4.json"

# Fixed CPE 2.3 vendor:product pairs for "Other software" to avoid CPE false positives.
OTHER_CPES = {
    "postgresql": ("postgresql", "postgresql"),
    "postgres": ("postgresql", "postgresql"),
    "postgis": ("postgis", "postgis"),
    "redis": ("redis", "redis"),
    "nginx": ("f5", "nginx"),
    "sqlite": ("sqlite", "sqlite"),
    "mysql": ("oracle", "mysql"),
    "mariadb": ("mariadb", "mariadb"),
}


def _normalise_severity(raw: str | None) -> str:
    sev = (raw or "").strip().upper()
    if sev == "MEDIUM":
        return "MODERATE"
    return sev if sev in RANK else "UNKNOWN"


def _osv_severity(vuln: dict[str, Any]) -> str:
    sev = _normalise_severity((vuln.get("database_specific") or {}).get("severity"))
    if sev != "UNKNOWN":
        return sev
    for item in vuln.get("severity", []):
        bucket = cvss_vector_bucket(item.get("score", ""))
        if bucket != "UNKNOWN":
            return bucket
    return "UNKNOWN"


def _osv_fixed(vuln: dict[str, Any], ecosystem: str, name: str) -> list[str]:
    fixes: set[str] = set()
    for affected in vuln.get("affected", []):
        pkg = affected.get("package", {})
        if pkg.get("ecosystem") == ecosystem and pkg.get("name", "").lower() == name.lower():
            for rng in affected.get("ranges", []):
                for event in rng.get("events", []):
                    if "fixed" in event:
                        fixes.add(event["fixed"])
    return sorted(fixes, key=version_sort_key)


def _fetch_osv_detail(vuln_id: str) -> dict[str, Any] | None:
    try:
        return cached_fetch(
            "osv_vuln",
            vuln_id,
            TTL_VULN,
            lambda: fetch_json(OSV_VULN.format(urllib.parse.quote(vuln_id, safe=""))),
        )
    except Exception:
        return None


def query_osv(packages: list[Any], fetcher=None) -> dict[tuple, list[dict[str, Any]]]:
    """Return {(pkg.key, pkg.version): [vuln, ...]} for PyPI and npm packages."""
    json_get = fetcher or (lambda url, body=None, timeout=20: fetch_json(url, body=body, timeout=timeout))
    pkgs = [p for p in packages if p.ecosystem in ("PyPI", "npm")]
    if not pkgs:
        return {}

    ids_by_pkg: dict[tuple, list[str]] = {}
    for i in range(0, len(pkgs), 500):
        chunk = pkgs[i:i + 500]
        queries = [
            {
                "package": {"name": p.name, "ecosystem": p.ecosystem},
                **({"version": p.version} if p.version else {}),
            }
            for p in chunk
        ]
        resp = json_get(OSV_BATCH, body={"queries": queries}, timeout=10)
        for pkg, result in zip(chunk, resp.get("results", [])):
            ids_by_pkg[(pkg.key, pkg.version)] = [x["id"] for x in result.get("vulns", [])]

    unique_ids = sorted({vid for vids in ids_by_pkg.values() for vid in vids})

    def detail(vid: str) -> tuple[str, dict[str, Any] | None]:
        try:
            url = OSV_VULN.format(urllib.parse.quote(vid, safe=""))
            return vid, cached_fetch("osv_vuln", vid, TTL_VULN, lambda: json_get(url, timeout=5))
        except Exception:
            return vid, None

    with ThreadPoolExecutor(max_workers=16) as pool:
        details = dict(pool.map(detail, unique_ids))

    out: dict[tuple, list[dict[str, Any]]] = {}
    for pkg in pkgs:
        rows = []
        for vid in ids_by_pkg.get((pkg.key, pkg.version), []):
            v = details.get(vid) or {}
            rows.append({
                "id": vid,
                "aliases": list(v.get("aliases", [])),
                "summary": v.get("summary") or (v.get("details") or "Details unavailable")[:200],
                "severity": _osv_severity(v) if v else "UNKNOWN",
                "fixed": _osv_fixed(v, pkg.ecosystem, pkg.name),
                "published": (v.get("published") or "")[:10],
                "url": f"https://osv.dev/vulnerability/{vid}",
                "sources": ["OSV.dev"],
                "exploited": False,
            })
        out[(pkg.key, pkg.version)] = rows
    return out


# --------------------------------------------------------------------------- CISA KEV
def fetch_cisa_kev() -> set[str]:
    """Return the set of actively exploited CVE IDs from CISA KEV (cached 24h)."""
    data = cached_fetch("cisa_kev", "catalog", TTL_CATALOG, lambda: fetch_json(CISA_KEV_URL, timeout=6))
    return {
        item["cveID"].upper()
        for item in data.get("vulnerabilities", [])
        if item.get("cveID")
    }


# --------------------------------------------------------------------------- CVE.org
def _extract_cve_org_severity(record: dict[str, Any]) -> str:
    containers = record.get("containers") or {}
    metric_blocks: list[dict[str, Any]] = []
    cna = containers.get("cna") or {}
    metric_blocks.extend(cna.get("metrics") or [])
    for adp in containers.get("adp") or []:
        metric_blocks.extend(adp.get("metrics") or [])

    for key in ("cvssV4_0", "cvssV3_1", "cvssV3_0", "cvssV2_0"):
        for block in metric_blocks:
            cvss = block.get(key)
            if not isinstance(cvss, dict):
                continue
            sev = _normalise_severity(cvss.get("baseSeverity"))
            if sev != "UNKNOWN":
                return sev
            bucket = cvss_score_bucket(cvss.get("baseScore"))
            if bucket != "UNKNOWN":
                return bucket
            bucket = cvss_vector_bucket(cvss.get("vectorString", ""))
            if bucket != "UNKNOWN":
                return bucket
    return "UNKNOWN"


def fetch_cve_org_severity(cve_id: str) -> str:
    """Look up a CVE record on cve.org and return its severity bucket (cached 24h)."""
    if not cve_id.startswith("CVE-"):
        return "UNKNOWN"
    record = cached_fetch(
        "cve_org",
        cve_id,
        TTL_VULN,
        lambda: fetch_json(CVE_ORG_URL.format(urllib.parse.quote(cve_id, safe="")), timeout=4),
    )
    return _extract_cve_org_severity(record)


# --------------------------------------------------------------------------- NVD API 2.0
def _nvd_headers() -> dict[str, str]:
    api_key = getattr(settings, "NVD_API_KEY", "") or ""
    return {"apiKey": api_key} if api_key else {}


def _extract_nvd_severity(cve: dict[str, Any]) -> str:
    metrics = cve.get("metrics") or {}
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        for entry in metrics.get(key) or []:
            data = entry.get("cvssData") or {}
            sev = _normalise_severity(data.get("baseSeverity") or entry.get("baseSeverity"))
            if sev != "UNKNOWN":
                return sev
            bucket = cvss_score_bucket(data.get("baseScore"))
            if bucket != "UNKNOWN":
                return bucket
    return "UNKNOWN"


def _nvd_summary(cve: dict[str, Any]) -> str:
    for desc in cve.get("descriptions") or []:
        if desc.get("lang") == "en" and desc.get("value"):
            return desc["value"][:240]
    return "Details unavailable"


def _nvd_cpe_matches_version(cve: dict[str, Any], vendor: str, product: str, version: str | None) -> tuple[bool, list[str]]:
    """Check if an NVD CVE matches vendor:product and version; also extract fixed versions."""
    prefix = f"cpe:2.3:a:{vendor}:{product}:"
    matched = False
    fixes: set[str] = set()
    for config in cve.get("configurations") or []:
        for node in config.get("nodes") or []:
            for match in node.get("cpeMatch") or []:
                if not match.get("vulnerable", True):
                    continue
                criteria = (match.get("criteria") or "").lower()
                if not criteria.startswith(prefix):
                    continue
                if match.get("versionEndExcluding"):
                    fixes.add(match["versionEndExcluding"])
                if version is None:
                    matched = True
                    continue
                parts = criteria.split(":")
                cpe_ver = parts[5] if len(parts) > 5 else "*"
                if cpe_ver not in ("*", "-"):
                    if cpe_ver == version:
                        matched = True
                    continue
                if version_in_range(
                    version,
                    at_or_above=match.get("versionStartIncluding"),
                    above=match.get("versionStartExcluding"),
                    below=match.get("versionEndExcluding"),
                    at_or_below=match.get("versionEndIncluding"),
                ):
                    matched = True
    return matched, sorted(fixes, key=version_sort_key)


def query_nvd_other(packages: list[Any]) -> dict[tuple, list[dict[str, Any]]]:
    """Query NVD API 2.0 in parallel for 'Other' software packages with known CPE mappings."""
    other_pkgs = [p for p in packages if p.ecosystem == "Other" and p.name.lower() in OTHER_CPES]
    if not other_pkgs:
        return {}

    headers = _nvd_headers()

    def _query_one(pkg: Any) -> tuple[tuple, list[dict[str, Any]]]:
        vendor, product = OTHER_CPES[pkg.name.lower()]
        cpe = f"cpe:2.3:a:{vendor}:{product}:{pkg.version or '*'}:*:*:*:*:*:*:*"
        params = urllib.parse.urlencode({"virtualMatchString": cpe, "resultsPerPage": "50"})
        url = f"{NVD_API_URL}?{params}"
        data = cached_fetch(
            "nvd_cpe",
            cpe,
            TTL_VULN,
            lambda u=url: fetch_json(u, headers=headers, timeout=8),
        )
        rows = []
        for item in data.get("vulnerabilities") or []:
            cve = item.get("cve") or {}
            cve_id = cve.get("id")
            if not cve_id:
                continue
            matched, fixes = _nvd_cpe_matches_version(cve, vendor, product, pkg.version)
            if not matched:
                continue
            rows.append({
                "id": cve_id,
                "aliases": [],
                "summary": _nvd_summary(cve),
                "severity": _extract_nvd_severity(cve),
                "fixed": fixes,
                "published": (cve.get("published") or "")[:10],
                "url": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
                "sources": ["NVD"],
                "exploited": False,
            })
        return (pkg.key, pkg.version), rows

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(_query_one, other_pkgs))


def fetch_nvd_severity(cve_id: str) -> str:
    """Look up a single CVE on NVD API 2.0 and return its severity bucket (cached 24h)."""
    if not cve_id.startswith("CVE-"):
        return "UNKNOWN"
    params = urllib.parse.urlencode({"cveId": cve_id})
    url = f"{NVD_API_URL}?{params}"
    headers = _nvd_headers()
    data = cached_fetch(
        "nvd_cve",
        cve_id,
        TTL_VULN,
        lambda: fetch_json(url, headers=headers, timeout=5),
    )
    vulns = data.get("vulnerabilities") or []
    if not vulns:
        return "UNKNOWN"
    return _extract_nvd_severity(vulns[0].get("cve") or {})


# --------------------------------------------------------------------------- RetireJS
def fetch_retirejs_repo() -> dict[str, Any]:
    """Fetch and cache the RetireJS vulnerability repository (24h TTL)."""
    return cached_fetch("retirejs", "v4", TTL_CATALOG, lambda: fetch_json(RETIREJS_URL, timeout=6))


def query_retirejs(packages: list[Any], repo: dict[str, Any] | None = None) -> dict[tuple, list[dict[str, Any]]]:
    """Match npm packages against the RetireJS repository."""
    npm_pkgs = [p for p in packages if p.ecosystem == "npm"]
    if not npm_pkgs:
        return {}
    catalog = repo if repo is not None else fetch_retirejs_repo()
    by_name = {k.lower(): v for k, v in catalog.items() if isinstance(v, dict)}

    out: dict[tuple, list[dict[str, Any]]] = {}
    for pkg in npm_pkgs:
        entry = by_name.get(pkg.name.lower())
        if not entry:
            continue
        rows = []
        for idx, vuln in enumerate(entry.get("vulnerabilities") or []):
            at_or_above = vuln.get("atOrAbove")
            below = vuln.get("below")
            if pkg.version:
                if not version_in_range(pkg.version, at_or_above=at_or_above, below=below):
                    continue
            identifiers = vuln.get("identifiers") or {}
            cves = [c for c in (identifiers.get("CVE") or []) if isinstance(c, str)]
            vid = cves[0] if cves else f"RETIREJS-{pkg.name}-{idx}"
            aliases = cves[1:] if len(cves) > 1 else []
            summary = identifiers.get("summary") or f"RetireJS advisory for {pkg.name}"
            info_urls = vuln.get("info") or []
            url = info_urls[0] if info_urls else f"https://nvd.nist.gov/vuln/detail/{vid}"
            rows.append({
                "id": vid,
                "aliases": aliases,
                "summary": summary,
                "severity": _normalise_severity(vuln.get("severity")),
                "fixed": [below] if below else [],
                "published": "",
                "url": url,
                "sources": ["RetireJS"],
                "exploited": False,
            })
        if rows:
            out[(pkg.key, pkg.version)] = rows
    return out
