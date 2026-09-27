# repo-exercise

Practising git commands, and now home to **memlog**: a light conversation
memory and retrieval system.

## memlog

Record what you talk about and what you do, then ask questions like
*"what have I been doing over the past month?"* or *"what did I say about
Rust about 3 years ago?"* and get back the relevant conversations, a
per-conversation relevance analysis, and a readable summary.

Pure standard library. Storage is one SQLite file with a full-text index.
No API keys needed; an LLM can optionally polish the summary.

### Try it in ten seconds

```bash
python3 -m memlog demo                       # runs against a built-in sample history
python3 -m memlog demo "hiking in the past weeks"
```

### Use it for real

```bash
pip install -e .                             # gives you the `memlog` command (optional)

memlog log "Refactored the auth module, still fighting the token refresh bug"
memlog log --role activity --source gym "Deadlift 100kg"
memlog ingest chat_export.jsonl              # .jsonl / .json / .txt / .md
memlog watch ~/notes/today.md                # background capture: tail a file

memlog ask "what have I been doing over the past month?"
memlog ask "what did I ask about airflow in the past weeks?"
memlog ask "what was I thinking about 3 years ago?" --json
memlog recall "borrow checker" --since "last week"
memlog timeframe "about 3 years ago"         # see how a time phrase is interpreted
memlog stats
```

The database lives at `~/.memlog/memlog.db` by default. Override with
`--db PATH` or `MEMLOG_DB=PATH`.

### Remember a chat loop from Python

```python
from memlog import Store
from memlog.recorder import Recorder

store = Store()
rec = Recorder(store, source="gemini")
chat = rec.wrap(generate_response)          # any function messages -> reply
chat([{"role": "user", "content": "Explain lifetimes"}])   # both sides get stored
rec.activity("Read the Rust book chapter on lifetimes")
```

`generate_response` can be the LiteLLM function from `ProgrammaticPrompting1.ipynb`.

### Optional LLM summary

```bash
pip install litellm
export MEMLOG_LLM_MODEL=gemini/gemini-2.5-flash    # plus that provider's API key
memlog ask "what have I been up to this month?" --llm
```

### How it works

1. **Timeframe parsing** (`memlog/timeframe.py`): the vocal time phrase in the
   question becomes a date window. "over the month", "in the past weeks",
   "about 3 years ago", "yesterday", "since June", "in 2024", "lately".
   The rest of the question becomes the topical query.
2. **Store** (`memlog/store.py`): SQLite table plus an FTS5 index with Porter
   stemming, so "borrowing" finds "borrow". Each entry has a timestamp,
   source, role and conversation id.
3. **Retrieval** (`memlog/retrieve.py`): BM25 ranking inside the window,
   blended with recency, then grouped per conversation. Each conversation
   gets a relevance score (how strongly its best turns match, and how much
   of it matched), a keyword profile, and a snippet. An activity profile
   (entries per day, week or month) shows *when* you were busy.
4. **Summary** (`memlog/summarize.py`): extractive, local, and deterministic.
   Sentences that carry the most of the matched vocabulary, boosted when
   they mention what you asked about.

Run the tests with:

```bash
python3 -m unittest discover -s tests
```

See `DESIGN.md` for the roadmap and the reasoning behind the choices.
