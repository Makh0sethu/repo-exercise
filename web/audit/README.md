# Audit trail (`audit` app)

The tamper-evident core of the Sentinel Security Server. Every meaningful action is an append-only event in a **hash chain**: each event stores the hash of the one before it, so changing or deleting any past event breaks every hash after it.

## Use it
```python
from audit.log import record, store_evidence

record("run.created", actor=request.user.username,
       payload={"target": "myapp:1.2", "reason": "nightly scan"})

art = store_evidence(raw_tool_output, label="trivy image")   # bytes, addressed by sha256
record("step.finished", payload={"tool": "trivy", "reason": "scan image for OS CVEs",
                                 "evidence": art.sha256, "findings": 1})
```
`record()` is atomic and locks the tail of the chain, so concurrent writers can't fork it.

## Verify it
```
python manage.py verify_audit      # exit 0 if intact, 1 (with the first break) if tampered
```
Run it on a schedule and after any DB maintenance. The admin list also shows a per-row "hash ok" flag.

## Guarantees and limits
- **Detects** silent edits or deletes of past events, and evidence bytes swapped on disk (checked on read against the recorded sha256).
- It does **not** prevent a DB admin from rewriting the whole chain from a point forward. For that, periodically copy the latest `this_hash` somewhere append-only off-box (print it, email it, write it to a WORM bucket); any later history must still end in a chain that reproduces a hash you already saw.
- Audit events are never edited or deleted through the app or admin; that's enforced in `audit/admin.py`.

## Roles
`verify_auditevent` and `export_auditevent` permissions gate verifying and CSV export; pair them with an "auditor" group as the `scanner` app does.
