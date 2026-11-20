import hashlib
import tempfile
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase, override_settings

from .log import read_evidence, record, store_evidence, verify_chain
from .models import GENESIS, AuditEvent


class ChainTests(TestCase):
    def test_first_event_links_to_genesis(self):
        ev = record("run.created", actor="alice", payload={"target": "demo"})
        self.assertEqual((ev.seq, ev.prev_hash), (1, GENESIS))
        self.assertEqual(ev.this_hash, ev.compute_hash())

    def test_each_event_links_to_the_previous(self):
        a = record("a"); b = record("b"); c = record("c")
        self.assertEqual(b.prev_hash, a.this_hash)
        self.assertEqual(c.prev_hash, b.this_hash)
        self.assertEqual([a.seq, b.seq, c.seq], [1, 2, 3])

    def test_clean_chain_verifies(self):
        for i in range(5):
            record("step.finished", payload={"i": i})
        ok, errors = verify_chain()
        self.assertTrue(ok, errors)

    def test_tampering_with_a_payload_is_detected(self):
        record("a"); target = record("auth.login", actor="mallory"); record("c")
        # Edit a past event directly in the DB, as an attacker with table access would.
        AuditEvent.objects.filter(pk=target.pk).update(actor="admin")
        ok, errors = verify_chain()
        self.assertFalse(ok)
        self.assertEqual(errors[0][0], target.seq)
        self.assertIn("tampered", errors[0][1])

    def test_deleting_an_event_breaks_the_chain(self):
        record("a"); mid = record("b"); record("c")
        AuditEvent.objects.filter(pk=mid.pk).delete()
        ok, errors = verify_chain()
        self.assertFalse(ok)
        self.assertTrue(any("sequence gap" in r for _, r in errors))

    def test_object_binding_records_type_and_id(self):
        u = User.objects.create_user("u")
        ev = record("config.changed", obj=u)
        self.assertEqual((ev.object_type, ev.object_id), ("auth.user", str(u.pk)))

    def test_verify_audit_command_passes_and_fails(self):
        record("a"); bad = record("b")
        call_command("verify_audit")  # no exception
        AuditEvent.objects.filter(pk=bad.pk).update(action="forged")
        with self.assertRaises(SystemExit):
            call_command("verify_audit")


class EvidenceTests(TestCase):
    def test_store_and_read_back_by_hash(self):
        with tempfile.TemporaryDirectory() as d, override_settings(AUDIT_EVIDENCE_DIR=d):
            art = store_evidence(b"nuclei raw output", label="nuclei")
            self.assertEqual(art.sha256, hashlib.sha256(b"nuclei raw output").hexdigest())
            self.assertEqual(read_evidence(art), b"nuclei raw output")

    def test_identical_bytes_are_stored_once(self):
        with tempfile.TemporaryDirectory() as d, override_settings(AUDIT_EVIDENCE_DIR=d):
            a = store_evidence(b"same")
            b = store_evidence(b"same")
            self.assertEqual(a.pk, b.pk)

    def test_altered_evidence_on_disk_is_detected(self):
        with tempfile.TemporaryDirectory() as d, override_settings(AUDIT_EVIDENCE_DIR=d):
            art = store_evidence(b"original", label="zap")
            (Path(d) / art.sha256).write_bytes(b"swapped")
            with self.assertRaises(ValueError):
                read_evidence(art)


class AdminPermissionTests(TestCase):
    def setUp(self):
        record("auth.login", actor="someone")

    def test_audit_events_cannot_be_deleted_in_admin(self):
        from .admin import AuditEventAdmin
        from .models import AuditEvent as M
        a = AuditEventAdmin(M, None)
        self.assertFalse(a.has_delete_permission(None))
        self.assertFalse(a.has_change_permission(None))
        self.assertFalse(a.has_add_permission(None))
