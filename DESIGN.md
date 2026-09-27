# memlog design notes

The idea: light software that remembers your conversations in the background,
lets you ask what you've been up to in natural time frames, analyses each
conversation for relevance, and gives usable text back.

## What v0 does

| Piece | Choice | Why |
|---|---|---|
| Layout | Folders: `conversations/YYYY/MM/source/conv.jsonl`, `activities/YYYY/MM/source.jsonl`, `reports/`, `index/` | Human-browsable, greppable, backup-friendly, and a month or a source can be dropped as a unit. |
| Index | SQLite + FTS5, rebuilt from the folders | Zero deps, fast enough for years of text; a cache, never the only copy. |
| Ranking | BM25 (built into FTS5) + recency blend | Good keyword relevance without a vector DB. |
| Time phrases | Hand-written parser | Covers the spoken forms ("over the month", "about 3 years ago") and keeps the remainder as the topical query. |
| Per-conversation analysis | depth (best 3 turns) + coverage (matched / total turns) | Separates "one passing mention" from "a whole conversation about it". |
| Summary | Extractive, local | Deterministic and free. LLM prose is an opt-in layer on top. |
| Notepad | Markdown under `notes/`, sections headed by timestamps, indexed per section | Deliberate memory next to the recorded kind; readable and editable with any tool; searchable through the same `ask`. |
| Capture | `Recorder.wrap()`, `ingest`, `watch` | In-process for your own chat loops, file tail for everything else. |

## Why folders and an index, not one of them

One SQLite file is the simplest thing that works, and it was v0.1. It has two
problems for a personal memory: you cannot look at it without a tool, and
"delete everything from that month" or "back up just my chat history" means
SQL. Folders fix both. But folders alone make questions slow and lose BM25
ranking. So the folders are canonical and the index is a derived cache: any
write goes to both, any delete goes to both, and `reindex()` rebuilds the
index from the folders whenever it is missing or stale.

Each memory carries a random 16-hex `uid` written into its line of JSON and
into the index, so a forget can find the exact line in the exact file, and a
reindex keeps the same identities.

Per-file sealing needs keys that do not cost a scrypt per file. The vault
derives keys once per session from the salt in `vault.json` and checks them
against a stored HMAC verifier, then seals each file with a fresh nonce
(`crypto.seal`, format `MEMLOG2`). The single-file `Store` keeps its
self-contained `MEMLOG1` format for standalone use and for importing old
databases.

## The notepad

Recorded memories are things that happened; notes are things you decided to
keep. They share the vault, the sealing and the index, but differ in two ways:

- **Edited in place.** A note is one Markdown file; writes replace it and
  reindex it whole (one index row per `## ` section, uid = hash of file,
  section number and text). A forget on a note section rewrites the file
  without that section; the notepad reindexes as part of that, so the vault
  counts victims rather than asking the index what it deleted.
- **Never expired.** `purge()` passes `keep_notes=True`. Retention is for
  the background stream, not for what you wrote down on purpose.

`edit` is the one place memlog starts another program. It writes the note to
a fresh 0700 temp directory as a 0600 file, runs `$EDITOR`, reads it back,
shreds the temp file, and re-seals into the vault. The security test allows
`subprocess` in `notepad.py` only.

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
- **Appending to a sealed file rewrites it.** Each add decrypts, appends and
  re-seals the conversation's file. Fine for conversations (hundreds of
  lines); an activity source with tens of thousands of lines a month would
  want per-day files or an append-only sealed log. Wrap bulk imports in
  `Vault.add_many()` so the index is written once.
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

**Encryption at rest.** Per file: every memory file, report and the index
is sealed separately, and the index is deserialised into a `:memory:` SQLite
connection so full-text search keeps working. Construction, all from the
standard library:

- scrypt (n=2^15, r=8, p=1) over the passphrase and the vault's 16-byte salt,
  once per session, giving a 32-byte encryption key and a 32-byte MAC key;
  `vault.json` stores the salt and HMAC(mac_key, constant) as a verifier;
- HMAC-SHA256 in counter mode as the keystream, fresh 16-byte nonce per file
  per write;
- HMAC-SHA256 over header and ciphertext, checked in constant time before any
  decryption (encrypt-then-MAC).

Why not AES-GCM: it isn't in the standard library, and the zero-dependency
rule matters for a tool that holds your private history. If `cryptography` is
acceptable later, swapping `crypto.py` for AES-256-GCM is a contained change.

**What encryption does not cover.** Plaintext exists in process memory while a
command runs, and the passphrase sits in `MEMLOG_PASSPHRASE` if you export it
(prefer `--passphrase-file` with 0600 permissions, or the prompt). The demo
command uses an in-memory store and writes nothing.

**Retention.** The policy lives in `vault.json`, so it travels with the
folders. `purge()` runs on every open and after every `set_retention()`.
`forget()` deletes by date, conversation, source or id: the matching lines
are removed from their folder files (a file left empty is shredded and empty
folders pruned), then the index rows go, followed by an FTS rebuild and a
`VACUUM` under `PRAGMA secure_delete`. `wipe()` overwrites every file with
random bytes and unlinks it. On SSDs and
copy-on-write filesystems overwriting is best-effort, which is the real
reason to keep the file encrypted in the first place.
