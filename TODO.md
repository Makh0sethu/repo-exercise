# TODO

- [ ] **Re-enable login on Security runs.** Temporarily open on localhost (DEBUG + loopback only) so the pen-test
  pages work without signing in. To restore: set `SENTINEL_OPEN_ACCESS=0`, then remove the bypass
  (`SENTINEL_OPEN_ACCESS` in `config/settings.py`, `pipeline/access.py`, and its uses in `pipeline/views.py`
  and `scanner/context_processors.py`). Search for `TODO(re-enable-login)`.
- [ ] **Build the offline database.** Run `python manage.py update_osv` once (needs internet, large download), then
  set `SENTINEL_OFFLINE=1`. Offline mode is tested with synthetic data only. See README, "Offline mode".
- [ ] **Package it** for use against local repos (Docker image with Gitleaks + the OSV index, or `pip install`).
- [ ] **Pin your dependencies.** `requirements.txt` uses ranges, so `dependency_scan` reports them as "version not pinned".
  Commit a `pip freeze` lockfile to get real results.
