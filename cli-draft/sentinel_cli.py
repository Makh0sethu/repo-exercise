#!/usr/bin/env python3
"""sentinel - track installed libraries, check them for known vulnerabilities,
scan the web for recent security stories about them, and build an HTML dashboard.

Standard library only. Data sources:
  * OSV.dev (https://osv.dev)      - known vulnerabilities per package/version
  * Security news RSS feeds        - recent stories mentioning your packages / CVEs
  * Hacker News (Algolia search)   - recent discussion per package

Usage:
  python sentinel_cli.py discover            # write inventory of this system to packages.txt
  python sentinel_cli.py scan                # read packages.txt, query the web, write dashboard.html
  python sentinel_cli.py discover scan       # both
"""
import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{}"
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=10&query={}"
FEEDS = {
    "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "Krebs on Security": "https://krebsonsecurity.com/feed/",
    "CISA Advisories": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
    "GitHub Security Blog": "https://github.blog/security/feed/",
    "SecurityWeek": "https://feeds.feedburner.com/securityweek",
}
UA = {"User-Agent": "sentinel/1.0 (local dependency scanner)"}
ECOSYSTEMS = {"pypi": "PyPI", "npm": "npm", "go": "Go", "crates": "crates.io",
              "cargo": "crates.io", "maven": "Maven", "nuget": "NuGet",
              "rubygems": "RubyGems", "gem": "RubyGems", "packagist": "Packagist"}
SEV_ORDER = {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}


# --------------------------------------------------------------------------- HTTP
def http(url, data=None, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, data=None, timeout=20):
    return json.loads(http(url, data, timeout))


# --------------------------------------------------------------------------- inventory
def discover():
    """Collect libraries installed on this system (pip, global npm)."""
    pkgs = {}
    try:
        from importlib import metadata
        for d in metadata.distributions():
            name = d.metadata["Name"]
            if name:
                pkgs[("PyPI", name.lower())] = d.version
    except Exception as e:
        print(f"! pip discovery failed: {e}", file=sys.stderr)
    if shutil.which("npm"):
        try:
            out = subprocess.run(["npm", "ls", "-g", "--depth=0", "--json"],
                                 capture_output=True, text=True, timeout=60).stdout
            for n, info in json.loads(out or "{}").get("dependencies", {}).items():
                if info.get("version"):
                    pkgs[("npm", n)] = info["version"]
        except Exception as e:
            print(f"! npm discovery failed: {e}", file=sys.stderr)
    return pkgs


def write_inventory(pkgs, path):
    lines = [
        "# sentinel inventory - one package per line.",
        "# Formats:  name==version        (PyPI)",
        "#           npm:name@version     (also go:, crates:, maven:, nuget:, gem:, packagist:)",
        f"# Generated {datetime.now():%Y-%m-%d %H:%M} - edit freely; add your own entries.",
        "",
    ]
    for (eco, name), ver in sorted(pkgs.items()):
        lines.append(f"{name}=={ver}" if eco == "PyPI" else f"{eco.lower()}:{name}@{ver}")
    Path(path).write_text("\n".join(lines) + "\n")


LINE = re.compile(r"^(?:(?P<eco>[A-Za-z.]+):)?(?P<name>[^=@\s<>~!]+)\s*(?:==|@|=)\s*(?P<ver>[^\s;#]+)")


def read_inventory(path):
    """Parse the text document into [(ecosystem, name, version)]."""
    out = []
    for raw in Path(path).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = LINE.match(line)
        if not m:
            print(f"! skipping unversioned/unparseable line: {raw!r}", file=sys.stderr)
            continue
        eco = ECOSYSTEMS.get((m["eco"] or "pypi").lower())
        if not eco:
            print(f"! unknown ecosystem in line: {raw!r}", file=sys.stderr)
            continue
        out.append((eco, m["name"], m["ver"]))
    return out


# --------------------------------------------------------------------------- OSV
def severity_of(v):
    sev = (v.get("database_specific") or {}).get("severity")
    if sev:
        return sev.upper()
    for s in v.get("severity", []):
        m = re.search(r"CVSS:3\.\d/.*", s.get("score", ""))
        if m:
            return cvss3_bucket(s["score"])
    return "UNKNOWN"


