# vulnwatch

Tracks the libraries on your system, checks them for known vulnerabilities, scans the web for recent security stories about them, and builds a standalone HTML dashboard. Python 3.8+, standard library only.

```bash
cd vulnwatch
python vulnwatch.py discover        # inventory this system -> packages.txt (pip + global npm)
python vulnwatch.py scan            # read packages.txt, query the web -> dashboard.html
python vulnwatch.py discover scan   # both
```

Options: `-i FILE` (inventory), `-o FILE` (dashboard), `--days N` (news window, default 30), `--no-news`.

## The text document
`packages.txt`, one package per line. `#` comments are allowed. Versions are required.

```
requests==2.19.0          # PyPI (default)
npm:lodash@4.17.15        # also go:, crates:, maven:, nuget:, gem:, packagist:
```
`discover` generates it; edit it by hand to add projects' dependencies (e.g. output of `pip freeze`).

## Sources
- **OSV.dev**: known vulnerabilities per exact package version, with severity and fixed versions.
- **Security RSS feeds** (The Hacker News, BleepingComputer, Krebs, CISA, GitHub Security Blog, SecurityWeek): stories within the window that mention a package name or one of its CVE ids. Edit `FEEDS` in `vulnwatch.py` to add more.
- **Hacker News**: recent stories per package.

News matching is by name, so it can produce false positives for generic names. Very short or common names are skipped. Re-run on a schedule (cron) to keep the dashboard fresh.
