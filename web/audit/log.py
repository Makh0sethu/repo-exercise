"""record() appends one event to the hash chain, atomically.

Every meaningful action in the server goes through here, so the audit trail is
the single source of truth for what happened and why.
"""
import hashlib
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import GENESIS, AuditEvent, EvidenceArtifact


@transaction.atomic
def record(action: str, *, actor="system", obj=None, object_type="", object_id="", payload=None) -> AuditEvent:
    """Append an event. Locks the tail of the chain so concurrent writers can't
    fork it: seq and prev_hash are read under the lock and applied in order."""
    if obj is not None and not object_type:
        object_type = obj._meta.label_lower
        object_id = str(obj.pk)
    # Lock the latest row (real row lock on PostgreSQL; writes serialise on SQLite).
    last = AuditEvent.objects.select_for_update().order_by("-seq").first()
    seq = (last.seq + 1) if last else 1
    prev_hash = last.this_hash if last else GENESIS
    ev = AuditEvent(seq=seq, at=timezone.now(), actor=str(actor), action=action,
                    object_type=object_type, object_id=str(object_id), payload=payload or {},
                    prev_hash=prev_hash)
    ev.this_hash = ev.compute_hash()
    ev.save()
    return ev


def _evidence_dir() -> Path:
    d = Path(settings.AUDIT_EVIDENCE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_evidence(data: bytes, *, label="", content_type="text/plain") -> EvidenceArtifact:
    """Save raw bytes addressed by their sha256. Re-storing identical bytes is a no-op."""
    if isinstance(data, str):
        data = data.encode()
    digest = hashlib.sha256(data).hexdigest()
    artifact, created = EvidenceArtifact.objects.get_or_create(
        sha256=digest, defaults={"size": len(data), "content_type": content_type, "label": label})
    path = _evidence_dir() / digest
    if created or not path.exists():
        path.write_bytes(data)
    return artifact


def read_evidence(artifact: EvidenceArtifact) -> bytes:
    """Read evidence back and confirm it still matches its recorded hash."""
    data = (_evidence_dir() / artifact.sha256).read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError(f"Evidence {artifact.sha256[:12]} was altered on disk")
    return data


def verify_chain(queryset=None):
    """Walk the chain in order. Returns (ok, errors). errors is a list of
    human-readable breaks (seq, reason) in chain order."""
    events = (queryset or AuditEvent.objects.all()).order_by("seq")
    errors, expected_prev, expected_seq = [], GENESIS, 1
    for ev in events.iterator():
        if ev.seq != expected_seq:
            errors.append((ev.seq, f"sequence gap: expected {expected_seq}, found {ev.seq}"))
        if ev.prev_hash != expected_prev:
            errors.append((ev.seq, "prev_hash does not match the hash of the previous event"))
        if ev.compute_hash() != ev.this_hash:
            errors.append((ev.seq, "event body does not match its stored hash (tampered)"))
        expected_prev = ev.this_hash
        expected_seq = ev.seq + 1
    return (not errors), errors