def cvss3_bucket(vector):
    """Approximate CVSS v3 base score from the vector and bucket it."""
    w = dict(p.split(":") for p in vector.split("/")[1:])
    av = {"N": .85, "A": .62, "L": .55, "P": .2}[w["AV"]]
    ac = {"L": .77, "H": .44}[w["AC"]]
    changed = w["S"] == "C"
    pr = {"N": .85, "L": .68 if changed else .62, "H": .5 if changed else .27}[w["PR"]]
    ui = {"N": .85, "R": .62}[w["UI"]]
    cia = {"H": .56, "L": .22, "N": 0}
    iss = 1 - (1 - cia[w["C"]]) * (1 - cia[w["I"]]) * (1 - cia[w["A"]])
    imp = 7.52 * (iss - .029) - 3.25 * (iss - .02) ** 15 if changed else 6.42 * iss
    if imp <= 0:
        return "UNKNOWN"
    score = min((1.08 if changed else 1) * (imp + 8.22 * av * ac * pr * ui), 10)
    return "CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MODERATE" if score >= 4 else "LOW"


def fixed_versions(v, eco, name):
    fixes = set()
    for a in v.get("affected", []):
        pkg = a.get("package", {})
        if pkg.get("ecosystem") != eco or pkg.get("name", "").lower() != name.lower():
            continue
        for r in a.get("ranges", []):
            for ev in r.get("events", []):
                if "fixed" in ev:
                    fixes.add(ev["fixed"])
    return sorted(fixes)


def query_osv(pkgs):
    """Return {(eco,name,ver): [vuln dict, ...]}."""
    results = {}
    for i in range(0, len(pkgs), 500):
        chunk = pkgs[i:i + 500]
        body = {"queries": [{"package": {"name": n, "ecosystem": e}, "version": v} for e, n, v in chunk]}
        resp = http_json(OSV_BATCH, body, timeout=60)
        for p, r in zip(chunk, resp["results"]):
            results[p] = [x["id"] for x in r.get("vulns", [])]
    ids = sorted({i for v in results.values() for i in v})
    with ThreadPoolExecutor(8) as ex:
        details = dict(zip(ids, ex.map(lambda i: _safe(lambda: http_json(OSV_VULN.format(i))), ids)))
    out = {}
    for p, vids in results.items():
        eco, name, _ = p
        rows = []
        for vid in vids:
            v = details.get(vid)
            if not v:
                rows.append({"id": vid, "summary": "(details unavailable)", "severity": "UNKNOWN",
                             "aliases": [], "fixed": [], "published": "", "url": f"https://osv.dev/{vid}"})
                continue
            rows.append({
                "id": vid,
                "aliases": v.get("aliases", []),
                "summary": v.get("summary") or (v.get("details", "")[:200]),
                "severity": severity_of(v),
                "fixed": fixed_versions(v, eco, name),
                "published": (v.get("published") or "")[:10],
                "url": f"https://osv.dev/{vid}",
            })
        rows.sort(key=lambda r: -SEV_ORDER.get(r["severity"], 0))
        out[p] = rows
    return out


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


# --------------------------------------------------------------------------- news
def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fetch_feed(name, url):
    try:
        root = ET.fromstring(http(url))
    except Exception as e:
        return name, [], str(e)
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        g = lambda t: next((c for c in it if c.tag.split("}")[-1] == t), None)
        title, link = g("title"), g("link")
        href = (link.get("href") if link is not None and link.get("href") else (link.text if link is not None else ""))
        desc = g("description") or g("summary") or g("content")
        date = g("pubDate") or g("published") or g("updated")
        items.append({
            "source": name,
            "title": html.unescape((title.text or "") if title is not None else "").strip(),
            "url": (href or "").strip(),
            "text": re.sub(r"<[^>]+>", " ", html.unescape(desc.text or "")) if desc is not None else "",
            "date": parse_date(date.text if date is not None else None),
        })
    return name, items, None


def name_pattern(pkg):
    # word-ish boundary match; skip very short / very generic names to avoid noise
    return re.compile(r"(?<![\w-])" + re.escape(pkg) + r"(?![\w-])", re.I)


GENERIC = {"six", "attrs", "click", "rich", "wheel", "pip", "idna", "toml", "ply", "tqdm", "packaging", "typing"}


