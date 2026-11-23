# Sprint 1: Sentinel as a full desktop app, with watching, scheduling and alerts

**Dates:** Mon 23 Nov – Fri 4 Dec 2026 (2 weeks, 10 working days)
**Team:** 5 developers, one per lane (below). Add a product owner and part-time QA if you can.
**Area:** Security pipeline (`pipeline/`), with smaller changes in `scanner/parsers`, `accounts` and the build setup

**Sprint goal:** Sentinel installs as a full Windows app, built on top of the existing web version. It watches the folders and dependency files you choose and re-checks them on a schedule and whenever they change. It runs the extra test packages chosen for your stack, and tells you about *new* problems by email and desktop notification. The same release also ships to the web.

---

## Where we start

| Already there | Missing |
|---|---|
| Runs as a Django web server (Docker files exist) | No desktop build, no installer, no Store listing |
| Localhost "open access" bypass (DEBUG only, see `TODO.md`) | No proper single-user sign-in for a desktop install |
| Targets can be a Git URL **or a local folder** inside `SENTINEL_LOCAL_ROOTS` | No way to pick folders or single files from the UI |
| Runs start by hand (`/server/start/`) in a plain background thread (`pipeline/jobs.py`) | No schedules, no "run when a file changes" |
| One fixed pipeline for every target (`DEFAULT_PIPELINE` in `pipeline/runner.py`) | No way to pick tools per project or stack |
| Gitleaks, Bandit, bug checks, pip-audit, npm audit, OSV cross-check | Only **Python and JavaScript** dependencies are understood |
| Tools found on PATH or `SENTINEL_TOOL_DIRS` | Tools must be installed by hand |
| Hash-chained audit log for every action | No notifications of any kind |

## Decisions

1. **The desktop app comes first, on top of the web version.** It's the same Django app with a native window around it, so there's one codebase and the web keeps working. Lane 1 delivers a working app skeleton by **day 3**. From day 4 every lane develops and tests inside the desktop app as well as the browser.
2. **Desktop shell:** PyInstaller bundles Python, Django and the static files. The app serves on `127.0.0.1` on a random port, shows its UI in a native window (pywebview on Edge WebView2, built into Windows 10 and 11), and adds a tray icon so watching and schedules keep running when the window is closed.
3. **Windows packaging: MSIX**, for the Microsoft Store and for direct download. It needs the "full trust" desktop capability, because the pipeline starts external tools.
4. **Scheduling uses APScheduler** (Python), version 3.x, with `django-apscheduler` to keep jobs in the Sentinel database:
   - On the desktop, a `BackgroundScheduler` runs inside the app process. An MSIX startup task starts the app in the tray when you sign in to Windows, so schedules run without the window open.
   - **Missed runs:** `coalesce=True` plus a long `misfire_grace_time` means a laptop that slept through three checks runs **one** check when it wakes, not three.
   - `max_instances=1` per job: a slow check is never started twice.
   - On the web server, the scheduler runs as **one** separate process (`manage.py run_scheduler`, its own Docker service). APScheduler 3 isn't safe to run in several processes at once, so never inside each web worker.
5. **Test packages are chosen per project.** The built-in checks always run. On top of those, developers pick **test packs** from a catalog, for example "Go: govulncheck + gosec" or "Containers: Trivy". Sentinel detects the stack and suggests packs, but nothing extra runs unless someone switches it on. Every pack is pinned by version and SHA-256 in a manifest, and a download that doesn't match is refused.
6. **New languages go through OSV.** OSV.dev and the offline OSV copy already cover Go, Rust, Java/Kotlin, PHP, Ruby and .NET. Basic support for a language needs a **parser**, not a new tool. Test packs add deeper, language-specific checks on top.
7. **Re-check on a schedule even when nothing changed.** New advisories are published every day, so an unchanged lockfile can become vulnerable overnight.
8. **Notify only on what's new.** Each run is compared with the previous run for the same target. You're told about new findings at or above your chosen severity, and always about anything on CISA KEV or flagged as malware.

---

## Backlog

**M** = must (defines the sprint goal), **S** = should, **C** = could (stretch). Points use the Fibonacci scale; 1 point ≈ half a developer-day, so each person delivers about 20 points in the sprint.

