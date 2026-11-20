"""Append-only, hash-chained audit trail.

Each event stores the hash of the event before it and its own hash, so altering
or deleting any past event breaks every hash that follows. `verify_audit` walks
the chain and reports the first break. Nothing here is ever updated or deleted in
normal use; the records answer "what happened, when, by whom, and why".
"""
import hashlib
import json

from django.db import models

GENESIS = "0" * 64


def canonical(payload) -> str:
    """Stable JSON: same content always serialises to the same bytes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def event_hash(prev_hash: str, fields: dict) -> str:
    return hashlib.sha256((prev_hash + canonical(fields)).encode()).hexdigest()


class AuditEvent(models.Model):
    # A chain, in insertion order. seq is assigned by record() under a lock.
    seq = models.BigIntegerField(unique=True, db_index=True)
    at = models.DateTimeField(db_index=True)
    actor = models.CharField(max_length=150, default="system", db_index=True)
    action = models.CharField(max_length=60, db_index=True)
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict)
    prev_hash = models.CharField(max_length=64)
    this_hash = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["seq"]
        permissions = [("verify_auditevent", "Can verify the audit chain"),
                       ("export_auditevent", "Can export the audit trail")]

    def hashed_fields(self) -> dict:
        return {"seq": self.seq, "at": self.at.isoformat(), "actor": self.actor,
                "action": self.action, "object_type": self.object_type,
                "object_id": self.object_id, "payload": self.payload}

    def compute_hash(self) -> str:
        return event_hash(self.prev_hash, self.hashed_fields())

    def __str__(self):
        return f"#{self.seq} {self.action} by {self.actor} @ {self.at:%Y-%m-%d %H:%M:%S}"


class EvidenceArtifact(models.Model):
    """Raw tool output, addressed by the sha256 of its bytes so stored evidence
    cannot be swapped without detection. The bytes live under AUDIT_EVIDENCE_DIR."""
    sha256 = models.CharField(max_length=64, unique=True, db_index=True)
    size = models.PositiveBigIntegerField()
    content_type = models.CharField(max_length=100, default="text/plain")
    label = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.label or 'artifact'} ({self.sha256[:12]}, {self.size} bytes)"
