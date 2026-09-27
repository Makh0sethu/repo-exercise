# repo-exercise

Practising git commands, and now home to **memlog**: a light conversation
memory and retrieval system.

## memlog

Record what you talk about and what you do, then ask questions like
*"what have I been doing over the past month?"* or *"what did I say about
Rust about 3 years ago?"* and get back the relevant conversations, a
per-conversation relevance analysis, and a readable summary.

Pure standard library (Python 3.11+). Memories live in structured folders you
can browse and grep, with a rebuildable SQLite search index alongside. Local
only: nothing goes online and there is no telemetry. The whole vault can be
sealed with a passphrase, and you decide how long memories are kept.

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
memlog ask "what did I do this week?" --save  # keep the answer under reports/
memlog recall "borrow checker" --since "last week"
memlog timeframe "about 3 years ago"         # see how a time phrase is interpreted
memlog tree                                  # the memory folders
memlog stats
```

### The notepad

```bash
memlog note "Buy milk, ask about the airflow catchup bug"   # appends to today's journal page
memlog note --to ideas "memlog could watch shell history"   # appends to a named note
memlog note list
memlog note show ideas
memlog note edit ideas                       # opens $EDITOR; works on a sealed vault too
memlog note rename ideas plans
memlog note delete plans
```

Notes are Markdown files under `notes/`. A quick note lands in
`notes/journal/YYYY/YYYY-MM-DD.md`; a named note is `notes/<name>.md`, and
names can have folders (`work/standup`). Every appended note becomes a section
headed by its timestamp:

```markdown
## 2026-09-27 14:03
Buy milk, ask about the airflow catchup bug
```

so `memlog ask "what did I note last week?"` works: notes are indexed section
by section, each with the heading's timestamp (or the file's modification time
when a heading is not a timestamp), and show up in answers with the source
`notepad`. Edit the files with anything you like when the vault is plaintext,
then `memlog reindex`; when it is sealed, `memlog note edit` decrypts to a
private temp file, launches your editor, re-seals on save and shreds the temp
file. Notes are sealed with everything else, and **retention never expires a
note**: they stay until you delete them (or `memlog forget --conv note:NAME`
for a section-level delete by date).

### The memory folders

Everything lives under `~/.memlog` (override with `--root DIR` or
`MEMLOG_ROOT=DIR`):

```
~/.memlog/
  vault.json                                  layout version, retention policy, key salt (no secrets)
  conversations/2026/09/chat/chat_2026-09-27.jsonl    one file per conversation
  conversations/2026/09/gemini/lifetimes.jsonl
  activities/2026/09/gym.jsonl                one file per source per month
  notes/ideas.md                              the notepad: named notes, edited in place
  notes/journal/2026/2026-09-27.md            quick notes land on the day's journal page
  reports/2026/2026-09-27_what-did-i-do-this-week.md   answers you chose to keep
  index/memlog.db                             search index, rebuilt from the folders by `memlog reindex`
```

The folders are the source of truth. Each memory is one line of JSON with a
stable `uid`, an ISO timestamp, source, role, conversation id, text and
metadata, so you can read it, grep it, back it up or drop files in by hand.
The index is a cache: delete it and it is rebuilt on the next open.

Entries whose role is `activity`, `event`, `action` or `note` go under
`activities/`; everything else is a conversation turn. Old single-file
databases from v0.1 import with `memlog ingest ~/.memlog/memlog.db`.

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
(`tests/test_security.py`) fails the build if one is ever added. The only
program memlog ever starts is your `$EDITOR`, for `memlog note edit`. There is no
telemetry, no update check, no analytics. `memlog stats` reports whether
anything *could* leave the machine under the current configuration.

**Seal the vault.**

```bash
memlog lock                      # prompts for a passphrase, seals every file under ~/.memlog
export MEMLOG_PASSPHRASE=...     # or --passphrase-file FILE, or type it when prompted
memlog ask "what did I do this week?"
memlog lock                      # again to change the passphrase
memlog unlock --yes              # back to plaintext files, if you ever want that
```

While sealed, every memory file, report and the index is encrypted
separately. Files are decrypted into memory only for the life of the command
and re-sealed on every write. On disk they start with `MEMLOG2`, not JSON, and
contain no plaintext. `vault.json` holds the key salt and a verifier, never the
passphrase or keys. The keys are derived once per command with scrypt;
encryption and integrity use HMAC-SHA256 (see `memlog/crypto.py` for the exact
construction). A wrong passphrase or a tampered file is refused outright. Every
file and folder is owner-only (0600 / 0700), sealed or not.

**Decide how long to keep memories.**

```bash
memlog retention 90              # keep 90 days; older entries are purged now and on every open
memlog retention                 # show the policy
memlog retention off             # keep forever (the default)
memlog forget --before "6 months ago"
memlog forget --older-than 30 --source shell
memlog forget --conv chat:2026-09-12
memlog forget --id 42
memlog wipe --yes                # delete everything and shred every file
```

Deleted entries are really gone: the line leaves its folder file (the file is
rewritten, or shredded if it ends up empty, and empty folders are removed),
the index is rebuilt so old terms leave it, and its free pages are overwritten.
`wipe` overwrites every file with random bytes before unlinking it.

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
2. **Vault** (`memlog/vault.py`): the folder layout above. Writes go to the
   right folder file and to the index; forget and retention rewrite the
   files. **Store** (`memlog/store.py`) is the index: a SQLite table plus an
   FTS5 index with Porter stemming, so "borrowing" finds "borrow". Each
   entry has a timestamp, source, role, conversation id, a stable uid and
   the folder file it lives in.
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
