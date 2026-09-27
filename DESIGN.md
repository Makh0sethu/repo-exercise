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

## Privacy and security

**Threat model.** Someone who gets hold of the disk, a backup, or a shared
account should not be able to read the memories. Someone who runs memlog
should be able to see exactly what could ever leave the machine. Deleted
memories should not be recoverable from the file.

**No network.** The package never imports networking modules; a test parses
every source file and also checks the loaded module list after import. The
single opt-in online feature (`--llm`) needs three explicit conditions and can
be hard-blocked with `MEMLOG_NO_NETWORK=1`. When it runs, only the rendered
report for that question is sent, and `litellm.telemetry` is set to False.

**Encryption at rest.** Whole-file, so the FTS index stays usable in memory.
`Store` deserialises the encrypted file into a `:memory:` SQLite connection
and re-encrypts on every commit (batched with `deferred()`). Construction,
all from the standard library:

- scrypt (n=2^15, r=8, p=1) with a fresh 16-byte salt per write, giving a
  32-byte encryption key and a 32-byte MAC key;
- HMAC-SHA256 in counter mode as the keystream, fresh 16-byte nonce per write;
- HMAC-SHA256 over header and ciphertext, checked in constant time before any
  decryption (encrypt-then-MAC).

Why not AES-GCM: it isn't in the standard library, and the zero-dependency
rule matters for a tool that holds your private history. If `cryptography` is
acceptable later, swapping `crypto.py` for AES-256-GCM is a contained change.

**What encryption does not cover.** Plaintext exists in process memory while a
command runs, and the passphrase sits in `MEMLOG_PASSPHRASE` if you export it
(prefer `--passphrase-file` with 0600 permissions, or the prompt). The demo
command uses an in-memory store and writes nothing.

**Retention.** The policy lives inside the database (`settings` table), so it
travels with the file. `purge()` runs on every open and after every
`set_retention()`. `forget()` deletes by date, conversation, source or id.
Deletion is followed by an FTS rebuild (so old terms leave the index) and a
`VACUUM` under `PRAGMA secure_delete`, so the file no longer holds the text.
`wipe()` overwrites the file with random bytes and unlinks it. On SSDs and
copy-on-write filesystems overwriting is best-effort, which is the real
reason to keep the file encrypted in the first place.
