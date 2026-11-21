# Changelog

## Unreleased: pen-test pages in the app, bug checks, offline mode

Branch `claude/pen-tests-7f882d`, on top of `e1add3e`. Nothing here is committed yet.

### Added
- **Security runs page, fully usable in the app** (`/server/`)
  - Targets list and "Add target" form. Name, URL, branch and ref are validated before saving; each add is audited as `target.created`.
  - Run page shows each step's reason, exact command, status and a collapsible **Output** with captured text, re-checked against its recorded hash. Failed steps open by default.
  - **Audit trail page** at `/server/audit/`: latest 200 events plus the hash-chain integrity check.
  - A `skipped` step status (grey badge) for steps that did not run on purpose.
- **Navigation:** one **Security runs** item after a separator: `Scan · Results | Security runs · Admin`.
- **Bug checks:** new `bug_checks` pipeline step, 12 static offline rules (`BC000` to `BC011`) in `pipeline/bugchecks.py`.
- **Local folders as targets:** a target can be a folder inside `SENTINEL_LOCAL_ROOTS`. It is copied and the original only read; uncommitted changes are included. Off by default. Migration `pipeline/0002_target_git_url_charfield` changes `Target.git_url` from URLField to CharField.
- **Offline mode:** `python manage.py update_osv` builds a local OSV index (`pipeline/osvlocal.py`); `SENTINEL_OFFLINE=1` makes `dependency_scan` use it and records `pip_audit` / `npm_audit` as skipped. Output states the data's date. No database = the step fails, never "clean".
- **Admin skin:** the Django admin now shares the app's header, nav, fonts, colours, cards, buttons and light/dark switch (`templates/admin/base_site.html`, `scanner/static/scanner/admin-theme.css` and `.js`). Django's own dark mode is replaced so there is one theme setting.
- **Settings:** `SENTINEL_OFFLINE`, `SENTINEL_OSV_DIR`, `SENTINEL_LOCAL_ROOTS`, `SENTINEL_OPEN_ACCESS` (see `.env.example`). `osv-data/` is git-ignored.
- `README.md` sections: Bug checks, Local folders as targets, Offline mode, Security runs in the app, Temporary open access, Settings added. `TODO.md` tracks follow-ups.

### Changed
- **`dependency_scan` matches exact versions only.** A package with no exact version (a range like `>=5.2,<7`, or a bare name) is now one `unrated` "version not pinned, cannot be checked" finding. Its name-only advisories are no longer reported as vulnerabilities. Before, a single repo produced 330 findings, mostly false positives (e.g. Django 1.x advisories against `Django>=5.2`).
- `Target.git_url` accepts a local path as well as an https URL.
- `check_git_url` also validates local paths (`check_local_path`) against `SENTINEL_LOCAL_ROOTS`.

### Fixed (found by running the pipeline on Windows)
- **Every clone failed** (`getaddrinfo() thread failed to start`): the scrubbed subprocess environment dropped `SYSTEMROOT`. Non-secret Windows system variables are now passed through (`SYSTEMROOT`, `WINDIR`, `TEMP`, `TMP`, `COMSPEC`, `PATHEXT`).
- **pip-audit failed** ("Could not determine home directory"): `USERPROFILE`, `LOCALAPPDATA` and `APPDATA` now point at the run's workspace. This also stops the Windows-Store Python creating a stray `~/` folder in the repo.
- **Cloned source was left on disk after every run:** git's read-only files blocked deletion and the error was hidden. The checkout is now removed with a read-only-aware delete.
- **Runs showed only "FAILED":** the reason (git's stderr) is now visible on the run page.
- **Test suite:** `test_only_network_tools_get_proxy_settings_and_no_credentials` compared env var names case-sensitively; Windows names are case-insensitive. Four other tests that failed only because of the Windows bugs above now pass.

### Temporary (tracked in `TODO.md`)
- **Open access on localhost.** With `DJANGO_DEBUG=1` and a request from `127.0.0.1` / `::1`, Security runs, the audit page and `/admin/` work without signing in, as the first superuser. Controlled by `SENTINEL_OPEN_ACCESS` (default on in DEBUG only). Other addresses and production use normal login and permissions. Sign-ins are audited as `auth.open_access_login`. Remove it by setting `SENTINEL_OPEN_ACCESS=0` or deleting code tagged `TODO(re-enable-login)`: `pipeline/access.py`, the middleware line in `config/settings.py`, and their uses in `pipeline/views.py` and `scanner/context_processors.py`.

### Tests
131 tests pass (4 skipped) across `scanner`, `pipeline`, `accounts` and `audit`. New: `BugCheckTests`, `LocalTargetTests`, `OfflineTests`, and a test for unpinned packages. The target form, audit page and admin skin were checked by hand in the browser and with test requests, not by automated tests.

### Not done
- The offline database has not been downloaded; offline mode is tested with synthetic data only.
- Packaging (Docker or `pip install`) for local-repo use.
- Dependencies in `requirements.txt` are ranges, so `dependency_scan` flags them as unpinned until a lockfile is committed.
- No attack-style testing (ZAP/Nuclei) of a running app; it needs the Docker sandbox from `sentinel-server-spec.md`.
- Admin restyle checked at desktop width only, not on a phone.

### Files
Modified: `.env.example`, `.gitignore`, `README.md`, `config/settings.py`, `pipeline/adapters.py`, `pipeline/gitsafe.py`, `pipeline/models.py`, `pipeline/runner.py`, `pipeline/tests.py`, `pipeline/urls.py`, `pipeline/views.py`, `pipeline/templates/pipeline/{runs,run_detail}.html`, `scanner/context_processors.py`, `scanner/static/scanner/app.css`, `scanner/templates/scanner/base.html`.
New: `CHANGELOG.md`, `TODO.md`, `pipeline/{access,bugchecks,osvlocal}.py`, `pipeline/management/commands/update_osv.py`, `pipeline/migrations/0002_target_git_url_charfield.py`, `pipeline/templates/pipeline/audit.html`, `scanner/static/scanner/admin-theme.{css,js}`, `templates/admin/base_site.html`.