def scan_news(pkgs, cve_ids, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stories, feed_status = [], {}
    with ThreadPoolExecutor(6) as ex:
        for name, items, err in ex.map(lambda kv: fetch_feed(*kv), FEEDS.items()):
            feed_status[name] = err or f"{len(items)} items"
            for it in items:
                if it["date"] and it["date"] >= cutoff:
                    stories.append(it)
    names = sorted({n for _, n, _ in pkgs})
    pats = {n: name_pattern(n) for n in names if len(n) > 2 and n.lower() not in GENERIC}
    cve_pat = {c: re.compile(re.escape(c), re.I) for c in cve_ids}
    matched = {}
    for s in stories:
        blob = f"{s['title']} {s['text']}"
        hits = [n for n, p in pats.items() if p.search(blob)] + [c for c, p in cve_pat.items() if p.search(blob)]
        if hits:
            matched[s["url"]] = {**s, "date": s["date"].isoformat(), "matches": hits, "text": ""}
    # Hacker News: recent security stories per package
    def hn(n):
        try:
            q = urllib.parse.quote(f"{n} vulnerability")
            hits = http_json(HN_SEARCH.format(q))["hits"]
        except Exception:
            return n, [], True
        out = []
        for h in hits:
            d = parse_date(h.get("created_at"))
            title = h.get("title") or ""
            if d and d >= cutoff and h.get("url") and pats.get(n, name_pattern(n)).search(title):
                out.append({"source": "Hacker News", "title": title, "url": h["url"],
                            "date": d.isoformat(), "matches": [n], "text": ""})
        return n, out, False
    hn_names = list(pats)[:60]
    hn_fail = 0
    with ThreadPoolExecutor(6) as ex:
        for n, out, failed in ex.map(hn, hn_names):
            hn_fail += failed
            for o in out:
                if o["url"] in matched:
                    matched[o["url"]]["matches"] = sorted(set(matched[o["url"]]["matches"] + o["matches"]))
                else:
                    matched[o["url"]] = o
    feed_status["Hacker News"] = "unreachable" if hn_names and hn_fail == len(hn_names) else "ok"
    return sorted(matched.values(), key=lambda s: s["date"], reverse=True), feed_status


# --------------------------------------------------------------------------- dashboard
def build_dashboard(data, out_path):
    tpl = (HERE / "dashboard_template.html").read_text()
    payload = json.dumps(data).replace("</", "<\\/")
    Path(out_path).write_text(tpl.replace("/*__DATA__*/null", payload))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("commands", nargs="+", choices=["discover", "scan"])
    ap.add_argument("-i", "--inventory", default=str(HERE / "packages.txt"), help="text document listing packages")
    ap.add_argument("-o", "--output", default=str(HERE / "dashboard.html"))
    ap.add_argument("--days", type=int, default=30, help="how far back to look for news stories (default 30)")
    ap.add_argument("--no-news", action="store_true", help="skip the news scan")
    a = ap.parse_args()

    if "discover" in a.commands:
        pkgs = discover()
        write_inventory(pkgs, a.inventory)
        print(f"discovered {len(pkgs)} packages -> {a.inventory}")
    if "scan" in a.commands:
        if not Path(a.inventory).exists():
            sys.exit(f"{a.inventory} not found - run `discover` first or create it.")
        inv = read_inventory(a.inventory)
        print(f"checking {len(inv)} packages against OSV.dev ...")
        try:
            vulns = query_osv(inv)
        except (urllib.error.URLError, OSError) as e:
            sys.exit(f"Could not reach OSV.dev ({e}). Check your network/proxy and retry.")
        cves = sorted({al for rows in vulns.values() for r in rows for al in r["aliases"] if al.startswith("CVE-")})
        news, feeds = ([], {}) if a.no_news else scan_news(inv, cves, a.days)
        packages = []
        for (eco, name, ver), rows in vulns.items():
            top = max((SEV_ORDER.get(r["severity"], 0) for r in rows), default=-1)
            packages.append({"ecosystem": eco, "name": name, "version": ver, "vulns": rows, "top": top,
                             "news": [n for n in news if name in n["matches"] or
                                      any(al in n["matches"] for r in rows for al in r["aliases"])]})
        packages.sort(key=lambda p: (-p["top"], -len(p["vulns"]), p["name"].lower()))
        data = {"generated": datetime.now().isoformat(timespec="seconds"), "days": a.days,
                "packages": packages, "news": news, "feeds": feeds}
        build_dashboard(data, a.output)
        vp = sum(1 for p in packages if p["vulns"])
        print(f"{vp}/{len(packages)} packages have known vulnerabilities; {len(news)} related stories")
        print(f"dashboard -> {a.output}")


if __name__ == "__main__":
    main()
