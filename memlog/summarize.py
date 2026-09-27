"""Turn a RecallResult into readable text.

The default summary is extractive and fully local: it picks the sentences
that carry the most of the matched conversations' vocabulary.

``llm_summary`` is the one function in memlog that can send data off the
machine. It runs only when all of these hold: the caller asks for it
explicitly, MEMLOG_LLM_MODEL names a model, ``litellm`` is installed, and
MEMLOG_NO_NETWORK is not set. Library telemetry is switched off before the
call, and only the rendered report for that one question is sent.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Iterable, Optional

from .retrieve import RecallResult
from .text import content_terms, sentences


def keywords(texts: Iterable[str], n: int = 8) -> list[str]:
    counts: Counter[str] = Counter()
    for t in texts:
        counts.update(content_terms(t))
    return [w for w, _ in counts.most_common(n)]


def extractive_summary(texts: Iterable[str], max_sentences: int = 5, focus: Iterable[str] = ()) -> list[str]:
    """Pick the sentences that best represent the corpus (frequency-weighted, focus terms strongly boosted)."""
    texts = list(texts)
    freq: Counter[str] = Counter()
    for t in texts:
        freq.update(content_terms(t))
    if not freq:
        return []
    focus_set = set(focus)
    top_freq = max(freq.values())
    scored: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    order = 0
    for t in texts:
        for s in sentences(t):
            key = s.lower()
            terms = content_terms(s)
            if len(terms) < 3 or key in seen:
                continue
            seen.add(key)
            score = sum(freq[w] for w in terms) / (len(terms) ** 0.5)
            # A sentence that mentions what was asked about beats a merely frequent one.
            score += 2 * top_freq * sum(1 for w in terms if w in focus_set)
            scored.append((score, order, s))
            order += 1
    scored.sort(key=lambda x: x[0], reverse=True)
    chosen = sorted(scored[:max_sentences], key=lambda x: x[1])  # restore reading order
    return [s for _, _, s in chosen]


def render_report(result: RecallResult, *, max_conversations: int = 6, width: int = 110) -> str:
    frame = result.timeframe
    lines: list[str] = []
    lines.append(f"Timeframe: {frame.describe()}")
    lines.append("Focus: " + (", ".join(result.terms) if result.terms else "everything"))

    if not result.hits:
        lines.append("")
        lines.append("Nothing recorded matches that. Try a wider timeframe or a different word.")
        return "\n".join(lines)

    n_conv = len({h.entry.conv for h in result.hits})
    lines.append(f"Matched {len(result.hits)} turn(s) across {n_conv} conversation(s).")

    if result.activity:
        lines.append("")
        lines.append("Activity:")
        peak = max(c for _, c in result.activity) or 1
        for label, count in result.activity:
            bar = "#" * max(1, round(10 * count / peak))
            lines.append(f"  {label:<20} {bar} {count}")

    all_texts = [h.entry.text for h in result.hits]
    themes = keywords(all_texts, n=8)
    if themes:
        lines.append("")
        lines.append("Themes: " + ", ".join(themes))

    lines.append("")
    lines.append("Conversations, most relevant first:")
    for i, c in enumerate(result.conversations[:max_conversations], 1):
        lines.append(
            f"  {i}. [{c.relevance:.2f}] {c.span}  ({c.source}, {c.turns_in_frame} turn(s), {len(c.hits)} matched)"
        )
        if c.keywords:
            lines.append(f"       keywords: {', '.join(c.keywords)}")
        snippet = c.snippet if len(c.snippet) < width - 10 else c.snippet[: width - 13] + "..."
        lines.append(f'       "{snippet}"')

    summary = extractive_summary(all_texts, max_sentences=5, focus=result.terms)
    if summary:
        lines.append("")
        lines.append("Summary:")
        for s in summary:
            lines.append(f"  - {s}")
    return "\n".join(lines)


class NetworkDisabled(Exception):
    """MEMLOG_NO_NETWORK is set; nothing may leave this machine."""


def network_allowed() -> bool:
    return os.environ.get("MEMLOG_NO_NETWORK", "").strip().lower() not in {"1", "true", "yes", "on"}


def llm_summary(result: RecallResult, model: Optional[str] = None, max_tokens: int = 400) -> Optional[str]:
    """Rewrite the report as prose with an LLM. Returns None when no model is configured or available.

    Raises ``NetworkDisabled`` when MEMLOG_NO_NETWORK is set, whatever else is configured.
    """
    if not network_allowed():
        raise NetworkDisabled("MEMLOG_NO_NETWORK is set; refusing to send memories to a model")
    model = model or os.environ.get("MEMLOG_LLM_MODEL")
    if not model:
        return None
    try:
        import litellm  # type: ignore
        from litellm import completion  # type: ignore
    except ImportError:
        return None
    litellm.telemetry = False
    context = render_report(result, max_conversations=10)
    messages = [
        {
            "role": "system",
            "content": (
                "You summarise a person's own conversation history back to them. "
                "Be concrete, mention the timeframe, group by theme, keep it under 150 words, "
                "and never invent details that are not in the notes."
            ),
        },
        {"role": "user", "content": f"Question: {result.question}\n\nNotes:\n{context}"},
    ]
    response = completion(model=model, messages=messages, max_tokens=max_tokens)
    return response.choices[0].message.content
