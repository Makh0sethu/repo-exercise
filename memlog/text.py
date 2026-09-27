"""Small text utilities shared by retrieval and summarisation."""

import re

STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because
    been before being below between both but by can could did do does doing
    down during each few for from further had has have having he her here hers
    him his how i if in into is it its itself just let me more most my myself
    no nor not now of off on once only or other our ours out over own same she
    should so some such than that the their theirs them then there these they
    this those through to too under until up very was we were what when where
    which while who whom why will with would you your yours yourself
    ve ll re d s m t don didn doesn isn wasn aren
    been doing done did get got getting go going went gone
    like thing things stuff something anything everything lot lots
    tell told say said ask asked asking know think thinking thought want wanted
    help helping need needs really also maybe still already
    up upto work working worked
    time day days week weeks month months year years ago recently lately
    last past previous today yesterday tomorrow
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]*", re.IGNORECASE)
_SENT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens; hyphens stay inside words, apostrophes split (so "can't" -> can, t)."""
    return [m.group(0).lower().strip("-") for m in _WORD_RE.finditer(text)]


def content_terms(text: str) -> list[str]:
    """Tokens with stopwords and very short tokens removed, order preserved, de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokenize(text):
        if len(tok) < 2 or tok in STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def sentences(text: str) -> list[str]:
    """Split text into sentences on terminal punctuation or line breaks."""
    return [s.strip() for s in _SENT_RE.split(text) if s and s.strip()]
