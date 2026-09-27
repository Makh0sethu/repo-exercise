"""Turn vocal time phrases into concrete date ranges.

Handles the kinds of phrases people actually say when asking about their own
history: "over the month", "in the past weeks", "about 3 years ago",
"yesterday", "since March", "in 2024", "recently". Anything unrecognised
yields an open-ended frame so the caller can still search everything.

All datetimes are timezone-aware. ``now`` defaults to the current UTC time
but callers can pass a local-time ``now`` and everything stays consistent.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "couple": 2, "couple of": 2, "few": 3, "several": 4,
}
_NUM = r"(?P<num>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|couple(?: of)?|few|several)"
_UNIT = r"(?P<unit>day|week|month|year)s?"
_APPROX = r"(?P<approx>about|around|roughly|approximately|some|~)?\s*"
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

# Order matters: more specific patterns first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ago", re.compile(
        rf"\b{_APPROX}{_NUM}\s+{_UNIT}\s+(?:ago|back)\b", re.I)),
    ("last_n", re.compile(
        rf"\b(?:(?:in|over|during|for|within)\s+)?(?:the\s+)?(?:last|past|previous|recent)\s+"
        rf"(?:(?P<num>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|couple(?: of)?|few|several)\s+)?"
        rf"{_UNIT}\b", re.I)),
    ("over_the", re.compile(
        rf"\b(?:over|during|throughout)\s+(?:the|this)\s+{_UNIT}\b", re.I)),
    ("this", re.compile(r"\b(?:this|the current)\s+(?P<unit>week|month|year)\b", re.I)),
    ("named_day", re.compile(r"\b(?P<word>today|yesterday)\b", re.I)),
    ("since_month", re.compile(
        rf"\bsince\s+(?P<month>{_MONTH_RE})(?:\s+(?P<year>\d{{4}}))?\b", re.I)),
    ("since_last", re.compile(r"\bsince\s+(?:last\s+)?(?P<unit>week|month|year|yesterday)\b", re.I)),
    ("month", re.compile(
        rf"\b(?:in|during|back in)?\s*(?P<month>{_MONTH_RE})(?:\s+(?P<year>\d{{4}}))?\b", re.I)),
    ("year", re.compile(r"\b(?:in|during|back in)\s+(?P<year>(?:19|20)\d{2})\b", re.I)),
    ("recent", re.compile(r"\b(?:recently|lately|these days)\b", re.I)),
]


@dataclass(frozen=True)
class TimeFrame:
    """A half-open interval [start, end). ``None`` on either side means unbounded."""

    start: Optional[datetime]
    end: Optional[datetime]
    label: str
    remainder: str  # the query text with the time phrase removed

    @property
    def bounded(self) -> bool:
        return self.start is not None and self.end is not None

    def contains(self, ts: datetime) -> bool:
        if self.start is not None and ts < self.start:
            return False
        if self.end is not None and ts >= self.end:
            return False
        return True

    def describe(self) -> str:
        fmt = "%Y-%m-%d"
        if self.bounded:
            assert self.start and self.end
            return f"{self.label} ({self.start.strftime(fmt)} → {(self.end - timedelta(seconds=1)).strftime(fmt)})"
        if self.start is not None:
            return f"{self.label} (from {self.start.strftime(fmt)})"
        if self.end is not None:
            return f"{self.label} (until {self.end.strftime(fmt)})"
        return self.label


def _num(raw: Optional[str]) -> int:
    if raw is None:
        return 1
    raw = raw.strip().lower()
    return int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw, 1)


def shift(dt: datetime, n: int, unit: str) -> datetime:
    """Move ``dt`` by ``n`` units (negative goes back). Months and years keep calendar semantics."""
    if unit == "day":
        return dt + timedelta(days=n)
    if unit == "week":
        return dt + timedelta(weeks=n)
    if unit == "month":
        total = dt.year * 12 + (dt.month - 1) + n
        year, month = divmod(total, 12)
        day = min(dt.day, calendar.monthrange(year, month + 1)[1])
        return dt.replace(year=year, month=month + 1, day=day)
    if unit == "year":
        return shift(dt, 12 * n, "month")
    raise ValueError(f"unknown unit {unit!r}")


def _start_of(dt: datetime, unit: str) -> datetime:
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "day":
        return day
    if unit == "week":
        return day - timedelta(days=day.weekday())
    if unit == "month":
        return day.replace(day=1)
    if unit == "year":
        return day.replace(month=1, day=1)
    raise ValueError(unit)


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" + ("" if n == 1 else "s")


def _clean(text: str, span: tuple[int, int]) -> str:
    out = text[: span[0]] + " " + text[span[1]:]
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\s+([?.,!])", r"\1", out)
    return out.strip(" ,?.!")


def parse_timeframe(text: str, now: Optional[datetime] = None) -> TimeFrame:
    """Extract a time window from ``text``.

    Returns the window plus the remaining query text with the time phrase cut
    out, so the rest can be used as a topical search.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    end_now = now + timedelta(seconds=1)

    for kind, pat in _PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        g = m.groupdict()
        unit = (g.get("unit") or "").lower()

        if kind == "ago":
            n = _num(g["num"])
            centre = shift(now, -n, unit)
            # Window is one unit wide around the point; widen if the phrase was approximate.
            half = 1 if g.get("approx") else 0.5
            if unit == "day":
                start, end = centre - timedelta(days=half), centre + timedelta(days=half)
            elif unit == "week":
                start, end = centre - timedelta(weeks=half), centre + timedelta(weeks=half)
            elif unit == "month":
                start, end = centre - timedelta(days=15 * (2 * half)), centre + timedelta(days=15 * (2 * half))
            else:  # year
                start, end = shift(centre, -int(6 * (2 * half)), "month"), shift(centre, int(6 * (2 * half)), "month")
            label = ("about " if g.get("approx") else "") + f"{_plural(n, unit)} ago"
            return TimeFrame(start, min(end, end_now), label, _clean(text, m.span()))

        if kind == "last_n":
            raw = g.get("num")
            # "past weeks" (plural, no number) reads as "the past few weeks".
            n = _num(raw) if raw else (3 if m.group(0).lower().rstrip().endswith("s") else 1)
            start = shift(now, -n, unit)
            return TimeFrame(start, end_now, f"past {_plural(n, unit)}", _clean(text, m.span()))

        if kind == "over_the":
            start = shift(now, -1, unit)
            return TimeFrame(start, end_now, f"past {unit}", _clean(text, m.span()))

        if kind == "this":
            start = _start_of(now, unit)
            return TimeFrame(start, end_now, f"this {unit}", _clean(text, m.span()))

        if kind == "named_day":
            word = g["word"].lower()
            day = _start_of(now, "day")
            if word == "today":
                return TimeFrame(day, end_now, "today", _clean(text, m.span()))
            return TimeFrame(day - timedelta(days=1), day, "yesterday", _clean(text, m.span()))

        if kind == "since_month":
            month = _MONTHS[g["month"].lower()]
            year = int(g["year"]) if g.get("year") else now.year
            start = now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
            if start > now:
                start = shift(start, -1, "year")
            return TimeFrame(start, end_now, f"since {calendar.month_name[month]} {start.year}", _clean(text, m.span()))

        if kind == "since_last":
            if unit == "yesterday":
                start = _start_of(now, "day") - timedelta(days=1)
            else:
                start = _start_of(shift(now, -1, unit), unit)
            return TimeFrame(start, end_now, f"since last {unit}" if unit != "yesterday" else "since yesterday", _clean(text, m.span()))

        if kind == "month":
            month = _MONTHS[g["month"].lower()]
            year = int(g["year"]) if g.get("year") else now.year
            start = now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
            if start > now and not g.get("year"):
                start = shift(start, -1, "year")
            end = shift(start, 1, "month")
            return TimeFrame(start, min(end, end_now), f"{calendar.month_name[month]} {start.year}", _clean(text, m.span()))

        if kind == "year":
            year = int(g["year"])
            start = now.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end = shift(start, 1, "year")
            return TimeFrame(start, min(end, end_now), str(year), _clean(text, m.span()))

        if kind == "recent":
            start = now - timedelta(weeks=2)
            return TimeFrame(start, end_now, "recently (past 2 weeks)", _clean(text, m.span()))

    return TimeFrame(None, None, "all time", text.strip())
