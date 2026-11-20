# Sentinel Security Server

## Summary
A self-hosted server where you deploy an app into an isolated sandbox, run a fixed pipeline of **named, version-pinned, open-source** security tools against it, and get a complete, tamper-evident record of every command, its output, and why it ran. An AI may **read** the results and explain them, but never decides what runs or launches any tool. Everything is open: the orchestrator is your code, the tools are public and pinned, and nothing executes that isn't in the recorded pipeline.

Built as an extension of the Sentinel dependency scanner (`web/`): that scanner becomes the pipeline's first step.

## Why build it instead of using ARTEX
ARTEX (`BaaRai/ARTEX`, a fork of `Autumn-27/ARTEX`, AGPL-3.0) is an autonomous agent that *decides on its own* which exploit tools to run. That autonomy is the hardest part to audit and the part we don't trust. This design keeps the power (deploy an app, run real tools, keep evidence) but removes the black box: a human defines the pipeline, the run is deterministic, and the AI is advisory and logged.

## Goals and success criteria
- Deploy any app (Docker image, compose file, or a git repo we build) into a sandbox with no route to internal systems.
- Run four kinds of test: dependency, SAST + secrets (source), container image, and DAST (running app).
- **Every action is on an append-only, hash-chained audit log**; `verify_audit` proves the chain was not altered. Tampering with any past entry is detectable.
- Every finding links to the exact tool output that proves it.
- A run is reproducible: same target version + same pipeline definition → same commands, recorded verbatim.
- The AI advisor can explain and prioritise, and its full prompt + reply is itself an audit entry; it cannot change a verdict or run a tool.

## Non-goals
- **No autonomous exploitation.** No agent choosing or launching tools (the ARTEX model). AI is read-only.
- **No testing of targets you don't control.** Scope is an app you deploy here, not arbitrary internet hosts.
- Not a replacement for a human pentester; it runs known tools and records them well.
- No accounts for the public; this is an internal tool with staff logins and roles.

## Core concepts (data model)
| Entity | What it is |
|---|---|
| **Target** | An app under test. Source = a Docker image ref, a compose file, or a git repo + revision we build into an image. |
| **Run** | One execution of a pipeline against one target version. Immutable once finished. Carries the exact pipeline definition used. |
| **Step** | One tool invocation in a run. Records: tool name, pinned image **digest**, exact argv, sandbox id, start/end, exit code, raw stdout/stderr (stored as evidence artifacts), parsed findings, and `reason` (why this step is in the pipeline). |
| **Finding** | A normalised result: severity, title, location, and a pointer to the raw evidence that proves it. |
| **Advisory** | Optional AI output for a run or finding: a plain-language explanation / prioritisation. Stores the full prompt and reply. Read-only. |
| **AuditEvent** | Append-only, hash-chained record of every meaningful action (see below). |

## The audit log (the heart of it)
- One `AuditEvent` row per meaningful action: `run.created`, `step.started`, `step.finished`, `finding.recorded`, `advisory.requested`, `advisory.received`, `report.exported`, `config.changed`, `auth.login`, `auth.denied`, `pipeline.defined`.
- Each event stores: time, actor (user or `system`), action, target object, a JSON payload, the **hash of the previous event**, and its **own hash** (`sha256(prev_hash + canonical_json(this event without its own hash))`). This is a hash chain: altering or deleting any past event breaks every hash after it.
- `manage.py verify_audit` walks the chain and reports the first break, if any.
- The log records **what happened and why**: every step carries the reason it ran, every AI call carries its prompt, every denied action is recorded.
- Raw tool output is stored as evidence artifacts (files on disk / object store), referenced by sha256 so the stored bytes can't be swapped silently either.

## The tools (all open-source, pinned by image digest)
| Step | Tool | Needs |
|---|---|---|
| Dependency | Sentinel's OSV scanner (`web/`) + Trivy `fs` on lockfiles | source or lockfiles |
| SAST | Semgrep | source |
| Secrets | Gitleaks | source (git history) |
| Container image | Trivy `image` | the built/`pulled` image |
| DAST | OWASP ZAP baseline, then Nuclei | the running app in the sandbox |

Each tool runs in its own pinned container (recorded by digest, not tag). The pipeline definition is declarative YAML, stored with the run, so you can read exactly what a run did.

