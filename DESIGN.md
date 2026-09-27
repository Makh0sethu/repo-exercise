# memlog design notes

The idea: light software that remembers your conversations in the background,
lets you ask what you've been up to in natural time frames, analyses each
conversation for relevance, and gives usable text back.

## What v0 does

| Piece | Choice | Why |
|---|---|---|
| Storage | SQLite + FTS5 | One file, zero deps, fast enough for years of text. |
| Ranking | BM25 (built into FTS5) + recency blend | Good keyword relevance without a vector DB. |
| Time phrases | Hand-written parser | Covers the spoken forms ("over the month", "about 3 years ago") and keeps the remainder as the topical query. |
| Per-conversation analysis | depth (best 3 turns) + coverage (matched / total turns) | Separates "one passing mention" from "a whole conversation about it". |
| Summary | Extractive, local | Deterministic and free. LLM prose is an opt-in layer on top. |
| Capture | `Recorder.wrap()`, `ingest`, `watch` | In-process for your own chat loops, file tail for everything else. |

## Where it is weak, and what fixes it

- **Synonyms.** "job applications" won't find "applied for the role".
  BM25 is lexical. Fix: add an embeddings column (any small local model or an
  API) and blend cosine similarity with BM25. The `search()` interface
  already returns `(entry, relevance)` so the blend slots in there.
- **Conversation boundaries.** Without an explicit `conv`, entries are
  grouped one-per-source-per-day. Fix: a gap-based splitter (new
  conversation after N minutes of silence) at ingest time.
- **Summaries read like quotes.** They are. Abstractive summaries need a
  model; `llm_summary()` is the hook, driven by `MEMLOG_LLM_MODEL`.
- **Capture is manual or file-based.** True background capture of other
  apps' conversations means adapters: a ChatGPT/Claude export importer, a
  browser extension, a clipboard watcher, or shell history. Each is a small
  function that yields `{text, when, role, conv, source}` dicts into
  `Store.add_many()`.

## Roadmap

1. **Capture adapters**: JSON exports from chat apps, shell history, calendar.
2. **Semantic retrieval**: embeddings side-by-side with FTS, hybrid ranking.
3. **Better time understanding**: "the week before my trip", "last Tuesday",
   "Q2", relative to entries rather than to now.
4. **Insights**: recurring themes over time, "you keep coming back to X",
   week-over-week activity deltas.
5. **Actions on the PC**: only after the above, and behind an explicit
   allow-list. Retrieval is read-only and safe; acting on the machine is
   not. Start with suggestions ("open the notes from that conversation")
   and require confirmation per action.

## Privacy

Everything stays on disk in one SQLite file you own. Nothing leaves the
machine unless you set `MEMLOG_LLM_MODEL` and pass `--llm`, and then only
the rendered report for that one question is sent.
