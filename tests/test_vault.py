import json
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memlog import Vault, crypto, recall
from memlog.recorder import ingest
from memlog.store import LockedError, Store
from memlog.vault import safe_name, slugify

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def seeded(root):
    v = Vault(root)
    v.add("borrow checker pain", when=NOW - timedelta(days=2), source="chat", conv="c1")
    v.add("use Pin", when=NOW - timedelta(days=2), source="chat", role="assistant", conv="c1")
    v.add("gym 100kg", when=NOW - timedelta(days=20), source="notes", role="activity")
    v.add("old career thoughts", when=NOW - timedelta(days=1100), source="chat")
    return v


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_folders_by_kind_year_month_source(self):
        v = seeded(self.d)
        files = [f for f, _, _ in v.tree()]
        self.assertEqual(files, [
            "conversations/2023/09/chat/chat_2023-09-23.jsonl",
            "conversations/2026/09/chat/c1.jsonl",
            "activities/2026/09/notes.jsonl",
        ])
        self.assertTrue((self.d / "vault.json").exists())
        self.assertTrue((self.d / "index" / "memlog.db").exists())

    def test_files_are_one_json_per_line_and_readable(self):
        v = seeded(self.d)
        lines = (self.d / "conversations/2026/09/chat/c1.jsonl").read_text().splitlines()
        recs = [json.loads(l) for l in lines]
        self.assertEqual([r["role"] for r in recs], ["user", "assistant"])
        self.assertEqual(recs[0]["text"], "borrow checker pain")
        self.assertEqual(recs[0]["ts"], "2026-09-25T12:00:00+00:00")
        self.assertEqual(len(recs[0]["uid"]), 16)
        # the index points back at the file
        self.assertEqual(v.index.get(1).loc, "conversations/2026/09/chat/c1.jsonl")
        self.assertEqual(v.index.get(1).uid, recs[0]["uid"])

    def test_permissions(self):
        v = seeded(self.d)
        for p in self.d.rglob("*"):
            mode = stat.S_IMODE(p.stat().st_mode)
            self.assertEqual(mode, 0o700 if p.is_dir() else 0o600, p)

    def test_safe_names(self):
        self.assertEqual(safe_name("chat:2026-09-27"), "chat_2026-09-27")
        self.assertEqual(safe_name("../../etc/passwd"), "etc_passwd")
        self.assertEqual(safe_name(""), "unnamed")
        self.assertEqual(slugify("What did I do last week?"), "what-did-i-do-last-week")

    def test_recall_through_vault(self):
        v = seeded(self.d)
        r = recall(v.store, "borrow checker past week", now=NOW)
        self.assertEqual(r.conversations[0].conv, "c1")

    def test_reindex_from_folders(self):
        v = seeded(self.d)
        uids = sorted(e.uid for e in v)
        v.close()
        shutil.rmtree(self.d / "index")
        v = Vault(self.d)  # empty index + existing folders => automatic reindex
        self.assertEqual(v.count(), 4)
        self.assertEqual(sorted(e.uid for e in v), uids)
        self.assertEqual(v.reindex(), 4)

    def test_hand_dropped_file_is_picked_up_by_reindex(self):
        v = Vault(self.d)
        p = self.d / "activities" / "2026" / "01" / "manual.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"ts": "2026-01-05T09:00:00Z", "role": "activity", "source": "manual",
                                 "text": "wrote this by hand"}) + "\n")
        self.assertEqual(v.reindex(), 1)
        e = next(iter(v))
        self.assertEqual(e.loc, "activities/2026/01/manual.jsonl")
        self.assertTrue(e.uid)

    def test_save_report(self):
        v = seeded(self.d)
        rel = v.save_report("what did I do last week?", "Timeframe: ...", when=NOW)
        self.assertEqual(rel.as_posix(), "reports/2026/2026-09-27_what-did-i-do-last-week.md")
        self.assertIn("Timeframe", (self.d / rel).read_text())

    def test_ingest_old_single_file_db(self):
        old = self.d / "old.db"
        s = Store(old)
        s.add("from the old days", when=NOW - timedelta(days=3), source="chat", conv="x")
        s.close()
        v = Vault(self.d / "vault")
        self.assertEqual(ingest(v, old), 1)
        e = next(iter(v))
        self.assertEqual((e.text, e.source, e.conv), ("from the old days", "chat", "x"))
        self.assertEqual(e.loc, "conversations/2026/09/chat/x.jsonl")


class ForgetTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_forget_rewrites_file_and_prunes_empty_folders(self):
        v = seeded(self.d)
        self.assertEqual(v.forget(conv="c1"), 2)
        self.assertFalse((self.d / "conversations/2026").exists())
        self.assertTrue((self.d / "conversations/2023").exists())
        self.assertEqual(v.count(), 2)
        self.assertNotIn(b"borrow", b"".join(p.read_bytes() for p in self.d.rglob("*.jsonl")))

    def test_forget_one_line_keeps_the_rest(self):
        v = seeded(self.d)
        self.assertEqual(v.forget(ids=[2]), 1)
        recs = (self.d / "conversations/2026/09/chat/c1.jsonl").read_text().splitlines()
        self.assertEqual(len(recs), 1)
        self.assertIn("borrow", recs[0])

    def test_retention_in_config_and_applied_on_open(self):
        v = seeded(self.d)
        self.assertEqual(v.set_retention(365), 1)
        self.assertEqual(json.loads((self.d / "vault.json").read_text())["retention_days"], 365)
        v.add("older", when=NOW - timedelta(days=800))
        v.close()
        v = Vault(self.d)
        self.assertEqual(v.retention_days, 365)
        self.assertEqual({e.text for e in v}, {"borrow checker pain", "use Pin", "gym 100kg"})
        self.assertFalse((self.d / "conversations/2023").exists())

    def test_wipe(self):
        v = seeded(self.d)
        v.wipe()
        self.assertEqual(v.count(), 0)
        self.assertEqual(sorted(p.name for p in self.d.rglob("*")), ["index", "memlog.db", "vault.json"])


class SealedVaultTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _sealed_bytes(self):
        return [p.read_bytes() for p in self.d.rglob("*") if p.is_file() and p.name != "vault.json"]

    def test_lock_seals_every_file_and_config_has_no_secret(self):
        v = seeded(self.d)
        v.save_report("q", "answer text", when=NOW)
        v.lock("pw")
        v.close()
        blobs = self._sealed_bytes()
        self.assertEqual(len(blobs), 5)  # 3 memory files, 1 report, 1 index
        for b in blobs:
            self.assertTrue(crypto.is_encrypted(b))
            self.assertNotIn(b"borrow", b)
            self.assertNotIn(b"answer text", b)
        cfg = json.loads((self.d / "vault.json").read_text())
        self.assertEqual(set(cfg), {"version", "retention_days", "salt", "check", "created"})
        self.assertNotIn("pw", json.dumps(cfg))

    def test_reopen_needs_the_right_passphrase(self):
        seeded(self.d).lock("pw")
        with self.assertRaises(LockedError):
            Vault(self.d)
        with self.assertRaises(crypto.DecryptionError):
            Vault(self.d, passphrase="nope")
        v = Vault(self.d, passphrase="pw")
        self.assertEqual(v.count(), 4)
        self.assertEqual(recall(v.store, "borrow checker", now=NOW).conversations[0].conv, "c1")

    def test_writes_forget_and_reindex_while_sealed(self):
        seeded(self.d).lock("pw")
        v = Vault(self.d, passphrase="pw")
        v.add("still sealed", when=NOW, source="chat", conv="c2")
        self.assertEqual(v.forget(conv="c1"), 2)
        v.close()
        for b in self._sealed_bytes():
            self.assertTrue(crypto.is_encrypted(b))
            self.assertNotIn(b"sealed", b)
        shutil.rmtree(self.d / "index")
        v = Vault(self.d, passphrase="pw")
        self.assertEqual({e.text for e in v}, {"still sealed", "gym 100kg", "old career thoughts"})

    def test_change_passphrase_and_unlock(self):
        seeded(self.d).lock("pw")
        v = Vault(self.d, passphrase="pw")
        v.lock("pw2")
        v.close()
        with self.assertRaises(crypto.DecryptionError):
            Vault(self.d, passphrase="pw")
        v = Vault(self.d, passphrase="pw2")
        v.unlock()
        v.close()
        self.assertFalse(any(crypto.is_encrypted(b) for b in self._sealed_bytes()))
        self.assertEqual(Vault(self.d).count(), 4)

    def test_passphrase_on_open_vault_locks_it(self):
        seeded(self.d).close()
        v = Vault(self.d, passphrase="pw")
        self.assertTrue(v.encrypted)
        v.close()
        self.assertTrue(all(crypto.is_encrypted(b) for b in self._sealed_bytes()))


if __name__ == "__main__":
    unittest.main()
