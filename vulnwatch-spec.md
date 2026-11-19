# Vulnwatch

## Summary
A public web app for any developer. You upload or paste your dependency files, and it checks every package online for known vulnerabilities and recent security news. Results appear on a dashboard. Version 1 is a one-off scan: no accounts, and nothing the user uploads is saved.

## Goals and success criteria
- A developer uploads `requirements.txt` / `package.json` / `package-lock.json` and gets a dashboard in about 2–5 seconds.
- Every listed package is checked. None is skipped silently.
- Actively exploited vulnerabilities (CISA KEV) are impossible to miss.
- The report can be kept as PDF or JSON.

## Non-goals (v1)
- Accounts, saved projects, scheduled re-checks, email alerts: these are **v2**.
- Reading a user's machine directly. A website can't, so the user provides the files.
- Connecting to the user's database to read its version. That would mean sharing credentials with a public site.
- Scanning user-supplied URLs with OWASP ZAP. Without accounts we can't verify site ownership, so the server could be used to attack other sites.
- Sonatype OSS Index. Its tokens are mandatory and it is migrating to Sonatype Guide during 2026. Revisit once that settles.
- npm audit API. It duplicates GitHub advisories, which OSV.dev already includes.

## Decisions
| Decision | Choice | Rationale |
|---|---|---|
| Audience | Public web app, any developer | User's goal |
| Mode | One-off scan, no accounts | Useful on day one. Tracking is added later without rework |
| Stack covered | Python, npm, PostgreSQL, PostGIS | The user's own stack first |
| Inputs | Upload or paste `requirements.txt`, `package.json`, `package-lock.json`; an "Other software" box for lines like `postgresql 15.4`, `postgis 3.3.2` | PostgreSQL/PostGIS aren't in dependency files |
| No / loose version | Check by package name only, labelled "version unknown" | User's decision. Nothing skipped |
| Backend | Django | User's choice. The admin panel helps in v2 |
| Database | PostgreSQL: cache of **public** vulnerability/news data only | Already in the user's stack |
| UI | Material Web (Google M3 components), swapped for "izitho ui M3" later | izitho isn't available yet |
| Hosting | User's own server. Develop and test locally first, then push to the server | User's decision |
| Package vulnerabilities | OSV.dev is the primary source for PyPI + npm | Exact package-name matching. Avoids Dependency-Check's CPE false positives |
| Dependency-Check sources | NVD API 2.0 (with API key), CISA KEV, RetireJS | Taken from OWASP Dependency-Check's source list |
| CVE Program | Pull official CVE records (CNA + CISA ADP scores) from the cve.org API `cveawg.mitre.org` | Covers CVEs that NVD hasn't scored yet (backlog since 2024) |
| PostgreSQL/PostGIS | Checked against NVD | Not in OSV.dev |
| News | Six security RSS feeds + Hacker News, last 30 days, matched by package name or CVE id | From the draft |
| Export | Download PDF (browser print stylesheet) + Download JSON | Nothing is stored, so the user keeps a copy |
| OWASP ZAP | Baseline scan of **our own** app before each release (v1). Import ZAP reports into the dashboard (v2) | It's a public site that accepts uploads |

## Pipeline
```
READ     files + "Other software" box → [(ecosystem, name, version|None)]
FIND     OSV.dev (PyPI, npm) · RetireJS (JS libs) · NVD (postgresql, postgis)
ENRICH   per CVE id: cve.org record → NVD score if none → CISA KEV exploited flag
MERGE    deduplicate by CVE id / aliases → one row per problem
NEWS     cached feeds + Hacker News, 30 days, match names + CVE ids
SHOW     exploited first, then critical → high → moderate → low → clean
EXPORT   PDF / JSON
```

## Requirements
**Input**
- Accepts upload or paste. Max 1 MB per scan.
- Parses `requirements.txt` (`==`, ranges, bare names; ignores comments, `-r`, `-e`), `package.json` (dependencies + devDependencies), `package-lock.json` (v2/v3, exact versions), and Other-software lines `<name> <version>`.
- Lines it can't read are listed back to the user. They are never dropped silently.
- Tip shown on the page: upload `package-lock.json` or `pip freeze` output for exact results.

**Checking**
- Exact versions are queried by version. Missing or loose versions are queried by name and marked "version unknown".
- Each vulnerability shows: id + aliases, summary, severity, exploited (KEV) flag, fixed-in versions, published date, link.
- If a source fails or times out, partial results are still shown, along with a banner naming the failed source.

**Caching (public data only)**
- News feeds: hourly. CISA KEV + RetireJS definitions: daily. CVE/NVD/OSV records: cached for 24 h.
- Runs as Django management commands on cron (or Celery beat later).
- Uploaded files and scan results are never written to disk or the database.

**Dashboard**
- Totals: packages checked, vulnerable, exploited, critical/high, related stories.
- Severity bar, a filterable package list (expand to see problems + news), and a recent-stories list.
- Download PDF / JSON buttons.

**Security of the app itself**
- Per-IP rate limit (default 10 scans/min). Upload size limit. CSRF on the form.
- Secrets (NVD API key) only in server environment variables.
- OWASP ZAP baseline scan passes with no high alerts before release.

## Risks and mitigations
- **NVD rate limits / backlog:** use an API key and the 24 h cache, and fall back to cve.org scores.
- **External API outages:** show partial results with a source-status banner.
- **False-positive news matches on generic names:** skip very short or common names, and require a word-boundary match.
- **CPE matching for PostgreSQL/PostGIS:** fix the CPE names (`postgresql:postgresql`, `postgis:postgis`) and test them against known CVEs.
- **Abuse of a public upload form:** rate limits, size limits, nothing stored, ZAP scan.

## Plan and sequencing
1. Django project + Postgres + Material Web base layout + upload/paste form. Run locally.
2. Parsers + unit tests. **Validate first** with real files from your projects.
3. OSV.dev lookup (port from the draft `vulnwatch/vulnwatch.py`) + results page.
4. Enrichment: cve.org, NVD (API key), CISA KEV. Merge/deduplicate.
5. RetireJS. PostgreSQL/PostGIS via NVD.
6. News cache job + matching.
7. Dashboard polish + PDF/JSON export.
8. Hardening: rate limit, limits, ZAP baseline. Deploy to your server.

The draft in `vulnwatch/` (stdlib CLI) is reference code. Reuse its OSV lookup, feed parsing, CVSS bucketing and dashboard layout, and drop its `discover` command.

## Open questions
- "izitho ui M3": where it lives and when it's ready to replace Material Web.
- "CVE partnership": recorded here as pulling cve.org records. If it instead means applying to become a CVE Numbering Authority (CNA), that is a separate application to the CVE Program, not code.
- Exact server OS / web server (nginx + gunicorn assumed) for the deploy step.