## Deployment & sandbox
- **Server** runs as Docker Compose: Django (orchestrator + UI + audit), PostgreSQL, and a **runner** with access to the Docker socket to launch tool and target containers.
- **Target** is deployed into a dedicated, **internal** Docker network with **no egress** to your real systems; DAST tools run inside that network. The target is torn down after the run; evidence is kept.
- A git-repo target is built with a recorded Dockerfile/buildpack; the build is itself a recorded step (its own `reason`, command, output), because the build is part of the attack surface.

## Security of the server itself
- Staff logins with roles: **operator** (start runs, read), **auditor** (read + export + verify, no run), **admin** (manage pipelines, users). Reuses the auditor pattern already built for scan logs.
- The runner holds Docker socket access and is the most sensitive component; it only executes pipeline-defined commands with pinned digests, never free-form input.
- Secrets (LLM key for the advisor, registry creds) via environment, never in the repo or the audit payloads.
- The server is itself a deployable app, so it can be run through its own pipeline.

## Plan and sequencing
1. **Audit core** — hash-chained `AuditEvent`, `record()` helper, `verify_audit` command, evidence-artifact store. *Fully testable without Docker.* ← build first
2. **Data model** — Target, Run, Step, Finding, Advisory + admin screens with the role split.
3. **Pipeline runner** — declarative YAML pipeline, an adapter interface, and the **dependency step** wired to Sentinel's existing scanner. Tested with a fake adapter + the real (mocked) OSV one.
4. **Tool adapters needing Docker** — Trivy (fs + image), Semgrep, Gitleaks: each records its exact pinned command; run on the user's server.
5. **Target deployment + sandbox** — build/pull, isolated network, teardown.
6. **DAST** — ZAP baseline + Nuclei against the sandboxed app.
7. **AI advisor** — read-only, logged prompt/reply, cannot run tools or alter findings.
8. **Reports & exports**, then run the server through its own pipeline.

## Progress
- **Increment 1 done:** hash-chained audit core (`web/audit/`).
- **Increment 2 done:** git-repo targets and a runner (`web/pipeline/`). Steps so far: `git_clone` (pins the exact commit) and `dependency_scan` (OSV.dev). Run one with `python manage.py run_pipeline <name> --add <github-url> [--ref branch]`. First target: `Makh0sethu/repo-exercise`.
- Clone URLs are restricted to https on allowed hosts (default `github.com`); refs cannot inject git options; a failing or unreachable tool fails the step, never reports "clean".
- **Private repos:** `SENTINEL_GITHUB_TOKEN` is passed to git through the environment only, and redacted from stored output. Planned replacement: a GitHub App with read-only access (see the auth notes below).

- **Increment 3 done:** Sign in with GitHub (`web/accounts/`): operator / auditor / admin roles, fail-closed allowlist, immediate revocation, every login, denial and run attributed to a named person. `/server/` page to start and watch runs. Setup is in `web/README.md`.

- **Increment 4 done:** scanners that need no Docker: Gitleaks (secrets), Bandit (Python SAST), pip-audit and npm audit (dependencies), plus the OSV cross-check. Tested against a real private repo. **Still needs Docker (not built):** Trivy image scanning, Semgrep with registry rules, ZAP/Nuclei DAST, and deploying a target in a sandbox.

- **Increment 5 done:** navigation and flows. Top nav bar, breadcrumbs and back buttons, scan → `/results/<id>/` redirect, session-only history of the last 5 results, form/filter state kept per tab, run detail page with findings, friendly 403/404 pages, `no-store` on all dynamic pages. 106 tests plus a 32-step real-browser walkthrough.

## GitHub authentication
Sign-in is built (above). Reading *private repos* is still the token route; the GitHub App described below is the planned replacement.
Public repos need none. For private repos the options, safest first: **GitHub App** installed on only the chosen repos, Contents read-only, short-lived installation tokens minted per run; **per-repo read-only deploy key**; **fine-grained token** scoped to chosen repos. Avoid a classic personal token. "Sign in with GitHub" (OAuth) is for *who may use the server*, which is a separate question from how the server reads repos.

## Open questions
- Where evidence artifacts live: local disk first; object store (S3-compatible) later?
- Which AI provider for the advisor, and does it run fully locally (Ollama) to keep evidence on-prem?
- Runner isolation: Docker socket vs a rootless/again-sandboxed runner (gVisor, sysbox) for stronger separation.
