"""Orchestrate vulnerability lookups, enrichment, deduplication, and dashboard shaping."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any

from django.conf import settings

from .http_client import fetch_json, stale_since
from .logos import logo_for
from .news import fetch_all_rss_stories, match_news, story_kind
from .sources import (
    OTHER_CPES,
    fetch_cisa_kev,
    fetch_cve_org_severity,
    fetch_nvd_severity,
    query_nvd_other,
    query_osv,
    query_retirejs,
)
from .triage import could_affect, fixed_within, group_by_fix, is_malware, sort_key, spec_bounds, versions_in_range
from .versions import RANK, SEVERITIES, cvss3_bucket, version_sort_key

# Kept as module-level helper so tests can easily mock HTTP JSON calls.
def _get_json(url: str, body: dict[str, Any] | None = None, timeout: int = 20) -> Any:
    return fetch_json(url, body=body, timeout=timeout)


def _canonical_cve(vuln: dict[str, Any]) -> str:
    """Return the first CVE-* identifier in [cve, id, *aliases], or fall back to id."""
    for candidate in [vuln.get("cve"), vuln["id"], *vuln.get("aliases", [])]:
        if isinstance(candidate, str) and candidate.upper().startswith("CVE-"):
            return candidate.upper()
    return vuln.get("cve") or vuln["id"]


# Sources that only flag or enrich a finding. They carry no advisory of their own, so their
# "evidence" is the CVE id (KEV) or the severity they supplied.
_FLAG_SOURCES = {"CISA KEV"}


def _evidence_of(vuln: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per source that backs this finding: what that source called it and how severe."""
    if vuln.get("evidence"):
        return [dict(e) for e in vuln["evidence"]]
    out = []
    for src in vuln.get("sources") or []:
        flag = src in _FLAG_SOURCES
        out.append({
            "source": src,
            "id": _canonical_cve(vuln) if flag else vuln["id"],
            "severity": None if flag else vuln["severity"],
            "url": "" if flag else vuln.get("url", ""),
        })
    return out