| Lane | Owner | Must | Should | Total committed | Stretch |
|---|---|---|---|---|---|
| 1. Desktop app + web release | Dev 1 | 15 | 5 | 20 | — |
| 2. Watched folders + tracked files | Dev 2 | 18 | — | 18 | 2 |
| 3. Scheduling + notifications | Dev 3 | 18 | — | 18 | 3 |
| 4. Languages + stack detection | Dev 4 | 6 | 11 | 17 | 2 |
| 5. Test pack catalog | Dev 5 | 11 | 7 | 18 | 4 |
| **Sprint** | | **68** | **23** | **91** | **11** |

### Lane 1: Desktop app on top of the web version (goes first)

| # | Story | Pts | Pri |
|---|---|---|---|
| F1 | **Single-user local mode** to replace the DEBUG-only open-access bypass. On first launch the app creates one local admin. The window is signed in with a random per-launch token, and the server accepts only `127.0.0.1`. Closes `TODO(re-enable-login)` for the desktop; the web keeps GitHub sign-in. | 3 | M |
| F2 | **Desktop shell.** Launcher: start Django (waitress) on a free loopback port, open the pywebview window, add a tray icon (open, pause watching, check now, quit). Closing the window keeps Sentinel running in the tray; Quit stops it. **Due day 3.** | 5 | M |
| F3 | **App data and first run.** Database, evidence, logs, tools and the OSV copy live in `%LOCALAPPDATA%\Sentinel`, never in the install folder. Migrations run automatically on start. A short onboarding asks for the first folder to watch and whether to start with Windows. | 2 | M |
| F4 | **MSIX build in CI.** GitHub Actions builds the PyInstaller bundle, packages it as an MSIX with the startup task, test-signs it, and attaches it to each release, with a smoke test that installs it and loads `/`. | 5 | M |
| F5 | **Web release from the same build.** The release also builds the Docker image. `docker-compose.yml` gains a `scheduler` service and SMTP environment variables. | 2 | S |
| F6 | **Microsoft Store submission.** Partner Center listing, privacy policy page (reuse the About page wording), screenshots, age rating, and the full-trust capability justification. Submit by day 9; certification may finish after the sprint. | 3 | S |

**Done when:** a clean Windows machine installs the MSIX, Sentinel opens in its own window, keeps running in the tray after the window closes, and the web version deploys from the same release.

### Lane 2: Watched folders and tracked files

| # | Story | Pts | Pri |
|---|---|---|---|
| A1 | **Models + migration.** `WatchedFolder` (path, recursive, include/exclude globs, active, owner) and `TrackedFile` (path, ecosystem, last SHA-256, last scanned, optional folder link). Both are Targets of kind `folder` / `file`, so the runner, findings and audit log work unchanged. Agree on these models with Lane 3 on day 1. | 3 | M |
| A2 | **Add folders and files.** "Watch a folder" and "Track a file" on `/server/`: native picker in the desktop app, path field on the web. Paths must resolve inside the allowed roots, with symlinks and `..` resolved first. Adding, removing and pausing are audited. | 3 | M |
| A3 | **Auto-discovery.** A watched folder lists the dependency files found inside it (all formats from Lane 4). You can tick extra files to track or untick ones to ignore. Skips `node_modules`, `.venv`, `.git`, `dist` and `build`. | 2 | M |
| A4 | **Change detection.** A file watcher (`watchdog`) on active folders and tracked files. Changes are batched for 30 seconds, so one `npm install` starts one run. A file only counts as changed if its SHA-256 changed. Falls back to polling every 10 minutes on network drives and OneDrive. | 5 | M |
| A5 | **Tracked files page.** Every tracked file with its folder, ecosystem, last check, open problems and a "Check now" button. | 2 | M |
| C1 | **"What's new" diff.** Compare a run with the previous one for the same target. Findings match on tool + rule or advisory + package + location, and come out as new, fixed or still open. Shown on the run page as "3 new · 1 fixed"; Lane 3's notifications use it. | 3 | M |
| A6 | Pause watching while on battery power | 2 | C |

### Lane 3: Scheduling (APScheduler) and notifications

