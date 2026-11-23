# Deploying Sentinel to sentinel.uhambo.co.zw (public test site)

A Tier 2 app following `docs/runbooks/add-tier-2-app.md` in the uhambo workspace, gated by decision
0006 (no release without a sec-bundle pass).

**Public-only:** the server runs with `SENTINEL_PUBLIC_ONLY=1`. Only Home, Scan, Results, About and
`/health/` exist. The Django admin, GitHub sign-in (`/auth/`) and Security runs (`/server/`) are not
routed (404), are missing from the nav and the About page, and nginx returns 404 for them as well.

## Intake

| Field | Value |
|---|---|
| App name | `sentinel` |
| Subdomain | `sentinel.uhambo.co.zw` |
| Source folder | `Projects/uhambo/apps/sentinel/` |
| What it does | Checks dependency files against public vulnerability advisories; security news |
| Why it's on the site | portfolio demo / public test |
| Stack | Python 3.12 + Django + Gunicorn (2 workers × 4 threads) |
| Start command | `.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8101` |
| Health URL | `/health/` |
| Local port | 8101 |
| Database | own SQLite in `/var/lib/sentinel/` (scan logs only) |
| Secrets (names only) | `DJANGO_SECRET_KEY` (generated on first install), optional `NVD_API_KEY` |
| Outbound calls | api.osv.dev, services.nvd.nist.gov, cveawg.mitre.org, cisa.gov, raw.githubusercontent.com (RetireJS), security news RSS feeds, hn.algolia.com |
| Inbound webhooks | none |
| Personal data | IP address + package list per scan, deleted after 30 days; uploads are never stored |
| Payments | no |
| Public routes | `/`, `/scan/`, `/parse/`, `/results/…`, `/about/`, `/health/`, `/static/…` |
| Admin/private routes | none routed on this server (public-only) |
| Expected RAM / CPU | ~150–250 MB (capped at 384 MB), idle most of the time |
| Scheduled jobs | `sentinel-maintenance.timer` daily 03:45: delete old scan logs, refresh saved feeds |

## Files

| File | Goes to |
|---|---|
| `env.example` | `/etc/sentinel/env` (first install only; secrets generated) |
| `sentinel.service` | `/etc/systemd/system/` |
| `sentinel-maintenance.service`, `.timer` | `/etc/systemd/system/` |
| `sentinel.uhambo.co.zw.nginx.conf` | `/etc/nginx/sites-available/sentinel.uhambo.co.zw` |
| `sentinel-proxy.nginx.conf` | `/etc/nginx/snippets/sentinel-proxy.conf` |
| `install.sh` | run by the release script; safe to re-run |
| `rollback.sh` | `sudo bash /opt/sentinel/deploy/rollback.sh` puts the previous code back |
| `scripts/package_release.py` | builds `sentinel-app.tgz` + `sentinel-release.sh` (refuses without a fresh sec-bundle pass) |

## Release steps

1. **DNS (you, first time only):** in Webdev's panel add an `A` record `sentinel` → the server IP,
   Cloudflare proxy **on**. Check: `nslookup sentinel.uhambo.co.zw` returns Cloudflare IPs.
2. **sec-bundle (Claude):** run the `sec-bundle` agent in this app's folder. It writes
   `.claude/sec-bundle.pass` (valid 4 hours).
3. **Package (local):** with everything committed,
   `python deploy/scripts/package_release.py <release-id> release`
4. **Upload (local, no sudo):** `scp release/sentinel-app.tgz release/sentinel-release.sh <ssh-user>@<server>:/tmp/`
5. **Install (you, sudo):** `sudo bash /tmp/sentinel-release.sh`
   Step 1 checks the checksum and the pass and changes nothing if either fails. The first run also
   gets the TLS certificate (`certbot certonly --nginx`) and enables the nginx site.
6. **Verify:** see the checklist in the runbook (§5): health 200 through Cloudflare, http → https,
   `/server/`, `/auth/github/login/` and `/admin/` are 404, a burst of scans gets 429, port 8101 on
   127.0.0.1 only, the service runs as `sentinel`.

## Undo

- Previous version: `sudo bash /opt/sentinel/deploy/rollback.sh`
- Take it offline: `sudo systemctl disable --now sentinel sentinel-maintenance.timer`, then
  `sudo rm /etc/nginx/sites-enabled/sentinel.uhambo.co.zw && sudo nginx -t && sudo systemctl reload nginx`
