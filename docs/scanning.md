# How scanning works, and what changed (20–21 Nov 2026)

This documents the scanning side of Sentinel: where the data comes from, how long a scan waits, what is saved, what happens when a source fails, and what was fixed or learned while getting there. For setup and the product overview see the [README](../README.md); for the plan see [sentinel-spec.md](../sentinel-spec.md).

## 1. The scan, step by step
1. **Read** the uploaded or pasted files and the "Other software" box into packages (`scanner/parsers.py`).
2. **Look up** every source at the same time (`scanner/scan.py`, `run_scan`).
3. **Wait** for them, but never longer than `SENTINEL_SCAN_DEADLINE` (default 25 s). A scan ends the moment every source has answered.
4. **Enrich** unknown severities (CVE.org, then NVD if an API key is set), flag actively exploited CVEs (CISA KEV), merge duplicates by CVE id, match news.
5. **Show** the dashboard. The result gets its own address (`/results/<id>/`) and is kept only in the visitor's session.

## 2. Where the data comes from
| Source | What we ask for | Saved for | Request time limit |
|---|---|---|---|
| **OSV.dev** (PyPI, npm) | One batch call for your packages (500 per call), then each advisory found, 8 at a time | 24 h | 20 s batch, 12 s per advisory |
| **CISA KEV** | The whole catalog (~1.4 MB); only the CVE ids are kept | 24 h | 90 s |
| **RetireJS** | The whole JavaScript definitions file (~630 KB, 77 libraries) | 24 h | 120 s |
| **NVD** | Per "Other software" product and version (PostgreSQL, PostGIS…). One lookup per CVE with unknown severity, only when `NVD_API_KEY` is set | 24 h | 25 s product query, 15 s single CVE |
| **CVE.org** | One record per CVE with unknown severity (max 25 per scan); only the severity is kept | 24 h | 10 s |
| **News** | Six RSS feeds (each saved on its own), plus up to 5 Hacker News searches | 1 h | 12 s per feed, 8 s per search |

Four of the six ask only about your packages. KEV, RetireJS and the RSS feeds are whole files because they offer no way to ask for a subset.

## 3. Published limits, and what Sentinel does about them
Checked on 20 Nov 2026. Numbers can change; follow the links before relying on one.

