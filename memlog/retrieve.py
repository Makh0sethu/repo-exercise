"""Answer "what have I been up to?" style questions against the store.

Pipeline: parse the time phrase out of the question, full-text search the
remainder inside that window, then analyse the hits per conversation so
each conversation gets its own relevance score, keyword profile and snippet.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .store import Entry, Store
from .text import content_terms
from .timeframe import TimeFrame, parse_timeframe


@dataclass
class Hit:
    entry: Entry
    relevance: float  # 0..1 from full-text ranking
    recency: float    # 0..1, 1 = newest in the frame
    score: float      # blended


@dataclass
class ConversationAnalysis:
    conv: str
    source: str
    first: datetime
    last: datetime
    turns_in_frame: int
    hits: list[Hit]
    relevance: float
    keywords: list[str]
    snippet: str

    @property
    def day(self) -> str:
        return self.first.strftime("%Y-%m-%d")

    @property
    def span(self) -> str:
        a, b = self.first.strftime("%Y-%m-%d"), self.last.strftime("%Y-%m-%d")
        return a if a == b else f"{a} → {b}"


@dataclass
class RecallResult:
    question: str
    timeframe: TimeFrame
    terms: list[str]
    hits: list[Hit]
    conversations: list[ConversationAnalysis]
    activity: list[tuple[str, int]] = field(default_factory=list)  # (bucket label, count)

    @property
    def topical(self) -> bool:
        return bool(self.terms)


def _recency(ts: datetime, start: Optional[datetime], end: Optional[datetime]) -> float:
    if start is None or end is None or end <= start:
        return 0.5
    frac = (ts - start).total_seconds() / (end - start).total_seconds()
    return max(0.0, min(1.0, frac))


def _keywords(entries: list[Entry], n: int = 6) -> list[str]:
    counts: Counter[str] = Counter()
    for e in sorted(entries, key=lambda e: (e.ts, e.id)):  # ties resolve in reading order
        counts.update(content_terms(e.text))
    return [w for w, _ in counts.most_common(n)]


def _activity_buckets(entries: list[Entry], frame: TimeFrame) -> list[tuple[str, int]]:
    """Count entries per day for short frames, per week for medium, per month for long."""
    if not entries:
        return []
    lo = min(e.ts for e in entries)
    hi = max(e.ts for e in entries)
    days = (hi - lo).days + 1
    if days <= 21:
        key = lambda e: e.ts.strftime("%Y-%m-%d")
    elif days <= 180:
        key = lambda e: "week of " + (e.ts - timedelta(days=e.ts.weekday())).strftime("%Y-%m-%d")
    else:
        key = lambda e: e.ts.strftime("%Y-%m")
    counts: Counter[str] = Counter(key(e) for e in entries)
    return sorted(counts.items())


def recall(
    store: Store,
    question: str,
    *,
    now: Optional[datetime] = None,
    limit: int = 200,
    top_conversations: int = 10,
    recency_weight: float = 0.25,
) -> RecallResult:
    now = now or datetime.now(timezone.utc)
    frame = parse_timeframe(question, now=now)
    terms = content_terms(frame.remainder)

    raw = store.search(frame.remainder, frame.start, frame.end, limit=limit)
    hits: list[Hit] = []
    for entry, rel in raw:
        rec = _recency(entry.ts, frame.start, frame.end)
        blended = rel if not terms else (1 - recency_weight) * rel + recency_weight * rec
        hits.append(Hit(entry, rel, rec, blended))
    hits.sort(key=lambda h: h.score, reverse=True)

    # Everything inside the frame, for per-conversation turn counts and the activity profile.
    in_frame = store.between(frame.start, frame.end, limit=max(limit * 10, 1000))
    by_conv_all: dict[str, list[Entry]] = defaultdict(list)
    for e in in_frame:
        by_conv_all[e.conv].append(e)

    by_conv_hits: dict[str, list[Hit]] = defaultdict(list)
    for h in hits:
        by_conv_hits[h.entry.conv].append(h)

    analyses: list[ConversationAnalysis] = []
    for conv, conv_hits in by_conv_hits.items():
        all_turns = by_conv_all.get(conv) or [h.entry for h in conv_hits]
        # On ties, the user's own turn makes the better snippet.
        conv_hits.sort(key=lambda h: (h.score, h.entry.role == "user"), reverse=True)
        top = conv_hits[:3]
        depth = sum(h.score for h in top) / len(top)
        coverage = len(conv_hits) / max(1, len(all_turns))
        relevance = 0.8 * depth + 0.2 * coverage if terms else 0.5 + 0.5 * min(1.0, len(all_turns) / 10)
        best = top[0].entry
        analyses.append(
            ConversationAnalysis(
                conv=conv,
                source=best.source,
                first=min(e.ts for e in all_turns),
                last=max(e.ts for e in all_turns),
                turns_in_frame=len(all_turns),
                hits=conv_hits,
                relevance=round(relevance, 3),
                keywords=_keywords(all_turns),
                snippet=best.text[:200].replace("\n", " "),
            )
        )
    analyses.sort(key=lambda a: (a.relevance, a.last), reverse=True)

    return RecallResult(
        question=question,
        timeframe=frame,
        terms=terms,
        hits=hits,
        conversations=analyses[:top_conversations],
        activity=_activity_buckets(in_frame, frame),
    )
