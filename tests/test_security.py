import ast
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from memlog import crypto
from memlog.store import LockedError, Store
from memlog.summarize import NetworkDisabled, llm_summary
from memlog.retrieve import recall

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
PKG = Path(__file__).resolve().parent.parent / "memlog"


class CryptoTests(unittest.TestCase):
    def test_roundtrip(self):
        blob = crypto.encrypt_bytes(b"hello memories", "pw")
        self.assertTrue(blob.startswith(crypto.MAGIC))
        self.assertNotIn(b"hello", blob)
        self.assertEqual(crypto.decrypt_bytes(blob, "pw"), b"hello memories")

    def test_wrong_passphrase(self):
        blob = crypto.encrypt_bytes(b"secret", "right")
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt_bytes(blob, "wrong")

    def test_tamper_detected(self):
        blob = bytearray(crypto.encrypt_bytes(b"secret" * 50, "pw"))
        blob[len(crypto.MAGIC) + crypto.SALT_LEN + crypto.NONCE_LEN + 3] ^= 0x01
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt_bytes(bytes(blob), "pw")
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt_bytes(bytes(blob)[:-1], "pw")

    def test_fresh_salt_and_nonce_each_time(self):
        a, b = crypto.encrypt_bytes(b"x", "pw"), crypto.encrypt_bytes(b"x", "pw")
        self.assertNotEqual(a, b)

    def test_empty_passphrase_rejected(self):
        with self.assertRaises(ValueError):
            crypto.encrypt_bytes(b"x", "")

    def test_write_private_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "f.bin"
            crypto.write_private(p, b"data")
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
            self.assertEqual(p.read_bytes(), b"data")

    def test_shred(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f"
            p.write_bytes(b"gone")
            self.assertTrue(crypto.shred(p))
            self.assertFalse(p.exists())
            self.assertFalse(crypto.shred(p))


class EncryptedStoreTests(unittest.TestCase):
    def test_plaintext_never_hits_disk(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            with Store(path, passphrase="pw") as s:
                s.add("the secret plan for the hiking trip", when=NOW)
                self.assertTrue(s.encrypted)
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(crypto.MAGIC))
            self.assertNotIn(b"hiking", raw)
            self.assertNotIn(b"SQLite format", raw)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["m.db"])  # no temp files, no journals

    def test_reopen_and_search(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            with Store(path, passphrase="pw") as s:
                s.add("borrow checker again", when=NOW - timedelta(days=1), conv="c1")
            with Store(path, passphrase="pw") as s:
                self.assertEqual(s.count(), 1)
                self.assertEqual(recall(s, "borrow checker", now=NOW).conversations[0].conv, "c1")

    def test_locked_without_passphrase(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            Store(path, passphrase="pw").close()
            with self.assertRaises(LockedError):
                Store(path)
            with self.assertRaises(crypto.DecryptionError):
                Store(path, passphrase="nope")

    def test_lock_existing_plaintext_and_unlock(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            with Store(path) as s:
                s.add("plain first", when=NOW)
            self.assertIn(b"plain first", path.read_bytes())
            with Store(path) as s:
                s.change_passphrase("pw")
                self.assertTrue(s.encrypted)
                s.add("added after lock", when=NOW)
            self.assertTrue(crypto.is_encrypted_file(path))
            with Store(path, passphrase="pw") as s:
                self.assertEqual(s.count(), 2)
                s.change_passphrase("pw2")
            with Store(path, passphrase="pw2") as s:
                s.change_passphrase(None)
                self.assertFalse(s.encrypted)
            self.assertFalse(crypto.is_encrypted_file(path))
            self.assertEqual(Store(path).count(), 2)

    def test_unlock_with_retention_set_does_not_hang(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            with Store(path, passphrase="pw") as s:
                s.add("keep", when=NOW)
                s.set_retention(30)
            with Store(path, passphrase="pw") as s:  # purge on open runs a DELETE that removes nothing
                s.change_passphrase(None)
            self.assertFalse(crypto.is_encrypted_file(path))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(Store(path).count(), 1)

    def test_plaintext_file_created_owner_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            Store(path).close()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_deferred_batches_saves(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            with Store(path, passphrase="pw") as s:
                with mock.patch.object(s, "_persist", wraps=s._persist) as persist:
                    s.add_many([{"text": f"entry {i}", "when": NOW} for i in range(20)])
                    self.assertEqual(persist.call_count, 1)

    def test_wipe_shreds_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            s = Store(path)
            s.add("bye", when=NOW)
            s.wipe()
            self.assertFalse(path.exists())
            self.assertEqual(s.count(), 0)


class RetentionTests(unittest.TestCase):
    def seeded(self, path=":memory:"):
        s = Store(path)
        s.add("old", when=NOW - timedelta(days=100), conv="old")
        s.add("mid", when=NOW - timedelta(days=40), conv="mid")
        s.add("new", when=NOW - timedelta(days=1), conv="new")
        return s

    def test_set_retention_purges_now_and_on_open(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            s = self.seeded(path)
            self.assertIsNone(s.retention_days)
            self.assertEqual(s.set_retention(60), 1)
            self.assertEqual({e.text for e in s}, {"mid", "new"})
            s.add("older again", when=NOW - timedelta(days=90))
            s.close()
            s = Store(path)  # purge on open
            self.assertEqual(s.retention_days, 60)
            self.assertEqual({e.text for e in s}, {"mid", "new"})
            s.set_retention(None)
            self.assertIsNone(Store(path).retention_days)

    def test_invalid_retention(self):
        with self.assertRaises(ValueError):
            Store(":memory:").set_retention(0)

    def test_forget_filters(self):
        s = self.seeded()
        self.assertEqual(s.forget(before=NOW - timedelta(days=50)), 1)
        self.assertEqual(s.forget(conv="mid"), 1)
        self.assertEqual(s.forget(ids=[999]), 0)
        with self.assertRaises(ValueError):
            s.forget()
        self.assertEqual(s.count(), 1)

    def test_forgotten_text_is_gone_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            s = Store(path)
            s.add("remember the zebra password", when=NOW)
            s.forget(before=NOW + timedelta(days=1))
            s.close()
            self.assertNotIn(b"zebra", path.read_bytes())


class NoNetworkTests(unittest.TestCase):
    # urllib.parse is a string parser that pathlib itself imports; the request side is what matters.
    FORBIDDEN = {"socket", "urllib.request", "urllib.error", "http", "requests", "httpx", "aiohttp",
                 "ssl", "smtplib", "ftplib", "telnetlib", "xmlrpc", "webbrowser", "subprocess"}

    @classmethod
    def forbidden(cls, module):
        return any(module == f or module.startswith(f + ".") for f in cls.FORBIDDEN)

    def test_package_imports_no_network_modules(self):
        offenders = []
        for py in PKG.glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if self.forbidden(name):
                        offenders.append(f"{py.name}: {name}")
        # The notepad launches $EDITOR on a private temp file; that is the only process it starts.
        self.assertEqual(offenders, ["notepad.py: subprocess"])

    def test_only_litellm_is_optional_and_lazy(self):
        for py in PKG.glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mod = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
                    if mod and mod.split(".")[0] == "litellm":
                        self.assertEqual(py.name, "summarize.py")
                        self.assertGreater(node.col_offset, 0, "litellm must be imported inside a function")

    def test_no_network_env_blocks_llm(self):
        result = recall(Store(":memory:"), "anything", now=NOW)
        with mock.patch.dict(os.environ, {"MEMLOG_NO_NETWORK": "1", "MEMLOG_LLM_MODEL": "x/y"}):
            with self.assertRaises(NetworkDisabled):
                llm_summary(result)

    def test_no_model_means_no_call(self):
        result = recall(Store(":memory:"), "anything", now=NOW)
        with mock.patch.dict(os.environ, {"MEMLOG_LLM_MODEL": ""}, clear=False):
            os.environ.pop("MEMLOG_LLM_MODEL", None)
            os.environ.pop("MEMLOG_NO_NETWORK", None)
            self.assertIsNone(llm_summary(result))

    def test_importing_memlog_loads_no_network_modules(self):
        import subprocess  # test-only; the package itself never uses it
        code = (
            "import sys; import memlog, memlog.cli, memlog.recorder, memlog.summarize, memlog.crypto; "
            "F = %r; bad = sorted(m for m in sys.modules if any(m == f or m.startswith(f + '.') for f in F)); print(bad)"
            % sorted(self.FORBIDDEN - {"subprocess"})
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=PKG.parent)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