def _union_evidence(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {(e["source"], e["id"]) for e in a}
    return [*a, *(e for e in b if (e["source"], e["id"]) not in seen)]


def _finish_evidence(vuln: dict[str, Any]) -> None:
    """Corroboration (how many independent sources) and whether they disagree on severity."""
    evidence = vuln["evidence"]
    vuln["corroboration"] = len({e["source"] for e in evidence})
    rated = {e["severity"] for e in evidence if e["severity"] and e["severity"] != "UNKNOWN"}
    vuln["disputed"] = len(rated) > 1


def _merge_package_vulns(raw_vulns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate advisories for one package by canonical CVE ID / overlapping aliases."""
    merged: list[dict[str, Any]] = []
    index_by_id: dict[str, int] = {}

    for vuln in raw_vulns:
        cve = _canonical_cve(vuln)
        ids = {vuln["id"], cve, *(vuln.get("aliases") or [])}
        existing_idx = next((index_by_id[i] for i in ids if i in index_by_id), None)

        if existing_idx is None:
            copy = {
                **vuln,
                "cve": cve,
                "aliases": sorted({a for a in (vuln.get("aliases") or []) if a != cve}),
                "fixed": sorted(set(vuln.get("fixed") or []), key=version_sort_key),
                "sources": list(dict.fromkeys(vuln.get("sources") or [])),
                "exploited": bool(vuln.get("exploited", False)),
                "evidence": _evidence_of(vuln),
            }
            if copy["id"] != cve and copy["id"] not in copy["aliases"]:
                copy["aliases"].append(copy["id"])
            idx = len(merged)
            merged.append(copy)
            for i in ids:
                index_by_id[i] = idx
        else:
            target = merged[existing_idx]
            if RANK.get(vuln["severity"], 0) > RANK.get(target["severity"], 0):
                target["severity"] = vuln["severity"]
            target["exploited"] = target["exploited"] or bool(vuln.get("exploited", False))
            target["sources"] = list(dict.fromkeys([*target["sources"], *(vuln.get("sources") or [])]))
            target["evidence"] = _union_evidence(target["evidence"], _evidence_of(vuln))
            all_fixes = set(target["fixed"]) | set(vuln.get("fixed") or [])
            target["fixed"] = sorted(all_fixes, key=version_sort_key)
            all_intro = set(target.get("introduced") or []) | set(vuln.get("introduced") or [])
            target["introduced"] = sorted(all_intro, key=version_sort_key)
            all_vers = set(target.get("versions") or []) | set(vuln.get("versions") or [])
            target["versions"] = sorted(all_vers, key=version_sort_key)
            all_aliases = set(target["aliases"]) | set(vuln.get("aliases") or []) | {vuln["id"]}
            all_aliases.discard(target["cve"])
            target["aliases"] = sorted(all_aliases)
            if not target["published"] and vuln.get("published"):
                target["published"] = vuln["published"]
            if target["summary"] == "Details unavailable" and vuln.get("summary"):
                target["summary"] = vuln["summary"]
            for i in ids:
                index_by_id[i] = existing_idx

    for row in merged:
        _finish_evidence(row)
    return merged


def _enrich_vulns(
    vulns_by_pkg: dict[tuple, list[dict[str, Any]]],
    kev_ids: set[str],
    sources: dict[str, str],
) -> None:
    """Apply CISA KEV flags and fill UNKNOWN severities via CVE.org -> NVD."""
    unknown_cves: set[str] = set()
    for rows in vulns_by_pkg.values():
        for vuln in rows:
            all_ids = {vuln["id"].upper(), *(a.upper() for a in vuln.get("aliases", []))}
            if all_ids & kev_ids:
                vuln["exploited"] = True
                if "CISA KEV" not in vuln["sources"]:
                    vuln["sources"].append("CISA KEV")
            if vuln["severity"] == "UNKNOWN":
                cve = _canonical_cve(vuln)
                if cve.startswith("CVE-"):
                    unknown_cves.add(cve)

    if not unknown_cves:
        sources.setdefault("CVE.org", "ok")
        return

    cve_targets = sorted(unknown_cves)[:10]
    enriched: dict[str, tuple[str, str]] = {}
    cve_org_ok = False
    cve_org_failed = False
    has_nvd_key = bool(getattr(settings, "NVD_API_KEY", ""))

    def enrich_one(cve_id: str) -> tuple[str, str | None, str | None]:
        nonlocal cve_org_ok, cve_org_failed
        try:
            sev = fetch_cve_org_severity(cve_id)
            cve_org_ok = True
            if sev != "UNKNOWN":
                return cve_id, sev, "CVE.org"
        except Exception:
            cve_org_failed = True
        if has_nvd_key:
            try:
                sev = fetch_nvd_severity(cve_id)
                if sev != "UNKNOWN":
                    return cve_id, sev, "NVD"
            except Exception:
                pass
        return cve_id, None, None

    pool = ThreadPoolExecutor(max_workers=6)
    try:
        futures = [pool.submit(enrich_one, cve_id) for cve_id in cve_targets]
        done, _ = wait(futures, timeout=4.5)
        for fut in done:
            try:
                cve_id, sev, src = fut.result()
                if sev and src:
                    enriched[cve_id] = (sev, src)
            except Exception:
                pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    sources["CVE.org"] = "ok" if cve_org_ok or not cve_org_failed else "unreachable"

    for rows in vulns_by_pkg.values():
        for vuln in rows:
            if vuln["severity"] == "UNKNOWN":
                cve = _canonical_cve(vuln)
                if cve in enriched:
                    sev, src = enriched[cve]
                    vuln["severity"] = sev
                    if src not in vuln["sources"]:
                        vuln["sources"].append(src)


def run_scan(packages: list[Any]) -> dict[str, Any]:
    """Run all connected vulnerability sources in parallel with a tight latency budget."""
    started = time.monotonic()
    started_wall = time.time()
    sources: dict[str, str] = {}
    vulns_by_pkg: dict[tuple, list[dict[str, Any]]] = {}

    has_osv = any(p.ecosystem in ("PyPI", "npm") for p in packages)
    has_npm = any(p.ecosystem == "npm" for p in packages)
    has_other_supported = any(p.ecosystem == "Other" and p.name.lower() in OTHER_CPES for p in packages)

    def _run_osv() -> tuple[str, dict[tuple, list[dict[str, Any]]]]:
        if not has_osv:
            return "ok", {}
        try:
            return "ok", query_osv(packages, fetcher=_get_json)
        except Exception:
            return "unreachable", {}

    def _run_kev() -> tuple[str, set[str]]:
        try:
            return "ok", fetch_cisa_kev()
        except Exception:
            return "unreachable", set()

    def _run_retirejs() -> tuple[str, dict[tuple, list[dict[str, Any]]]]:
        if not has_npm:
            return "ok", {}
        try:
            return "ok", query_retirejs(packages)
        except Exception:
            return "unreachable", {}

    def _run_nvd() -> tuple[str, dict[tuple, list[dict[str, Any]]]]:
        if not has_other_supported:
            return "ok", {}
        try:
            return "ok", query_nvd_other(packages)
        except Exception:
            return "unreachable", {}

    def _run_rss() -> tuple[list[dict[str, Any]], bool]:
        try:
            return fetch_all_rss_stories(days=30)
        except Exception:
            return [], False

    pool = ThreadPoolExecutor(max_workers=5)
    try:
        f_osv = pool.submit(_run_osv)
        f_kev = pool.submit(_run_kev)
        f_retire = pool.submit(_run_retirejs)
        f_nvd = pool.submit(_run_nvd)
        f_rss = pool.submit(_run_rss)

        # A ceiling, not a delay: wait() returns the moment every source has answered. A source
        # still running at the deadline is reported as "timeout" (slow), not "unreachable" (down).
        wait([f_osv, f_kev, f_retire, f_nvd, f_rss], timeout=float(getattr(settings, "SENTINEL_SCAN_DEADLINE", 25)))

        sources["OSV.dev"], osv_vulns = f_osv.result() if f_osv.done() else ("timeout", {})
        sources["CISA KEV"], kev_ids = f_kev.result() if f_kev.done() else ("timeout", set())
        sources["RetireJS"], retire_vulns = f_retire.result() if f_retire.done() else ("timeout", {})
        sources["NVD"], nvd_vulns = f_nvd.result() if f_nvd.done() else ("timeout", {})
        preloaded_rss = f_rss.result() if f_rss.done() else ([], False)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    for source_map in (osv_vulns, retire_vulns, nvd_vulns):
        for key, rows in source_map.items():
            vulns_by_pkg.setdefault(key, []).extend(rows)

    _enrich_vulns(vulns_by_pkg, kev_ids, sources)

    # Deduplicate per package before matching news so CVE IDs are canonical.
    for key, rows in list(vulns_by_pkg.items()):
        vulns_by_pkg[key] = _merge_package_vulns(rows)

    all_cves = {
        v["cve"]
        for rows in vulns_by_pkg.values()
        for v in rows
        if v["cve"].startswith("CVE-")
    }
    vuln_pkg_names = [
        p.name for p in packages if vulns_by_pkg.get((p.key, p.version))
    ]
    include_hn = (time.monotonic() - started) < 4.5
    try:
        news, news_status = match_news(
            packages,
            all_cves,
            days=30,
            include_hn=include_hn,
            preloaded_rss=preloaded_rss,
            priority_names=vuln_pkg_names,
        )
        sources["News feeds"] = news_status
    except Exception:
        news = []
        sources["News feeds"] = "unreachable"

    return build_result(packages, vulns_by_pkg, sources, news=news, stale=stale_since(started_wall))


def _why(sources: dict[str, str], *names: str) -> str:
    """'X could not be reached' or 'X timed out', depending on how the sources failed."""
    timed_out = all(sources.get(n) == "timeout" for n in names)
    joined = " and ".join(names)
    return f"{joined} timed out" if timed_out else f"{joined} could not be reached"


def unchecked_reason(package: Any, sources: dict[str, str]) -> str | None:
    """Return why a package could not be checked, or None when it was checked."""
    eco = package.ecosystem
    if eco == "PyPI":
        return None if sources.get("OSV.dev") == "ok" else _why(sources, "OSV.dev")
    if eco == "npm":
        if sources.get("OSV.dev") == "ok" or sources.get("RetireJS") == "ok":
            return None
        return _why(sources, "OSV.dev", "RetireJS")
    if package.name.lower() not in OTHER_CPES:
        return "no CPE mapping for this software yet"
    return None if sources.get("NVD") == "ok" else _why(sources, "NVD")


def split_groups(rows):
    """(vulnerable, clean, not-checked) package rows. Derived, so it is never stored.

    A package with findings is always listed as vulnerable, even if one of its sources failed:
    what the working sources found must never be hidden behind the failure of another. It is
    flagged "partly checked" instead. "Not checked" holds packages with no findings at all.
    """
    vulnerable = [r for r in rows if r["vulns"]]
    clean = [r for r in rows if r["checked"] and not r["vulns"]]
    unchecked = [r for r in rows if not r["checked"] and not r["vulns"]]
    return vulnerable, clean, unchecked


# Which saved-copy lookups belong to which source, for the "showing saved data" notice.
STALE_SOURCE = {
    "osv_pkg": "OSV.dev", "osv_vuln": "OSV.dev", "cisa_kev": "CISA KEV", "retirejs": "RetireJS",
    "nvd_cpe": "NVD", "nvd_cve": "NVD", "cve_org": "CVE.org", "rss_feed": "News feeds", "hn_pkg": "News feeds",
}


def _stale_notices(stale: dict[str, float] | None) -> list[dict[str, Any]]:
    oldest: dict[str, float] = {}
    for prefix, age in (stale or {}).items():
        name = STALE_SOURCE.get(prefix)
        if name:
            oldest[name] = max(oldest.get(name, 0.0), age)
    return [{"name": n, "hours": max(1, round(a / 3600))} for n, a in sorted(oldest.items())]


def _fix_plan(vulns: list[dict[str, Any]], version: str | None, spec: str) -> dict[str, Any]:
    """How to clear a package's findings: groups by fix, and the one version that clears them all."""
    bounds = spec_bounds(spec)
    groups, upgrade_to = group_by_fix(vulns, version or bounds.lower)
    return {"fix_groups": groups, "upgrade_to": upgrade_to}


def _split_news(news: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(all, reports, articles). Reports are official advisories; articles are coverage and discussion.

    Stories that name a CVE we found come first inside each list.
    """
    tagged = [
        {**n, "kind": n.get("kind") or story_kind(n["source"]), "matched_by": n.get("matched_by", "name")}
        for n in news
    ]
    tagged.sort(key=lambda n: n["matched_by"] != "cve")
    return (
        tagged,
        [n for n in tagged if n["kind"] == "report"],
        [n for n in tagged if n["kind"] != "report"],
    )


def _actions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The 'do this first' list: one line per vulnerable package, worst first."""
    out = []
    for r in rows:
        if not r["vulns"]:
            continue
        out.append({
            "name": r["name"],
            "ecosystem": r["ecosystem"],
            "current": r["version"] if r["exact"] else (r["spec"] or "any version"),
            "exact": r["exact"],
            "upgrade_to": r.get("upgrade_to"),
            "count": len(r["vulns"]),
            "worst": r["worst"],
            "worst_class": r["worst_class"],
            "exploited": r["exploited"],
            "malware": r.get("malware", False),
            "hidden": r.get("hidden", 0),
            "updatable": len(r.get("updatable", [])),
        })
    out.sort(key=lambda a: (not a["malware"], not a["exploited"], -RANK.get(a["worst"], 0), -a["count"], a["name"].lower()))
    return out


def hydrate(result):
    """Rebuild the derived lists on a result loaded back from the session."""
    result = dict(result)
    rows = []
    for r in result["packages"]:
        if "fix_groups" not in r:               # saved before grouping existed
            r = {**r, **_fix_plan(r["vulns"], r["version"], r["spec"]), "hidden": 0, "updatable": [], "malware": False}
        rows.append(r)
    result["packages"] = rows
    result["vulnerable"], result["clean"], result["unchecked"] = split_groups(rows)
    result.setdefault("actions", _actions(rows))
    if "reports" not in result:
        result["news"], result["reports"], result["articles"] = _split_news(result.get("news", []))
    return result


def build_result(
    packages: list[Any],
    vulns: dict[tuple, list[dict[str, Any]]],
    sources: dict[str, str],
    news: list[dict[str, Any]],
    stale: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Shape everything the dashboard and the JSON download need."""
    news, reports, articles = _split_news(news)
    rows = []
    for pkg in packages:
        deduped = _merge_package_vulns(vulns.get((pkg.key, pkg.version), []))
        hidden = 0
        updatable: list[dict[str, Any]] = []
        if not pkg.version:
            # No exact version. Keep only advisories that can touch what the requirement allows,
            # then set aside those a plain update inside the range already fixes.
            bounds = spec_bounds(pkg.spec)
            kept = [v for v in deduped if could_affect(v, bounds)]
            hidden = len(deduped) - len(kept)
            updatable = [
                {"cve": v["cve"], "severity": v["severity"], "fixed_in": next(
                    (f for f in v["fixed"] if fixed_within({**v, "fixed": [f]}, bounds)), None)}
                for v in kept if fixed_within(v, bounds)
            ]
            deduped = [v for v in kept if not fixed_within(v, bounds)]
            for v in deduped:
                if v.get("versions"):
                    v["in_range_versions"] = versions_in_range(v, bounds)
        vs = sorted(deduped, key=sort_key)
        plan = _fix_plan(vs, pkg.version, pkg.spec)
        logo, own = logo_for(pkg.ecosystem, pkg.name)
        worst = max((v["severity"] for v in vs), key=lambda s: RANK[s]) if vs else None
        cve_set = {v["cve"].lower() for v in vs}
        pkg_news = [
            n
            for n in news
            if pkg.name.lower() in [m.lower() for m in n["matches"]]
            or any(m.lower() in cve_set for m in n["matches"])
        ]
        reason = unchecked_reason(pkg, sources)
        rows.append({
            "checked": reason is None,
            "unchecked_reason": reason,
            "ecosystem": pkg.ecosystem,
            "name": pkg.name,
            "version": pkg.version,
            "spec": pkg.spec,
            "exact": bool(pkg.version),
            "logo": logo,
            "own_logo": own,
            "vulns": vs,
            **plan,
            "hidden": hidden,
            "updatable": updatable,
            "malware": any(is_malware(v) for v in vs),
            "worst": worst,
            "worst_class": (worst or "clean").lower(),
            "exploited": any(v["exploited"] for v in vs),
            "rank": (any(is_malware(v) for v in vs), any(v["exploited"] for v in vs), RANK[worst] if worst else -1, len(vs)),
            "news": pkg_news,
        })
    rows.sort(key=lambda r: tuple(-x for x in r["rank"][:3]) + (-r["rank"][3], r["name"].lower()))
    for r in rows:
        r.pop("rank")

    all_v = [v for r in rows for v in r["vulns"]]
    counts = {s: sum(v["severity"] == s for v in all_v) for s in SEVERITIES}
    peak = max(counts.values(), default=0) or 1
    vulnerable, clean, unchecked = split_groups(rows)
    checked = sum(r["checked"] for r in rows)
    # "Fixable" means an upgrade from where you are clears it, not merely that some old fix exists.
    fixable = sum(g["count"] for r in rows for g in r["fix_groups"] if g["to"])
    confirmed = sum(len(r["vulns"]) for r in rows if r["exact"])
    return {
        "generated": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        "packages": rows,
        "actions": _actions(rows),
        "vulnerable": vulnerable, "clean": clean, "unchecked": unchecked,
        "news": news,
        "reports": reports,
        "articles": articles,
        "sources": [{"name": k, "status": v} for k, v in sources.items()],
        "stale": _stale_notices(stale),
        "stats": {
            "packages": len(rows),
            "checked": checked,
            "vulnerable": len(vulnerable),
            "exploited": sum(r["exploited"] for r in rows),
            "critical_high": counts["CRITICAL"] + counts["HIGH"],
            "news": len(news),
            "reports": len(reports),
            "articles": len(articles),
            "corroborated": sum(v.get("corroboration", 1) > 1 for v in all_v),
            "single_source": sum(v.get("corroboration", 1) <= 1 for v in all_v),
            "disputed": sum(bool(v.get("disputed")) for v in all_v),
            "problems": len(all_v),
            "confirmed": confirmed,
            "possible": len(all_v) - confirmed,
            "hidden": sum(r["hidden"] for r in rows),
            "updatable": sum(len(r.get("updatable", [])) for r in rows),
            "malware": sum(is_malware(v) for v in all_v),
            "fixable": fixable,
            "clean_pct": round(100 * len(clean) / checked) if checked else None,
            "fixable_pct": round(100 * fixable / len(all_v)) if all_v else None,
        },
        "bars": [
            {
                "label": s.title(),
                "cls": s.lower(),
                "count": counts[s],
                "pct": max(2, round(100 * counts[s] / peak)),
            }
            for s in SEVERITIES[:4]
        ],
    }
