"""Look packages up and shape the results for the dashboard.

Connected now: OSV.dev (PyPI + npm). NVD, CVE.org, CISA KEV, RetireJS and
the news feeds are added in later steps of the spec; until then they are
reported as "not connected yet" so the page never implies a check that
did not happen.
"""
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .logos import logo_for

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{}"
UA = {"User-Agent": "sentinel/0.1", "Content-Type": "application/json"}
SEVERITIES = ["CRITICAL", "HIGH", "MODERATE", "LOW", "UNKNOWN"]
RANK = {s: 4 - i for i, s in enumerate(SEVERITIES)}
PENDING_SOURCES = ["NVD", "CVE.org", "CISA KEV", "RetireJS", "News feeds"]


def _get_json(url, body=None, timeout=20):
    req = urllib.request.Request(url, headers=UA,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def cvss3_bucket(vector):
    """Approximate a CVSS v3 base score from its vector and bucket it."""
    try:
        w = dict(part.split(":") for part in vector.split("/")[1:])
        changed = w["S"] == "C"
        av = {"N": .85, "A": .62, "L": .55, "P": .2}[w["AV"]]
        ac = {"L": .77, "H": .44}[w["AC"]]
        pr = {"N": .85, "L": .68 if changed else .62, "H": .5 if changed else .27}[w["PR"]]
        ui = {"N": .85, "R": .62}[w["UI"]]
        cia = {"H": .56, "L": .22, "N": 0}
        iss = 1 - (1 - cia[w["C"]]) * (1 - cia[w["I"]]) * (1 - cia[w["A"]])
    except (KeyError, ValueError):
        return "UNKNOWN"
    impact = 7.52 * (iss - .029) - 3.25 * (iss - .02) ** 15 if changed else 6.42 * iss
    if impact <= 0:
        return "UNKNOWN"
    score = min((1.08 if changed else 1) * (impact + 8.22 * av * ac * pr * ui), 10)
    return "CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MODERATE" if score >= 4 else "LOW"


def _severity(v):
    sev = ((v.get("database_specific") or {}).get("severity") or "").upper()
    if sev == "MEDIUM":
        sev = "MODERATE"
    if sev in RANK:
        return sev
    for s in v.get("severity", []):
        if s.get("score", "").startswith("CVSS:3"):
            return cvss3_bucket(s["score"])
    return "UNKNOWN"


def _fixed(v, ecosystem, name):
    fixes = []
    for a in v.get("affected", []):
        pkg = a.get("package", {})
        if pkg.get("ecosystem") == ecosystem and pkg.get("name", "").lower() == name.lower():
            for r in a.get("ranges", []):
                fixes += [e["fixed"] for e in r.get("events", []) if "fixed" in e]
    return sorted(set(fixes))


def query_osv(packages):
    """Return {package.key + version: [vuln, ...]} for PyPI and npm packages."""
    pkgs = [p for p in packages if p.ecosystem in ("PyPI", "npm")]
    ids_by_pkg = {}
    for i in range(0, len(pkgs), 500):
        chunk = pkgs[i:i + 500]
        queries = [{"package": {"name": p.name, "ecosystem": p.ecosystem},
                    **({"version": p.version} if p.version else {})} for p in chunk]
        resp = _get_json(OSV_BATCH, {"queries": queries}, timeout=30)
        for p, r in zip(chunk, resp["results"]):
            ids_by_pkg[(p.key, p.version)] = [x["id"] for x in r.get("vulns", [])]
    ids = sorted({i for v in ids_by_pkg.values() for i in v})

    def detail(vid):
        try:
            return vid, _get_json(OSV_VULN.format(vid))
        except Exception:
            return vid, None
    with ThreadPoolExecutor(8) as ex:
        details = dict(ex.map(detail, ids))

    out = {}
    for p in pkgs:
        rows = []
        for vid in ids_by_pkg.get((p.key, p.version), []):
            v = details.get(vid) or {}
            rows.append({
                "id": vid,
                "aliases": v.get("aliases", []),
                "summary": v.get("summary") or (v.get("details") or "Details unavailable")[:200],
                "severity": _severity(v) if v else "UNKNOWN",
                "fixed": _fixed(v, p.ecosystem, p.name),
                "published": (v.get("published") or "")[:10],
                "url": f"https://osv.dev/vulnerability/{vid}",
                "sources": ["OSV.dev"],
                "exploited": False,
            })
        out[(p.key, p.version)] = rows
    return out


def run_scan(packages):
    sources = {}
    try:
        vulns = query_osv(packages)
        sources["OSV.dev"] = "ok"
    except Exception:
        vulns = {}
        sources["OSV.dev"] = "unreachable"
    for name in PENDING_SOURCES:
        sources[name] = "not connected yet"
    return build_result(packages, vulns, sources, news=[])


def build_result(packages, vulns, sources, news, sample=False):
    """Shape everything the dashboard and the JSON download need."""
    rows = []
    for p in packages:
        vs = sorted(vulns.get((p.key, p.version), []),
                    key=lambda v: (not v["exploited"], -RANK[v["severity"]]))
        for v in vs:
            v["cve"] = next((a for a in [v["id"], *v["aliases"]] if a.startswith("CVE-")), v["id"])
        logo, own = logo_for(p.ecosystem, p.name)
        worst = vs[0]["severity"] if vs else None
        pkg_news = [n for n in news if p.name.lower() in [m.lower() for m in n["matches"]]]
        rows.append({
            "ecosystem": p.ecosystem, "name": p.name, "version": p.version, "spec": p.spec,
            "exact": bool(p.version), "logo": logo, "own_logo": own,
            "vulns": vs, "worst": worst, "worst_class": (worst or "clean").lower(),
            "exploited": any(v["exploited"] for v in vs),
            "rank": (any(v["exploited"] for v in vs), RANK[worst] if worst else -1, len(vs)),
            "news": pkg_news,
        })
    rows.sort(key=lambda r: tuple(-x for x in r["rank"][:2]) + (-r["rank"][2], r["name"].lower()))
    for r in rows:
        r.pop("rank")

    all_v = [v for r in rows for v in r["vulns"]]
    counts = {s: sum(v["severity"] == s for v in all_v) for s in SEVERITIES}
    peak = max(counts.values(), default=0) or 1
    vulnerable = [r for r in rows if r["vulns"]]
    fixable = sum(bool(v["fixed"]) for v in all_v)
    return {
        "generated": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        "sample": sample,
        "packages": rows,
        "vulnerable": vulnerable,
        "clean": [r for r in rows if not r["vulns"]],
        "news": news,
        "sources": [{"name": k, "status": v} for k, v in sources.items()],
        "stats": {
            "packages": len(rows),
            "vulnerable": len(vulnerable),
            "exploited": sum(r["exploited"] for r in rows),
            "critical_high": counts["CRITICAL"] + counts["HIGH"],
            "news": len(news),
            "problems": len(all_v),
            "fixable": fixable,
            "clean_pct": round(100 * (len(rows) - len(vulnerable)) / len(rows)) if rows else 0,
            "fixable_pct": round(100 * fixable / len(all_v)) if all_v else 100,
        },
        "bars": [{"label": s.title(), "cls": s.lower(), "count": counts[s],
                  "pct": max(2, round(100 * counts[s] / peak))} for s in SEVERITIES[:4]],
    }