| Source | Published rule | Sentinel's behaviour |
|---|---|---|
| NVD | [5 requests / rolling 30 s without a key, 50 with one](https://nvd.nist.gov/developers/start-here); HTTP 429 when exceeded; [~6 s between scripted calls suggested](https://nvd.nist.gov/developers/api-workflows) | Shared budget of 4 (no key) or 49 (key) per 30 s. Over budget a call is skipped and a saved copy is used. After a 429 that item rests 10 minutes |
| OSV.dev | [No rate limit at present](https://google.github.io/osv.dev/faq/); abuse can be blocked; [P95 latency target 6 s](https://osv.dev/blog/posts/api-latency-improvements-and-revised-slos/); batch cap of 1,000 reported by users, not in OSV's docs | 500 per batch call, 8 advisory lookups at once |
| CVE.org | [No limit published](https://www.cve.org/allresources/cveservices) that was found; [free for public use, don't alter records](https://www.cve.org/ResourcesSupport/FAQs); bulk belongs on [cvelistV5](https://github.com/CVEProject/cvelistV5) | Max 25 lookups per scan, 6 at a time |
| CISA KEV | No limit found; **licence text not read, check it** | One download a day |
| RetireJS | [Apache-2.0](https://github.com/RetireJS/retire.js/); no limit documented; GitHub's raw host can [rate-limit frequent fetchers](https://github.com/orgs/community/discussions/157940) | One download a day |
| Hacker News search | [10,000 / hour / IP](https://hn.algolia.com/api), 1,000 hits max | Max 5 per scan, saved 1 h |
| News RSS feeds | **Not researched.** Each publisher sets its own terms | One request per feed per hour |

No source stated a ban policy beyond OSV's "abuse" clause and NVD's 429 responses. None promises that this volume is safe either.

## 4. Saved copies (the cache)
- **Where:** `cache/public-cache.sqlite3` (`SENTINEL_CACHE_PATH`), a small file separate from the Django database (`scanner/store.py`). It survives restarts and is shared by all server workers. Put it on a persistent volume in production.
- **What goes in:** public lookup results only. Uploaded files, pasted text and scan results are never stored.
- **Fresh** copies are used without the network. **Expired** copies are still used when the live source fails or is slow: up to 30 days (news: 7). The results page then says "X is showing saved data" and how old it is.
- **Failures are remembered, never saved as data.** A failed item is not retried for 60 s (10 min after HTTP 403/429), so an outage costs the first scan its wait and the next ones nothing.
- **The audited pipeline is stricter.** It refuses saved copies older than their freshness window, so an OSV outage still *fails* its dependency step instead of passing on old data.
- **Warm it ahead of scans:** `python manage.py refresh_cache` (KEV, RetireJS, news). `--clear` forgets everything.

## 5. What a failed source looks like
Each source ends a scan as one of:
- `ok`: answered (possibly from a saved copy, noted on the page).
- `timeout`: still running at the deadline. Slow, not necessarily down. Its lookups keep running and save their answers, so a retry is usually faster.
- `unreachable`: failed outright (DNS, TLS, HTTP error).

A package no source could check is **Not checked** with the reason ("OSV.dev timed out", "NVD could not be reached"). It is never shown as clean.
What the *other* sources found is never hidden: a package with findings stays in the Vulnerable list marked **partly checked**, and a Not-checked package with related news shows those stories.

## 6. Settings
| Setting | Default | Meaning |
|---|---|---|
| `SENTINEL_SCAN_DEADLINE` | 25 | Longest a scan waits for the sources, seconds |
| `SENTINEL_CACHE_PATH` | `cache/public-cache.sqlite3` | Where saved copies live |
| `NVD_API_KEY` | empty | Raises NVD to 50 requests / 30 s and enables per-CVE NVD severity |
| `SENTINEL_RATE_LIMIT` | 10 | Scans per minute per IP |
| `SENTINEL_PREVIEW_RATE_LIMIT` | 30 | Live "Detected packages" previews per minute per IP |

## 7. What was found and fixed this session
| Problem | Effect | Fix |
|---|---|---|
| RSS dates were never read: an XML element with no children is falsy in Python, so `child("pubDate") or child("published")` skipped a valid date | **No news story ever matched**; the page always said "no recent stories" | Pick the first element that exists. Regression test added |
| A total RSS failure was saved for an hour as if it were a good empty answer | News stayed empty for an hour after a bad minute | Failures raise and are never saved; each feed is saved on its own |
| A hard-coded 8 s ceiling labelled slow sources "unreachable" | Python/npm packages shown as Not checked on any slow network | Ceiling is `SENTINEL_SCAN_DEADLINE` (25 s); slow sources are `timeout`, not `unreachable` |
| Request limits of 4–10 s were shorter than a slow line needs, and KEV/RetireJS never finished downloading | Those catalogs were re-fetched every scan and never saved | Catalog downloads get 90–120 s and finish in a background thread even after a scan gave up |
| Cache lived in memory only | Lost on every restart, not shared between workers, always a cold first scan | Persistent store (section 4) |
| An expired copy was thrown away | An outage meant "Not checked" even for packages seen an hour earlier | Expired copies served and flagged as old |
| A failed source hid what the others found | Findings and news dropped for "Not checked" packages | Section 5 |
| New request-tracing middleware crashed (HTTP 500) on a malformed `Content-Length` | Should have been a 400 | Safe parsing |
| The pipeline's OSV step bypassed its test mock | Pipeline tests hit the real network and took 100 s | Fetcher passed through; 4 s again |

## 8. Verified, and not
**Verified** (139 tests across scanner, pipeline, accounts and audit; 75 are scanner tests):
- Fresh reuse, stale fallback, failure backoff, 429 backoff, persistence across a restart, failures never stored as data.
- Per-feed news, the date-parsing fix, the NVD request budget, the deadline and `timeout` status, OSV answered from saved copies, partial-results page.
- Live, once the connection recovered: `refresh_cache` pulled 1,790 KEV ids, 77 RetireJS libraries and 112 news stories. A scan of four packages took 4.0 s with all six sources ok (96 problems, 2 news stories), 2.7 s then 0.5 s through the web app. With the network cut and every copy expired it still checked 4 of 4 packages and found the same 96 problems, with "saved data, 29 hours old" notices for KEV, CVE.org, NVD and OSV.dev.

**Not verified or still open**
- 4 pipeline tests fail on this machine: Gitleaks, Bandit and pip-audit are not installed, and Windows treats proxy variable names case-insensitively. They fail identically on the untouched upstream code.
- The stale-data notice for news feeds (the live outage test fetched feeds over a path it could not block), and RetireJS stale use (no npm package in that scan), were covered by unit tests only.
- The "saved data" and "partly checked" displays were checked in tests, not in the browser during a real outage.
- Terms for the six RSS feeds and the CISA KEV licence have not been read.
- Sign in with GitHub, the admin screens and the pipeline's external scanners were not exercised.
- Oversized *pasted* text gets a bare HTTP 400 instead of the friendly "larger than 1 MB" message (uploaded files get the message).
- The connection on the development machine was intermittently very slow or failing during the session (DNS lookups of 11 s, TLS handshakes of 3–8 s, a 628 KB download taking 282 s). It recovered on 21 Nov. Timeouts above are sized with that in mind.

## 9. Commits
The web/ flatten and the merge of the GitHub sign-in, audit and pipeline work are committed and pushed (`10a2c6d`, `e1add3e`). The deadline setting, saved-copy cache, news fix, partial-results display, tests and this document were still uncommitted when this was written.
