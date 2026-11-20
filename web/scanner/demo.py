"""Sample scan for /demo/: shows the dashboard without calling any source.

The advisories are real public ones, used as examples; the "clean" packages
are illustrative and were not checked.
"""
from .parsers import Package
from .scan import build_result


def _v(cve, sev, summary, fixed, published):
    return {"id": cve, "aliases": [], "summary": summary, "severity": sev, "fixed": [fixed],
            "published": published, "url": f"https://osv.dev/vulnerability/{cve}",
            "sources": ["OSV.dev", "CVE.org"], "exploited": False}


PACKAGES = [
    Package("PyPI", "requests", "2.19.0", "==2.19.0", "requirements.txt"),
    Package("PyPI", "pyyaml", "5.3", "==5.3", "requirements.txt"),
    Package("PyPI", "django", "4.2.1", "==4.2.1", "requirements.txt"),
    Package("PyPI", "psycopg2-binary", None, ">=2.9", "requirements.txt"),
    Package("PyPI", "flask", "3.1.0", "==3.1.0", "requirements.txt"),
    Package("PyPI", "gunicorn", "23.0.0", "==23.0.0", "requirements.txt"),
    Package("PyPI", "numpy", "2.1.3", "==2.1.3", "requirements.txt"),
    Package("PyPI", "celery", "5.4.0", "==5.4.0", "requirements.txt"),
    Package("PyPI", "whitenoise", "6.8.2", "==6.8.2", "requirements.txt"),
    Package("npm", "lodash", "4.17.15", "4.17.15", "package.json"),
    Package("npm", "express", "4.18.2", "4.18.2", "package.json"),
    Package("npm", "react", "18.3.1", "18.3.1", "package.json"),
    Package("Other", "postgresql", "15.4", "15.4", "Other software"),
    Package("Other", "postgis", "3.3.2", "3.3.2", "Other software"),
]
VULNS = {
    (("PyPI", "requests"), "2.19.0"): [
        _v("CVE-2018-18074", "CRITICAL", "Authorization header sent to the new host after an HTTPS to HTTP redirect.", "2.20.0", "2018-10-09"),
        _v("CVE-2023-32681", "MODERATE", "Proxy-Authorization header leaked to destination servers on redirect.", "2.31.0", "2023-05-22")],
    (("PyPI", "pyyaml"), "5.3"): [
        _v("CVE-2020-14343", "CRITICAL", "Arbitrary code execution when loading untrusted YAML with full_load.", "5.4", "2021-02-09")],
    (("PyPI", "django"), "4.2.1"): [
        _v("CVE-2023-36053", "HIGH", "Regular expression denial of service in EmailValidator and URLValidator.", "4.2.3", "2023-07-03")],
    (("npm", "lodash"), "4.17.15"): [
        _v("CVE-2021-23337", "HIGH", "Command injection through the template function.", "4.17.21", "2021-02-15"),
        _v("CVE-2020-8203", "HIGH", "Prototype pollution in zipObjectDeep.", "4.17.19", "2020-07-15")],
    (("npm", "express"), "4.18.2"): [
        _v("CVE-2024-29041", "MODERATE", "Open redirect when handling malformed URLs.", "4.19.2", "2024-03-25")],
    (("Other", "postgresql"), "15.4"): [
        _v("CVE-2024-0985", "HIGH", "REFRESH MATERIALIZED VIEW CONCURRENTLY can run SQL as the object owner.", "15.6", "2024-02-08")],
}
NEWS = [
    {"source": "The Hacker News", "title": "[Headline of a story that mentions lodash]", "url": "#", "date": "[date]", "matches": ["lodash"]},
    {"source": "BleepingComputer", "title": "[Headline of a story that mentions PostgreSQL]", "url": "#", "date": "[date]", "matches": ["postgresql"]},
    {"source": "Hacker News", "title": "[Headline of a story that mentions express]", "url": "#", "date": "[date]", "matches": ["express"]},
]
SOURCES = {name: "sample data" for name in ["OSV.dev", "NVD", "CVE.org", "CISA KEV", "RetireJS", "News feeds"]}


def demo_result():
    import copy
    return build_result(PACKAGES, copy.deepcopy(VULNS), SOURCES, NEWS, sample=True)
