# Sentinel

Django web app for scanning project dependencies against public vulnerability databases and security news feeds, styled with **izitho M3** (`scanner/static/scanner/izitho-m3.css`). Nothing you upload is saved.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py test scanner                       # 29 tests
python manage.py runserver
```

- http://127.0.0.1:8000/: scan page (drag-and-drop upload, paste, Other software, live "Detected packages")
- Light theme by default; the sun/moon button top-right switches to dark mode.
- Packages that couldn't be checked are shown as **Not checked** with the reason, never as clean.

## Supported inputs
- **Python**: `requirements.txt` (including inline comments and `--hash=` line continuations), `pyproject.toml` (PEP 621 & Poetry), `poetry.lock`, `uv.lock`
- **JavaScript / Node**: `package.json`, `package-lock.json` (v1–v3), `yarn.lock` (v1 & v2+), `pnpm-lock.yaml` (v6 & v9)
- **Other software**: `<name> <version>` lines (PostgreSQL, PostGIS, Redis, Nginx, SQLite, MySQL, MariaDB)

## Connected data sources & caching
- **OSV.dev** (PyPI + npm batch queries, individual advisories cached 24h)
- **CISA KEV** (actively exploited CVE catalog, cached 24h)
- **CVE.org** & **NVD API 2.0** (CNA/ADP severity enrichment + CPE matching for PostgreSQL, PostGIS, and other infrastructure software; set `NVD_API_KEY` for higher rate limits; cached 24h)
- **RetireJS** (JavaScript library vulnerability definitions, cached 24h)
- **Security News** (6 RSS feeds + Hacker News Algolia search over the last 30 days, cached 1h)
- Advisory deduplication by canonical CVE ID/alias, semantic fixed-version sorting, and CVSS v3.1/v4.0 severity bucketing.

Warm caches via cron with:
```bash
python manage.py refresh_cache
```

## Security & production
- 1 MB upload limit, memory-only upload handler (uploads are never written to disk or DB).
- Per-IP rate limiting (`SENTINEL_RATE_LIMIT`, default 10 scans/min; `SENTINEL_PREVIEW_RATE_LIMIT`, default 30/min).
- Strict `Content-Security-Policy` (`script-src 'self'`), `Permissions-Policy`, HSTS, and `X-Frame-Options: DENY`.
- Copy `.env.example` to `.env`, set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, and run with Docker (`docker compose up --build`) or `gunicorn config.wsgi:application`.

## Assets
- `scanner/static/scanner/logos/*.png`: 128 px PNGs from [Devicon](https://devicon.dev) (MIT, see `DEVICON-LICENSE.txt`). Packages without their own logo fall back to their ecosystem's logo (Python or Node.js).
- `scanner/static/scanner/vendor/material-web.js`: `@material/web` 2.2.0 bundled with esbuild (no CDN required).
