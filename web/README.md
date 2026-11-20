# Vulnwatch web app

Django app for the plan in [`../vulnwatch-spec.md`](../vulnwatch-spec.md), styled with **izitho M3**: Material Web components themed by `scanner/static/scanner/izitho-m3.css`.

## Run it locally

```bash
cd web
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py test scanner      # 14 tests
python manage.py runserver
```

- http://127.0.0.1:8000/: scan page (upload, paste, Other software, live "Detected packages")
- http://127.0.0.1:8000/demo/: results dashboard with sample data, no network needed

## What works now
- Reads `requirements.txt`, `package.json`, `package-lock.json` (v1–v3) and the Other software box. Lines it can't read are shown, never dropped.
- Checks PyPI + npm packages against **OSV.dev** (exact version, or by name when there is none).
- Dashboard: totals, health rings, severity bars, package cards with logos, filters, PDF (print) and JSON downloads.
- 1 MB upload limit. Uploads stay in memory and are never saved.

## Not connected yet (spec steps 4–6)
NVD, CVE.org, CISA KEV, RetireJS and the news feeds. The page lists them as "not connected yet" in the sources bar instead of pretending they ran. PostgreSQL/PostGIS are read but not checked until NVD is connected.

## Assets
- `static/scanner/logos/*.png`: 128 px PNGs made from [Devicon](https://devicon.dev) (MIT, see `DEVICON-LICENSE.txt`). The logos are trademarks of their projects. Packages without their own logo get their ecosystem's logo (Python or Node.js). Add a logo by dropping a PNG in and mapping it in `scanner/logos.py`.
- `static/scanner/vendor/material-web.js`: `@material/web` 2.2.0 bundled with esbuild, so the server needs no CDN.

## Production
Set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`. Then run `python manage.py collectstatic` and serve with gunicorn behind nginx (spec step 8).
