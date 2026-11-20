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

## Sign in with GitHub (security server)
Staff sign in with their GitHub account; the audit trail then names real people (`github:alice`) instead of "system".

**1. Register an OAuth App** at GitHub → Settings → Developer settings → OAuth Apps → New:
- Homepage URL: `http://127.0.0.1:8000`
- Authorization callback URL: `http://127.0.0.1:8000/auth/github/callback/` (use your real https address on the server)
- Copy the Client ID, and generate a Client secret.

**2. Set these (PowerShell shown) before `runserver`:**
```powershell
$env:SENTINEL_GITHUB_CLIENT_ID="..."
$env:SENTINEL_GITHUB_CLIENT_SECRET="..."
$env:SENTINEL_BASE_URL="http://127.0.0.1:8000"
$env:SENTINEL_GITHUB_USERS="yourgithubname:admin,teammate:operator,reviewer:auditor"
```
**3.** Open http://127.0.0.1:8000/server/ and sign in. To run the admin area, sign in through `/server/` first; the same session opens `/admin/`.

| Role | Can do |
|---|---|
| `admin` | Everything: manage targets, delete scan logs |
| `operator` | See targets and runs, **start runs** |
| `auditor` | Read runs, findings, the audit trail and scan logs, export and verify. **Cannot start runs** |

How it stays safe:
- **Nobody by default.** With `SENTINEL_GITHUB_USERS` empty, nobody can sign in. People not listed are refused and the attempt is recorded.
- **Identity is the GitHub numeric id**, not the name, so someone who later takes a released username doesn't inherit the old account.
- **Removal is immediate.** Take someone out of the list (or change their role) and it applies on their very next request, not their next login. Removal is recorded as `auth.revoked`.
- The login `state` is single-use and checked; the callback address comes from settings, never the Host header; `next` can't redirect off-site.
- The GitHub token is used once to ask "who is this?" and then discarded. It is never stored or logged. Only `read:user` is requested, so it can't read your repos.
- Starting a run is attributed to the person, and refused attempts are recorded as `auth.denied`.

**Runs page:** `/server/` lists runs with who started them, the exact commit, each step's reason and exact command. Runs happen in a background thread for now (a proper job queue comes with the Docker tools), so refresh to see one finish.

## Scanners (security pipeline)
`python manage.py run_pipeline <target>` runs these in order. Each step records its reason, exact command, tool version and binary hash on the audit trail.

| Step | Tool | What it does | Runs target code? |
|---|---|---|---|
| `git_clone` | git | Fetch source, pin the commit (required: nothing else runs without it) | no |
| `secrets_scan` | Gitleaks | Keys/tokens in the repo and the history that was cloned. **Values are redacted and never stored** | no |
| `sast_python` | Bandit | Insecure Python patterns. Source lines are not kept; quoted passwords in messages are scrubbed | no, parses only |
| `pip_audit` | pip-audit | Pinned requirements vs PyPI advisories (`--no-deps --disable-pip`: nothing installed) | no |
| `npm_audit` | npm | Lockfile vs npm advisories (`--package-lock-only`, scripts off) | no |
| `dependency_scan` | OSV.dev | Cross-check of every dependency file | no |

**Install:**
```
pip install -r requirements-tools.txt
go install github.com/zricethezav/gitleaks/v8@v8.21.2     # needs Go; or download the release binary
set SENTINEL_TOOL_DIRS=C:\path\to\tools      # optional: extra folders to look for scanner binaries
```
**Rules the pipeline follows**
- A scanner that finds things is a *result*. A scanner that can't run (missing, or can't reach its advisory service) **fails its step**; it is never shown as clean. One failed scanner doesn't stop the others; a failed clone does.
- Scanners get a minimal environment: no credentials. Only the two that call advisory services also get proxy/CA settings, and only the *names* of those variables are recorded.
- Clone depth defaults to 1, so Gitleaks only sees the latest commit. Set `"depth": N` on the `git_clone` step to scan more history.

## Navigation and session state
A top navigation bar on every page: **Scan · Results · Security runs · Admin**, with who you are, Sign out (or Sign in with GitHub) and the light/dark switch. The current page is highlighted, "Security runs" and "Admin" only appear if your role allows them, and on a phone the links wrap onto their own row.

**Flows**
- A scan redirects to its own address, `/results/<id>/`. Refresh, Back and Forward are safe: nothing is resubmitted, and a refresh never re-runs or re-logs the scan.
- Pages have a breadcrumb and a back button. Back links use your real browser history when you came from this site, so you return to the same scroll position and view; otherwise they follow the link.
- **Edit input** returns to the form with everything you entered; **New scan** starts clean.
- If a scan fails (nothing readable, file too big), the form comes back with your input still in it.
- Sign-in returns you to the page you wanted. Run pages show findings (worst first, filterable), each step's reason and exact command, and refresh themselves once when a running scan finishes.
- Missing and forbidden pages are friendly pages with the nav and a way back.

**What is remembered, and for how long** (this browser session only)
| What | Where | Ends |
|---|---|---|
| Your last 5 scan results | Server-side, in *your* session only; nobody else can open them | Closing the browser, 8 hours, or Sign out |
| Form input (text and uploaded file contents) | Your browser tab (`sessionStorage`) | Closing the tab, or "Clear and start fresh" |
| Result filters, which packages were open, run filters, chosen target | Your browser tab | Closing the tab |
| Light/dark choice | Your browser (`localStorage`) | You change it |

**Caching:** every page (HTML and JSON) is sent `Cache-Control: no-store`, so the browser never reuses a saved copy. Pressing Back after signing out can't show a cached signed-in page, and run status is always current. Static files (CSS, JS, logos) keep normal caching. State is restored by the page itself from the stores above, not from the cache.

## Assets
- `static/scanner/logos/*.png`: 128 px PNGs made from [Devicon](https://devicon.dev) (MIT, see `DEVICON-LICENSE.txt`). The logos are trademarks of their projects. Packages without their own logo get their ecosystem's logo (Python or Node.js). Add a logo by dropping a PNG in and mapping it in `scanner/logos.py`.
- `static/scanner/vendor/material-web.js`: `@material/web` 2.2.0 bundled with esbuild, so the server needs no CDN.

## Production
Set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, and for PostgreSQL `SENTINEL_DB_NAME`, `SENTINEL_DB_USER`, `SENTINEL_DB_PASSWORD`, `SENTINEL_DB_HOST` (plus `pip install "psycopg[binary]"`). Then run `python manage.py collectstatic` and serve with gunicorn behind nginx (spec step 8).
