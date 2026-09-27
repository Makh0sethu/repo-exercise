# repo-exercise

Practising git commands, and now home to **memlog**: a light conversation
memory and retrieval system.

## memlog

Record what you talk about and what you do, then ask questions like
*"what have I been doing over the past month?"* or *"what did I say about
Rust about 3 years ago?"* and get back the relevant conversations, a
per-conversation relevance analysis, and a readable summary.

Pure standard library (Python 3.11+). Storage is one SQLite file with a full-text index.
Local only: nothing goes online and there is no telemetry. The file can be
encrypted with a passphrase, and you decide how long memories are kept.

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

### Security and privacy

**Nothing goes online.** The package imports no networking modules, and a test
(`tests/test_security.py`) fails the build if one is ever added. There is no
telemetry, no update check, no analytics. `memlog stats` reports whether
anything *could* leave the machine under the current configuration.

**Encrypt the memory file.**

```bash
memlog lock                      # prompts for a passphrase, encrypts ~/.memlog/memlog.db
export MEMLOG_PASSPHRASE=...     # or --passphrase-file FILE, or type it when prompted
memlog ask "what did I do this week?"
memlog lock                      # again to change the passphrase
memlog unlock --yes              # back to plain SQLite, if you ever want that
```

While locked, the database is decrypted into memory only for the life of the
command and re-encrypted on every write. The file on disk starts with
`MEMLOG1`, not `SQLite format 3`, and contains no plaintext. The key is derived
with scrypt; encryption and integrity use HMAC-SHA256 (see `memlog/crypto.py`
for the exact construction). A wrong passphrase or a tampered file is refused
outright. Files and the `~/.memlog` directory are owner-only (0600 / 0700),
encrypted or not.

**Decide how long to keep memories.**

```bash
memlog retention 90              # keep 90 days; older entries are purged now and on every open
memlog retention                 # show the policy
memlog retention off             # keep forever (the default)
memlog forget --before "6 months ago"
memlog forget --older-than 30 --source shell
memlog forget --conv chat:2026-09-12
memlog forget --id 42
memlog wipe --yes                # delete everything and shred the file
```

Deleted entries are really gone: SQLite's `secure_delete` overwrites the freed
pages, the search index is rebuilt so old terms leave it, and the file is
compacted. `wipe` overwrites the file with random bytes before unlinking it.

**The one online feature is off unless you switch it on.** An LLM prose summary
needs `--llm` on the command line *and* `MEMLOG_LLM_MODEL` in the environment
*and* `litellm` installed. Only the rendered report for that one question is
sent, and the library's own telemetry is disabled first. To make it impossible
regardless of configuration:

```bash
export MEMLOG_NO_NETWORK=1
```

```bash
# if you do want it:
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
