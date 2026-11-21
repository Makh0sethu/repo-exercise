"""A local copy of the OSV vulnerability database, so dependency checks work with no network.

`update()` is the only function here that goes online (manage.py update_osv). It downloads the public
per-ecosystem dumps and reduces them to a small index. `query()` then answers from disk. The index records
when it was built, and every offline run reports that date: offline data is only as fresh as its last update.
"""
import json
import re
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from scanner.sources import _osv_fixed, _osv_severity
from scanner.versions import compare_versions

DUMP_URL = "https://osv-vulnerabilities.storage.googleapis.com/{eco}/all.zip"
ECOSYSTEMS = ("PyPI", "npm")


def data_dir() -> Path:
    return Path(getattr(settings, "SENTINEL_OSV_DIR", Path(settings.BASE_DIR) / "osv-data"))


def norm(eco: str, name: str) -> str:
    n = name.lower()
    return re.sub(r"[-_.]+", "-", n) if eco == "PyPI" else n


def _index_path(eco): return data_dir() / f"{eco}.index.json"
def _meta_path(): return data_dir() / "meta.json"


def meta() -> dict:
    """{'updated_at': ISO date, 'counts': {eco: n}} or {} when no database has been built."""
    try:
        return json.loads(_meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_index(eco: str, zip_path: Path) -> int:
    """Reduce an OSV dump to {normalised name: [advisory, ...]} and save it. Returns advisory count."""
    index: dict[str, list] = {}
    n = 0
    with zipfile.ZipFile(zip_path) as zf:
        for entry in zf.namelist():
            if not entry.endswith(".json"):
                continue
            v = json.loads(zf.read(entry))
            if v.get("withdrawn"):
                continue
            n += 1
            for aff in v.get("affected", []):
                pkg = aff.get("package", {})
                if pkg.get("ecosystem") != eco:
                    continue
                name = pkg.get("name", "")
                ranges = [r["events"] for r in aff.get("ranges", []) if r.get("type") in ("ECOSYSTEM", "SEMVER")]
                index.setdefault(norm(eco, name), []).append({
                    "id": v["id"], "aliases": v.get("aliases", []),
                    "summary": v.get("summary") or (v.get("details") or "Details unavailable")[:200],
                    "severity": _osv_severity(v), "fixed": _osv_fixed(v, eco, name),
                    "published": (v.get("published") or "")[:10],
                    "versions": aff.get("versions", []), "ranges": ranges})
    data_dir().mkdir(parents=True, exist_ok=True)
    _index_path(eco).write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    return n


def update(ecosystems=ECOSYSTEMS, progress=print) -> dict:
    """Download the dumps and rebuild the index. The only network call in this module."""
    data_dir().mkdir(parents=True, exist_ok=True)
    counts = {}
    for eco in ecosystems:
        zp = data_dir() / f"{eco}.zip"
        progress(f"Downloading {eco} advisories…")
        with urllib.request.urlopen(DUMP_URL.format(eco=eco), timeout=120) as r, open(zp, "wb") as f:  # noqa: S310
            while chunk := r.read(1 << 20):
                f.write(chunk)
        counts[eco] = build_index(eco, zp)
        zp.unlink(missing_ok=True)
        progress(f"{eco}: {counts[eco]} advisories indexed")
    old = meta().get("counts", {})
    _meta_path().write_text(json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                        "counts": {**old, **counts}}), encoding="utf-8")
    return counts


def _affected(version: str, adv: dict) -> bool:
    if version in adv["versions"]:
        return True
    for events in adv["ranges"]:
        start = None
        for ev in events:
            if "introduced" in ev:
                start = ev["introduced"]
            elif "fixed" in ev or "last_affected" in ev:
                end, inclusive = (ev["fixed"], False) if "fixed" in ev else (ev["last_affected"], True)
                if start is not None and _in(version, start, end, inclusive):
                    return True
                start = None
        if start is not None and _in(version, start, None, False):   # introduced, never fixed
            return True
    return False


def _in(version, start, end, inclusive):
    if start != "0" and compare_versions(version, start) < 0:
        return False
    if end is None:
        return True
    c = compare_versions(version, end)
    return c <= 0 if inclusive else c < 0


def query(packages) -> dict:
    """Same shape as scanner.sources.query_osv: {(pkg.key, pkg.version): [advisory rows]}.
    A package with no exact version gets every advisory filed for its name (callers must not score those)."""
    out, loaded = {}, {}
    for p in packages:
        if p.ecosystem not in ECOSYSTEMS:
            continue
        if p.ecosystem not in loaded:
            try:
                loaded[p.ecosystem] = json.loads(_index_path(p.ecosystem).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded[p.ecosystem] = {}
        rows, seen = [], set()
        for adv in loaded[p.ecosystem].get(norm(p.ecosystem, p.name), []):
            if adv["id"] in seen or (p.version and not _affected(p.version, adv)):
                continue
            seen.add(adv["id"])
            rows.append({"id": adv["id"], "aliases": adv["aliases"], "summary": adv["summary"],
                         "severity": adv["severity"], "fixed": adv["fixed"], "published": adv["published"],
                         "url": f"https://osv.dev/vulnerability/{adv['id']}", "sources": ["OSV (local copy)"],
                         "exploited": False})
        out[(p.key, p.version)] = rows
    return out
