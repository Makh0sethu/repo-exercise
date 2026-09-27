"""memlog: a light conversation memory and retrieval system.

Record conversation turns and activities into a local SQLite store, then ask
questions like "what have I been doing over the past month?" and get back the
relevant conversations, a per-conversation relevance analysis, and a readable
summary. No external services are required; an LLM can optionally polish the
summary.
"""

from .store import Entry, Store
from .vault import Vault
from .timeframe import TimeFrame, parse_timeframe
from .retrieve import Hit, ConversationAnalysis, RecallResult, recall
from .summarize import render_report, extractive_summary, keywords

__all__ = [
    "Entry",
    "Store",
    "Vault",
    "TimeFrame",
    "parse_timeframe",
    "Hit",
    "ConversationAnalysis",
    "RecallResult",
    "recall",
    "render_report",
    "extractive_summary",
    "keywords",
]

__version__ = "0.2.0"