| # | Story | Pts | Pri |
|---|---|---|---|
| B1 | **Schedule model.** Hourly, daily at HH:MM, weekly on a day, or off, for any target, folder or tracked file. Each schedule maps to one APScheduler job in the `django-apscheduler` job store. `Run` gains `trigger` (manual, schedule, file_change) and a schedule link. | 3 | M |
| B2 | **Scheduler service.** A `BackgroundScheduler` started by the desktop app (Lane 1), and `manage.py run_scheduler` for the web. Settings: `coalesce=True`, `misfire_grace_time` 12 h, `max_instances=1`, and at most 2 runs at once per machine. Tests cover a missed run after sleep (one run, not several) and a run still going when the next one is due. | 5 | M |
| B3 | **Schedule UI.** Choose a schedule when adding a folder or file, edit it later, and see "next check in 3 h" and "last checked 09:00 · 2 new" on `/server/`. Changes are audited. | 2 | M |
| C2 | **Notification preferences.** Per user: email on/off, desktop on/off, minimum severity (default high), always notify for CISA KEV and malware, immediate or one daily summary, quiet hours. | 2 | M |
| C3 | **Email.** Django email over SMTP. Settings come from the environment on the web and from Windows Credential Manager on the desktop, never from the database or the repo. Plain-text and HTML versions, with new findings and a link to the run, plus a "Send test email" button. **No secret values in emails:** leaked-secret findings show only the file and line. Every send and failure is an audit event. | 3 | M |
| C4 | **Desktop notifications.** Native Windows notification ("2 new high-severity issues in my-app"); clicking it opens the run in the app. In the browser, the Notification API shows the same while a Sentinel tab is open. | 3 | M |
| C5 | Web push when no browser tab is open (service worker + VAPID keys) | 3 | C |

### Lane 4: Language support and stack detection

Each parser goes in `scanner/parsers.py` with an OSV ecosystem mapping, so the public scanner and the pipeline both get it.

| # | Story | Pts | Pri |
|---|---|---|---|
| D0 | **Stack detection.** From the files found in a folder, report the stack (for example "Go, Docker, Python"). Lane 5 uses it to suggest test packs, and the folder page shows it. | 3 | M |
| D1 | **Go:** `go.mod`, `go.sum` | 2 | M |
| D2 | **Rust:** `Cargo.lock` (`Cargo.toml` shown as unpinned) | 1 | M |
| D3 | **Java/Kotlin:** `pom.xml` (direct dependencies), `gradle.lockfile` | 3 | S |
| D4 | **PHP:** `composer.lock`. **Ruby:** `Gemfile.lock` | 2 | S |
| D5 | **.NET:** `packages.lock.json`, `*.csproj` `PackageReference` | 2 | S |
| D6 | The offline OSV copy (`update_osv`) also downloads the new ecosystems | 1 | S |
| L1 | **Trivy filesystem pack** adapter (dependencies, Dockerfiles, IaC misconfigurations), built to Lane 5's pack interface | 3 | S |
| D7 | Dart/Flutter `pubspec.lock`, Swift `Package.resolved` | 2 | C |

**Done when:** each format has a parser test with a real-world sample, a known-vulnerable fixture gets a finding, and a mixed Go + Python folder is detected as both.

### Lane 5: Test pack catalog (extra tools per stack)

A **test pack** is one or more pinned tools with a pipeline step and an adapter, for example "Go security: govulncheck + gosec".

| # | Story | Pts | Pri |
|---|---|---|---|
| E1 | **Pack manifest.** `pipeline/packs.lock.json`: per pack, its stacks, tools, version, download URL per OS/architecture, SHA-256, licence, and anything it needs on the machine (for example "needs the Go toolchain"). | 2 | M |
| E2 | **Installer.** Downloads only what's in the manifest, refuses any file whose hash doesn't match, unpacks into Sentinel's tool folder, and records each install and removal as an audit event. Available as `manage.py install_pack <name>` and from the UI. | 3 | M |
| E3 | **Choose packs per target.** On a folder or target: Sentinel's suggestions from stack detection, a catalog to add or remove packs, and a "needs Go installed" warning where it applies. Choices are saved on the target and audited. | 3 | M |
| E4 | **Pipeline built from packs.** The runner builds each run's steps from the built-in steps plus that target's packs. The exact step list is already stored on the run, so history shows which packs ran. A missing tool fails its step and never reports "clean". | 3 | M |
| E5 | **First packs:** Go (govulncheck), Rust (cargo-audit), Semgrep with bundled local rules only (no registry calls) | 7 | S |
| E6 | More packs: gosec, bundler-audit (Ruby), Hadolint (Dockerfiles) | 4 | C |

**Done when:** a developer on a Go project accepts the suggested Go pack, it installs with its checksum verified, and the next scheduled run includes the govulncheck step with its findings.

---

## Order of work

