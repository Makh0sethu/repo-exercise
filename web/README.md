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

## Pen testing with ARTEX
[ARTEX](https://github.com/BaaRai/ARTEX) is an autonomous, AI-driven pen-testing platform. It runs real attack tools from a Docker container and records every request as evidence. Only point it at a copy of Sentinel on your own machine.

**1. Run Sentinel in production mode, reachable from Docker** (PowerShell, in `web\`):
```powershell
$env:DJANGO_DEBUG="0"; $env:DJANGO_SECRET_KEY="pentest-only-key"; $env:SENTINEL_HTTPS="0"
$env:DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1,host.docker.internal"
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000 --insecure
```
- `0.0.0.0` lets the ARTEX container reach it. Windows may ask about the firewall: allow **Private networks** only.
- `--insecure` serves CSS/JS while `DEBUG` is off.
- `SENTINEL_HTTPS=0` allows the security cookie over plain http.

**2. Make test accounts** in http://127.0.0.1:8000/admin/: a staff user *not* in Sentinel auditors, and one in it. Give both to ARTEX so it can try to reach scan logs it shouldn't.

**3. Start ARTEX** (Docker Desktop, separate folder):
```powershell
git clone https://github.com/BaaRai/ARTEX.git
cd ARTEX
copy .env.example .env      # set POSTGRES_PASSWORD and your LLM key
docker compose up -d --build
docker compose logs artex | findstr "Setup token"
```
Open http://localhost:8787 and enter the setup token.

**4. Limit it before starting a task:**
- **Scope:** only `host.docker.internal:8000`. `host.docker.internal` is your own PC, so a loose scope lets it probe everything else running there.
- **Noise:** System settings → Target noise → `balanced`. The Django dev server is single-process and a flood just takes it down.
- **Budget:** set `ARTEX_TASK_BUDGET_TOKENS` / `ARTEX_TASK_BUDGET_TURNS` in `.env`. Every step uses your LLM key.
- **Approvals:** for the first run, require approval for Bash tool calls (interception rules), so you see what it runs.

**5. Goal to give it**, for example: *"Test the Sentinel web app at http://host.docker.internal:8000. Focus on:*
- *XSS via package names in uploaded requirements.txt / package.json*
- *CSRF on /scan/ and /parse/*
- *upload size and parser abuse (huge or malformed lockfiles)*
- *IP spoofing with X-Forwarded-For*
- *access control on /admin/scanner/scanlog/ with the two test accounts*
- *admin login brute force*
- *information leakage."*

**6. Match its findings to Sentinel's logs.** ARTEX's requests come from Docker's address, usually `172.x.x.x` or your PC's vEthernet IP; the first lines in the access log show which.
- `logs\access.jsonl` has every request it made, with status codes. Filter by that IP: `Select-String '"ip": "172.' logs\access.jsonl`.
- Admin → Scan logs → search the IP to see every scan it submitted, including failed ones.
- A finding in ARTEX has the exact request; its `X-Request-ID` response header leads to the matching log line.

**Expected findings until spec step 8 is done:** there's no rate limit on scans and no lockout on the admin login (add `django-axes`). Anything else it proves is a real bug.

## Assets
- `static/scanner/logos/*.png`: 128 px PNGs made from [Devicon](https://devicon.dev) (MIT, see `DEVICON-LICENSE.txt`). The logos are trademarks of their projects. Packages without their own logo get their ecosystem's logo (Python or Node.js). Add a logo by dropping a PNG in and mapping it in `scanner/logos.py`.
- `static/scanner/vendor/material-web.js`: `@material/web` 2.2.0 bundled with esbuild, so the server needs no CDN.

## Production
Set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, and for PostgreSQL `SENTINEL_DB_NAME`, `SENTINEL_DB_USER`, `SENTINEL_DB_PASSWORD`, `SENTINEL_DB_HOST` (plus `pip install "psycopg[binary]"`). Then run `python manage.py collectstatic` and serve with gunicorn behind nginx (spec step 8).
