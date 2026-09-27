import unittest
from datetime import datetime, timedelta, timezone

from memlog.timeframe import parse_timeframe, shift

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


class TimeframeTests(unittest.TestCase):
    def frame(self, text):
        return parse_timeframe(text, now=NOW)

    def test_over_the_month(self):
        f = self.frame("what have I been doing over the month?")
        self.assertEqual(f.start, shift(NOW, -1, "month"))
        self.assertEqual(f.label, "past month")
        self.assertEqual(f.remainder, "what have I been doing")

    def test_past_weeks_plural_means_a_few(self):
        f = self.frame("what did I ask about rust in the past weeks?")
        self.assertEqual(f.start, NOW - timedelta(weeks=3))
        self.assertEqual(f.remainder, "what did I ask about rust")

    def test_last_n_days(self):
        f = self.frame("errors in the last 10 days")
        self.assertEqual(f.start, NOW - timedelta(days=10))
        self.assertEqual(f.label, "past 10 days")
        self.assertEqual(f.remainder, "errors")

    def test_about_three_years_ago_is_a_wide_window(self):
        f = self.frame("about 3 years ago")
        centre = shift(NOW, -3, "year")
        self.assertEqual(f.start, shift(centre, -12, "month"))
        self.assertEqual(f.end, shift(centre, 12, "month"))
        self.assertIn("about 3 years ago", f.label)

    def test_exact_years_ago_is_narrower(self):
        f = self.frame("3 years ago")
        centre = shift(NOW, -3, "year")
        self.assertEqual(f.start, shift(centre, -6, "month"))
        self.assertEqual(f.end, shift(centre, 6, "month"))

    def test_two_weeks_ago(self):
        f = self.frame("that bug two weeks ago")
        centre = NOW - timedelta(weeks=2)
        self.assertEqual(f.start, centre - timedelta(weeks=0.5))
        self.assertEqual(f.remainder, "that bug")

    def test_yesterday_and_today(self):
        y = self.frame("yesterday")
        self.assertEqual(y.start, datetime(2026, 9, 26, tzinfo=timezone.utc))
        self.assertEqual(y.end, datetime(2026, 9, 27, tzinfo=timezone.utc))
        t = self.frame("today")
        self.assertEqual(t.start, datetime(2026, 9, 27, tzinfo=timezone.utc))

    def test_this_week_starts_monday(self):
        f = self.frame("this week")
        self.assertEqual(f.start, datetime(2026, 9, 21, tzinfo=timezone.utc))

    def test_named_month_defaults_to_most_recent(self):
        f = self.frame("in March")
        self.assertEqual(f.start, datetime(2026, 3, 1, tzinfo=timezone.utc))
        self.assertEqual(f.end, datetime(2026, 4, 1, tzinfo=timezone.utc))
        f = self.frame("in November")  # not yet happened this year
        self.assertEqual(f.start, datetime(2025, 11, 1, tzinfo=timezone.utc))

    def test_since_month(self):
        f = self.frame("since june")
        self.assertEqual(f.start, datetime(2026, 6, 1, tzinfo=timezone.utc))
        self.assertGreater(f.end, NOW)

    def test_year(self):
        f = self.frame("what happened in 2024")
        self.assertEqual(f.start, datetime(2024, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(f.end, datetime(2025, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(f.remainder, "what happened")

    def test_recently(self):
        f = self.frame("what have I been up to lately")
        self.assertEqual(f.start, NOW - timedelta(weeks=2))

    def test_no_time_phrase_is_unbounded(self):
        f = self.frame("borrow checker")
        self.assertFalse(f.bounded)
        self.assertEqual(f.remainder, "borrow checker")
        self.assertTrue(f.contains(NOW - timedelta(days=5000)))

    def test_shift_month_clamps_day(self):
        self.assertEqual(shift(datetime(2026, 3, 31, tzinfo=timezone.utc), -1, "month"),
                         datetime(2026, 2, 28, tzinfo=timezone.utc))
        self.assertEqual(shift(datetime(2024, 2, 29, tzinfo=timezone.utc), -1, "year"),
                         datetime(2023, 2, 28, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