| Days | Lane 1: Desktop + web | Lane 2: Watch | Lane 3: Schedule + notify | Lane 4: Languages | Lane 5: Packs |
|---|---|---|---|---|---|
| 1 | F1 local mode | A1 models (with Lane 3) | B1 models (with Lane 2) | D0 stack detection | E1 manifest |
| 2–3 | **F2 shell (app skeleton ready, day 3)**, F3 | A2, A3 | B2 scheduler | D1, D2 | E2 installer |
| 4–5 | F4 MSIX in CI | A4 file watcher | B2 inside the desktop app, B3 | D3, D4 | E4 pipeline from packs |
| 6–7 | F5 web release | A5, C1 diff | C2, C3 email | D5, D6 | E3 pack chooser |
| 8 | First full MSIX with all lanes | Fixes | C4 desktop notifications | L1 Trivy pack | E5 Go, Rust packs |
| 9 | F6 Store submission | Help with testing | Fixes | Help Lane 5 | E5 Semgrep pack |
| 10 | Clean-machine walkthrough, bug fixes, demo | ← all lanes | | | |

Lanes 2–5 develop in the browser for days 1–3, then in the desktop app from day 4.

## Definition of done

- Tests for every story; the full suite passes (currently 175 tests).
- Works in the desktop app **and** on the web, except features that only make sense on one (native picker, Windows notifications, tray).
- Every new action (folder added, schedule changed, pack installed, notification sent or failed) is an audit event, and `verify_audit` passes.
- No secrets in the database, emails, notifications or audit payloads.
- Works with `SENTINEL_OFFLINE=1`; packs that need the network say so instead of failing silently.
- Pages work in light and dark mode at phone width.
- README and `docs/` updated; the About page site map includes the new pages.

## Demo script (sprint review)

1. Install the MSIX on a clean Windows machine; onboarding creates the local admin and asks to start with Windows.
2. Watch `C:\code\demo-app`. Sentinel detects Go and Python, lists `go.mod` and `requirements.txt`, and suggests the Go pack.
3. Accept the Go pack; it installs with its checksum verified. Set "daily 09:00" and use "Check now". The first run sets a baseline, so no notification.
4. Downgrade a package to a vulnerable version and save. Within a minute a desktop notification and an email say "1 new high". Clicking the notification opens the run, which shows the govulncheck step.
5. Close the window: Sentinel stays in the tray. Put the laptop to sleep past 09:00 and wake it: one catch-up run, not several.
6. Open the web version from the same release: same pages, GitHub sign-in, scheduler running as its own service. `verify_audit` passes on both.

## Risks

| Risk | Mitigation |
|---|---|
| **Microsoft Store policy may not allow downloading executables after install**, which is how test packs install. | Check the current Store policies on day 1. Fallbacks: the Store build includes the most common packs inside the MSIX and turns the downloader off, while the direct-download MSIX and the web version keep the full catalog. |
| Store review needs the full-trust capability justified, and certification takes several days. | Submit by day 9; the sprint goal is met by the test-signed MSIX from CI. |
| Every lane depends on the app skeleton (F2). | It's due day 3 and everyone uses the browser until then. If it slips, Lane 4 (Dev 4) helps Lane 1. |
| APScheduler 3 isn't safe in several processes. | Desktop: one app process. Web: one `scheduler` service, never inside web workers. A test checks the web workers don't start it. |
| Some packs need a language toolchain (govulncheck needs Go). | The manifest records this and the chooser warns before you add the pack. The step fails clearly if the toolchain is missing. |
| File watchers miss events on network drives and in OneDrive folders. | 10-minute polling fallback, plus the scheduled run. |
| Bundle size (Python, tools, offline OSV copy). | Packs and the OSV copy download after install (outside the Store build), with progress shown. |
| Email to a misconfigured SMTP server fails silently. | "Send test email" button, failures as audit events, and a banner on `/server/`. |

## Out of scope (Sprint 2 candidates)

- macOS and Linux desktop builds
- Auto-update outside the Store
- Slack, Teams or webhook notifications
- Heavier packs: OWASP Dependency-Check, SpotBugs, Brakeman, `dotnet list package --vulnerable`
- Docker-based work from the server spec: container image scanning, ZAP/Nuclei DAST, sandboxed targets
- GitHub App for private repos

## Open questions

1. Is a desktop install **one person**, or should it also be able to connect to a shared team server?
2. Default notification threshold: **high and above**, or also moderate?
3. Should emails go out from the user's own SMTP account (desktop), a shared Sentinel sender (web), or both?
4. Supported Windows versions: Windows 10 22H2+ and 11 is the suggestion (WebView2 is built in).
5. Who owns the pack catalog going forward: does adding a pack need a review, given that every pack runs third-party code on developers' machines?
