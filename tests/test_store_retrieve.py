import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memlog import Store, recall, render_report
from memlog.recorder import Recorder, ingest, watch

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def seeded():
    store = Store(":memory:")
    store.add("How do I satisfy the borrow checker with a self-referential struct?", when=NOW - timedelta(days=2), source="chat", conv="c1")
    store.add("Use Pin or indices instead of references.", when=NOW - timedelta(days=2), source="chat", role="assistant", conv="c1")
    store.add("Planned a hiking trip in the Drakensberg.", when=NOW - timedelta(days=6), source="notes", role="activity", conv="c2")
    store.add("Why does my airflow DAG run twice with catchup on?", when=NOW - timedelta(days=12), source="chat", conv="c3")
    store.add("What's the fastest way to learn Rust from Python?", when=NOW - timedelta(days=400), source="chat", conv="c4")
    store.add("Thinking of switching careers into software.", when=NOW - timedelta(days=1100), source="chat", conv="c5")
    return store


class StoreTests(unittest.TestCase):
    def test_add_and_count(self):
        s = seeded()
        self.assertEqual(s.count(), 6)
        self.assertEqual(s.stats()["conversations"], 5)

    def test_empty_entry_rejected(self):
        with self.assertRaises(ValueError):
            Store(":memory:").add("   ")

    def test_default_conversation_is_source_per_day(self):
        s = Store(":memory:")
        i = s.add("hello", when=NOW, source="cli")
        self.assertEqual(s.get(i).conv, "cli:2026-09-27")

    def test_search_ranks_and_stems(self):
        s = seeded()
        hits = s.search("borrowing checker")  # 'borrowing' stems to 'borrow'
        self.assertTrue(hits)
        self.assertIn("borrow checker", hits[0][0].text)
        self.assertEqual(hits[0][1], 1.0)

    def test_search_respects_time_window(self):
        s = seeded()
        self.assertEqual(len(s.search("rust", start=NOW - timedelta(days=30))), 0)
        self.assertEqual(len(s.search("rust")), 1)

    def test_stopword_only_query_lists_by_time(self):
        s = seeded()
        rows = s.search("what have I been doing", start=NOW - timedelta(days=7), end=NOW)
        self.assertEqual([r[0].conv for r in rows], ["c1", "c1", "c2"])

    def test_persists_to_disk(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.db"
            Store(path).add("persist me", when=NOW)
            self.assertEqual(Store(path).count(), 1)


class RecallTests(unittest.TestCase):
    def test_topical_question_in_window(self):
        r = recall(seeded(), "what did I ask about rust in the past weeks?", now=NOW)
        self.assertEqual(r.terms, ["rust"])
        self.assertEqual(r.hits, [])  # the rust question was 400 days ago

    def test_topical_question_open_window(self):
        r = recall(seeded(), "rust", now=NOW)
        self.assertEqual(len(r.hits), 1)
        self.assertEqual(r.conversations[0].conv, "c4")

    def test_browse_last_week_groups_by_conversation(self):
        r = recall(seeded(), "what have I been doing over the past week?", now=NOW)
        self.assertFalse(r.topical)
        self.assertEqual({c.conv for c in r.conversations}, {"c1", "c2"})
        c1 = next(c for c in r.conversations if c.conv == "c1")
        self.assertEqual(c1.turns_in_frame, 2)
        self.assertIn("borrow", c1.keywords)
        self.assertTrue(r.activity)

    def test_three_years_ago(self):
        r = recall(seeded(), "what was I thinking about 3 years ago?", now=NOW)
        self.assertEqual([h.entry.conv for h in r.hits], ["c5"])

    def test_relevance_prefers_denser_conversation(self):
        s = seeded()
        s.add("More borrow checker pain with lifetimes and the borrow checker again.", when=NOW - timedelta(days=1), source="chat", conv="c6")
        r = recall(s, "borrow checker", now=NOW)
        self.assertEqual(r.conversations[0].conv, "c6")
        self.assertGreaterEqual(r.conversations[0].relevance, r.conversations[1].relevance)

    def test_report_renders(self):
        text = render_report(recall(seeded(), "past 2 weeks", now=NOW))
        self.assertIn("Timeframe: past 2 weeks", text)
        self.assertIn("Conversations, most relevant first:", text)
        self.assertIn("Summary:", text)
        empty = render_report(recall(seeded(), "kubernetes yesterday", now=NOW))
        self.assertIn("Nothing recorded", empty)


class RecorderTests(unittest.TestCase):
    def test_wrap_records_both_sides(self):
        s = Store(":memory:")
        rec = Recorder(s, source="test")
        chat = rec.wrap(lambda messages: "reply to: " + messages[-1]["content"])
        self.assertEqual(chat([{"role": "user", "content": "hi"}]), "reply to: hi")
        turns = s.conversation(rec.conv)
        self.assertEqual([t.role for t in turns], ["user", "assistant"])

    def test_ingest_jsonl_and_text(self):
        s = Store(":memory:")
        with tempfile.TemporaryDirectory() as d:
            j = Path(d) / "chat.jsonl"
            j.write_text(
                json.dumps({"role": "user", "content": "hello there", "ts": "2026-09-01T10:00:00Z", "conversation_id": "x"}) + "\n"
                + json.dumps({"role": "assistant", "text": "general kenobi", "ts": "2026-09-01T10:00:05Z", "conv": "x"}) + "\n"
            )
            self.assertEqual(ingest(s, j), 2)
            t = Path(d) / "notes.md"
            t.write_text("# Monday\n\nWrote the parser.\n\nFixed the tests.")
            self.assertEqual(ingest(s, t, source="notes"), 3)
        entries = list(s)
        self.assertEqual(entries[0].conv, "x")
        self.assertEqual(entries[0].ts.isoformat(), "2026-09-01T10:00:00+00:00")
        self.assertEqual({e.source for e in entries[2:]}, {"notes"})

    def test_watch_from_start(self):
        s = Store(":memory:")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "log.txt"
            f.write_text("did a thing\n" + json.dumps({"text": "json thing", "role": "user"}) + "\n\n")
            calls = {"n": 0}

            def stop():
                calls["n"] += 1
                return calls["n"] > 3

            n = watch(s, f, from_start=True, poll_seconds=0.01, stop=stop)
        self.assertEqual(n, 2)
        self.assertEqual({e.text for e in s}, {"did a thing", "json thing"})


if __name__ == "__main__":
    unittest.main()
