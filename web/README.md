# Sentinel web app

Django app for the plan in [`../sentinel-spec.md`](../sentinel-spec.md), styled with **izitho M3**: Material Web components themed by `scanner/static/scanner/izitho-m3.css`.

## Run it locally

```bash
cd web
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate           # creates db.sqlite3 and the "Sentinel auditors" group
python manage.py createsuperuser   # your admin login
python manage.py test scanner      # 28 tests
python manage.py runserver
```

- http://127.0.0.1:8000/: scan page (upload, paste, Other software, live "Detected packages")
- Light theme by default. The sun/moon button top-right switches to dark, and the browser remembers the choice.
- Packages a source couldn't check are shown as **Not checked** with the reason, never as clean.

## What works now
- Reads `requirements.txt`, `package.json`, `package-lock.json` (v1–v3) and the Other software box. Lines it can't read are shown, never dropped.
- Checks PyPI + npm packages against **OSV.dev** (exact version, or by name when there is none).
- Dashboard: totals, health rings, severity bars, package cards with logos, filters, PDF (print) and JSON downloads.
- 1 MB upload limit. Uploaded file contents stay in memory and are never saved.

## Usage tracing (who, when, what)
Two logs, linked by a request ID (sent back to the browser as the `X-Request-ID` header and shown on results as "Ref xxxxxxxx"):

| Log | Where | What's in it |
|---|---|---|
| **Access log** | `logs/access.jsonl`, one JSON line per request (rotates at 10 MB, 5 files) | time, request ID, IP, logged-in user, method, path, query, status, ms, bytes, browser, referer. Every request, including attacks and 404s |
| **Scan log** | database, `ScanLog` table | time, request ID, IP, browser, outcome (`ok`, `empty`, `too_large`, `error`), files, package list, unreadable lines, source status, problem counts, duration, error |

**Who can see them (permissions):**
- **Superuser:** everything, and the only role that can delete scan logs.
- **Sentinel auditors** group: staff accounts in it can view and export the scan logs to CSV, but not change or delete them. Add someone in admin → Users: tick *Staff status* and add them to *Sentinel auditors*.
- **Other staff:** can log in to admin but can't see scan logs.

The admin is at `/admin/`. Move it with `SENTINEL_ADMIN_PATH=some-private-path/` so automated scanners don't find it.

**Retention:** scan logs are deleted after 30 days (`SENTINEL_LOG_RETENTION_DAYS`) by running this daily:
```
python manage.py purge_scan_logs
```
Run it from Windows Task Scheduler on your machine, or cron on the server: `0 3 * * * cd /srv/sentinel/web && .venv/bin/python manage.py purge_scan_logs`. The access log rotates by size.

**Real IPs behind nginx:** set `SENTINEL_TRUST_X_FORWARDED_FOR=1` only when Sentinel sits behind your own proxy. Otherwise anyone could fake their IP by sending that header, so it is ignored by default.

The pages tell visitors that their IP and package names are logged and for how long, because IP addresses are personal data under most data-protection laws.

## Pen testing
Point your pen-testing tool at a local or staging copy, not the live server: `DJANGO_DEBUG=0 DJANGO_SECRET_KEY=test DJANGO_ALLOWED_HOSTS=127.0.0.1` with `manage.py runserver`, or gunicorn. Set the tool's User-Agent to something you'll recognise, then filter the access log by it, or by your IP, to see every request it made. Worth focusing on:
- **Upload handling:** files over 1 MB, malformed JSON, huge lockfiles, odd encodings, package names containing HTML/JS (they must appear escaped in results and in the admin).
- **CSRF** on `/scan/` and `/parse/`, and header spoofing (`X-Forwarded-For` must not change the logged IP unless trusted).
- **Admin login:** there's no brute-force lockout yet. If the test confirms that, add `django-axes` before going live.
- **Abuse:** there's no rate limit yet (spec step 8). Expect a load or flood test to succeed until it's added.

## Not connected yet (spec steps 4–6)
NVD, CVE.org, CISA KEV, RetireJS and the news feeds. The page lists them as "not connected yet" in the sources bar instead of pretending they ran. PostgreSQL/PostGIS are read but not checked until NVD is connected.

## Assets
- `static/scanner/logos/*.png`: 128 px PNGs made from [Devicon](https://devicon.dev) (MIT, see `DEVICON-LICENSE.txt`). The logos are trademarks of their projects. Packages without their own logo get their ecosystem's logo (Python or Node.js). Add a logo by dropping a PNG in and mapping it in `scanner/logos.py`.
- `static/scanner/vendor/material-web.js`: `@material/web` 2.2.0 bundled with esbuild, so the server needs no CDN.

## Production
Set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, and for PostgreSQL `SENTINEL_DB_NAME`, `SENTINEL_DB_USER`, `SENTINEL_DB_PASSWORD`, `SENTINEL_DB_HOST` (plus `pip install "psycopg[binary]"`). Then run `python manage.py collectstatic` and serve with gunicorn behind nginx (spec step 8).
