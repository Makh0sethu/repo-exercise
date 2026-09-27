import os
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memlog import Vault, crypto, recall
from memlog.notepad import parse_sections

NOW = datetime(2026, 9, 27, 14, 3, tzinfo=timezone.utc)


class ParseTests(unittest.TestCase):
    def test_sections_and_stamps(self):
        secs = parse_sections("intro line\n\n## 2026-09-27 14:03\nBuy milk\n\n## Ideas\nfree text\n")
        self.assertEqual([s.heading for s in secs], ["", "2026-09-27 14:03", "Ideas"])
        self.assertEqual(secs[1].when, NOW)
        self.assertIsNone(secs[2].when)
        self.assertEqual(secs[0].text, "intro line")

    def test_empty(self):
        self.assertEqual(parse_sections(""), [])


class NotepadTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.v = Vault(self.d)
        self.pad = self.v.notes

    def tearDown(self):
        self.v.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_quick_note_goes_to_journal_page(self):
        name = self.pad.add("Buy milk. Ask about the airflow catchup bug.", when=NOW)
        self.assertEqual(name, "journal/2026/2026-09-27")
        path = self.d / "notes" / "journal" / "2026" / "2026-09-27.md"
        self.assertEqual(path.read_text(), "## 2026-09-27 14:03\nBuy milk. Ask about the airflow catchup bug.\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.pad.add("second thought", when=NOW + timedelta(minutes=30))
        self.assertEqual(path.read_text().count("## "), 2)

    def test_named_note_and_listing(self):
        self.pad.add("watch shell history", name="ideas", when=NOW)
        self.pad.write("todo", "- [ ] write tests\n- [ ] ship\n")
        names = [(n.name, n.sections) for n in self.pad.list()]
        self.assertEqual(names, [("ideas", 1), ("todo", 1)])
        self.assertEqual(self.pad.read("todo"), "- [ ] write tests\n- [ ] ship\n")

    def test_bad_names_rejected(self):
        for bad in ("../secret", "", "..", "/etc/passwd", "a/../b"):
            with self.assertRaises(ValueError, msg=bad):
                self.pad.path(bad)
        self.assertEqual(self.pad.path("my note!/2026").as_posix(), (self.d / "notes/my-note-/2026.md").as_posix())

    def test_notes_are_searchable_per_section(self):
        self.pad.add("airflow catchup runs the DAG twice", name="work", when=NOW - timedelta(days=1))
        self.pad.add("plan the drakensberg hike", name="work", when=NOW - timedelta(days=40))
        r = recall(self.v.store, "airflow this week", now=NOW)
        self.assertEqual(len(r.hits), 1)
        self.assertEqual(r.hits[0].entry.role, "note")
        self.assertEqual(r.hits[0].entry.conv, "note:work")
        self.assertEqual(r.hits[0].entry.loc, "notes/work.md")
        self.assertEqual(len(recall(self.v.store, "hike", now=NOW).hits), 1)

    def test_write_reindexes_and_delete_drops_index(self):
        self.pad.write("n", "## 2026-09-01\nalpha\n\n## 2026-09-02\nbeta\n")
        self.assertEqual(sorted(e.text for e in self.v), ["alpha", "beta"])
        self.pad.write("n", "## 2026-09-01\ngamma\n")
        self.assertEqual([e.text for e in self.v], ["gamma"])
        self.assertTrue(self.pad.delete("n"))
        self.assertEqual(self.v.count(), 0)
        self.assertFalse((self.d / "notes").exists())
        self.assertFalse(self.pad.delete("n"))

    def test_rename(self):
        self.pad.write("old", "## 2026-09-01\ntext\n")
        self.pad.rename("old", "new")
        self.assertEqual([n.name for n in self.pad.list()], ["new"])
        self.assertEqual(next(iter(self.v)).conv, "note:new")
        with self.assertRaises(FileNotFoundError):
            self.pad.rename("old", "x")

    def test_forget_removes_a_section_and_counts_it(self):
        self.pad.write("n", "## 2026-09-01\nalpha\n\n## 2026-09-20\nbeta\n")
        self.assertEqual(self.v.forget(conv="note:n", before=NOW - timedelta(days=10)), 1)
        self.assertEqual(self.pad.read("n"), "## 2026-09-20\nbeta\n")
        self.assertEqual([e.text for e in self.v], ["beta"])

    def test_retention_never_expires_notes(self):
        self.pad.write("keep", "## 2020-01-01\nancient wisdom\n")
        self.v.add("ancient chat", when=NOW - timedelta(days=2000))
        self.assertEqual(self.v.set_retention(30), 1)
        self.assertEqual([e.text for e in self.v], ["ancient wisdom"])
        self.assertTrue(self.pad.exists("keep"))

    def test_reindex_includes_notes_and_hand_written_files(self):
        self.pad.write("n", "## 2026-09-01\nalpha\n")
        (self.d / "notes" / "hand.md").write_text("no headings, just text\n")
        self.assertEqual(self.v.reindex(), 2)
        self.assertEqual({e.conv for e in self.v}, {"note:n", "note:hand"})

    def test_tree_lists_notes(self):
        self.pad.add("x", name="ideas", when=NOW)
        self.assertIn(("notes/ideas.md", 1, (self.d / "notes/ideas.md").stat().st_size), self.v.tree())

    def test_edit_with_editor_and_temp_file_cleanup(self):
        self.pad.write("n", "before\n")
        script = self.d / "fake-editor.sh"
        script.write_text("#!/bin/sh\nprintf 'after\\n' > \"$1\"\ncp \"$1\" \"$MEMLOG_TEST_COPY\"\n")
        script.chmod(0o700)
        copy = self.d / "seen.txt"
        os.environ["MEMLOG_TEST_COPY"] = str(copy)
        try:
            self.assertTrue(self.pad.edit("n", editor=str(script)))
        finally:
            os.environ.pop("MEMLOG_TEST_COPY", None)
        self.assertEqual(self.pad.read("n"), "after\n")
        self.assertEqual(copy.read_text(), "after\n")  # the editor saw a plain file
        self.assertFalse(self.pad.edit("n", editor="true"))  # editor that changes nothing
        with self.assertRaises(RuntimeError):
            self.pad.edit("n", editor="")


class NoteCliTests(unittest.TestCase):
    def test_option_parsing_anywhere_in_the_words(self):
        from memlog.cli import _note_options
        self.assertEqual(_note_options(["add", "--to", "ideas", "watch", "shell"]),
                         (["add", "watch", "shell"], {"to": "ideas", "when": "", "yes": False}))
        self.assertEqual(_note_options(["--to=x", "delete", "n", "-y"])[1], {"to": "x", "when": "", "yes": True})
        self.assertEqual(_note_options(["--", "--to", "literal"])[0], ["--to", "literal"])

    def test_cli_round_trip(self):
        from memlog.cli import main
        d = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(main(["--root", str(d), "note", "Buy milk", "--when", "2026-09-27T14:03:00+00:00"]), 0)
            self.assertEqual(main(["--root", str(d), "note", "add", "--to", "ideas", "watch shell history"]), 0)
            self.assertEqual(main(["--root", str(d), "note", "rename", "ideas", "plans"]), 0)
            self.assertEqual(main(["--root", str(d), "note", "delete", "plans", "--yes"]), 0)
            self.assertEqual(main(["--root", str(d), "note", "show", "nope"]), 1)
            self.assertEqual(sorted(p.name for p in (d / "notes").rglob("*.md")), ["2026-09-27.md"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class SealedNotepadTests(unittest.TestCase):
    def test_notes_sealed_with_vault(self):
        d = Path(tempfile.mkdtemp())
        try:
            v = Vault(d)
            v.notes.add("secret plan", name="ideas", when=NOW)
            v.lock("pw")
            v.close()
            raw = (d / "notes/ideas.md").read_bytes()
            self.assertTrue(crypto.is_encrypted(raw))
            self.assertNotIn(b"secret", raw)
            v = Vault(d, passphrase="pw")
            self.assertIn("secret plan", v.notes.read("ideas"))
            v.notes.add("still sealed", name="ideas", when=NOW)
            v.close()
            self.assertNotIn(b"sealed", (d / "notes/ideas.md").read_bytes())
            v = Vault(d, passphrase="pw")
            self.assertEqual(len(recall(v.store, "sealed", now=NOW).hits), 1)
            v.unlock()
            self.assertIn("still sealed", (d / "notes/ideas.md").read_text())
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
