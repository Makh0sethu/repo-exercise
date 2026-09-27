import unittest

from memlog.summarize import extractive_summary, keywords
from memlog.text import content_terms, sentences, tokenize


class TextTests(unittest.TestCase):
    def test_tokenize_splits_contractions(self):
        self.assertEqual(tokenize("I'm can't self-referential"), ["i", "m", "can", "t", "self-referential"])

    def test_content_terms_drops_filler(self):
        self.assertEqual(content_terms("what have I been doing with the borrow checker lately?"), ["borrow", "checker"])

    def test_sentences(self):
        self.assertEqual(sentences("One. Two!\nThree"), ["One.", "Two!", "Three"])


class SummaryTests(unittest.TestCase):
    docs = [
        "The airflow DAG runs twice because catchup is on. Set catchup to false.",
        "Airflow catchup schedules every missed interval. That is why the DAG runs twice.",
        "Unrelated: the weather was nice.",
    ]

    def test_keywords(self):
        top = keywords(self.docs, n=5)
        self.assertIn("airflow", top)
        self.assertIn("catchup", top)
        self.assertNotIn("weather", top)

    def test_extractive_summary_keeps_reading_order_and_dedups(self):
        out = extractive_summary(self.docs + self.docs, max_sentences=2)
        self.assertEqual(len(out), 2)
        self.assertNotIn("weather", " ".join(out))

    def test_focus_terms_boost(self):
        out = extractive_summary(self.docs, max_sentences=1, focus=["weather"])
        self.assertIn("weather", out[0])

    def test_empty(self):
        self.assertEqual(extractive_summary([]), [])


if __name__ == "__main__":
    unittest.main()
