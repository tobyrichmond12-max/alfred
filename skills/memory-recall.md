---
name: memory-recall
description: How Alfred pulls durable facts, decisions, people, and preferences from the indexed vault memory store. Use when a voice question references a specific person, project, past decision, or stated preference that isn't in current_state.json. Keyword search with no embedding dependency.
---

# Memory Recall

Alfred keeps a local, searchable record of durable facts about the user's life, built by `core/import_claude.py` from the Claude export plus any notes Alfred writes to `vault/memory/`. The search path is `core/memory_search.py`, a keyword + slug + tag scorer over the SQLite rows `build_index` upserts.

## When to reach for it

**Yes, call it when:**
- the user names a person, project, or acronym that is not on the calendar or in tasks. "How's the Thoth research pile?", "what does <contact-name-a> work on?", "drafting a reply to <contact-name-b>".
- the user asks about a past decision or preference. "Why did we kill openclaw?", "what's my em-dash rule?", "did I ever decide on Claude Max for Thoth?".
- You need context for a file edit or an outbound message that references durable facts.

**No, skip it when:**
- The answer is in `current_state.json` (today's calendar, tasks, location, biometrics).
- The question is purely conversational, greeting, or time-sensitive ("what's my day look like?", "what time is it?").
- You already have the fact in-context from the current session.

## How to call it

From Alfred's Bash tool:

```
python3 -m core.memory_search "<advisor> co-op advisor" --limit 3
python3 -m core.memory_search --type people <contact-b> --limit 2
python3 -m core.memory_search --type decisions "thoth research cap"
python3 -m core.memory_search --json "em dashes" | head -20
```

Flags:
- `--type <category>`: restrict to `people`, `decisions`, `projects`, `preferences`, or `technical`.
- `--limit N`: top-k (default 8).
- `--json`: emit raw JSON for parsing instead of the voice-formatted lines.

Default output is one line per hit: `[<category>/<slug>] <snippet truncated at 240 chars>`.

## Budget

One memory_search call per voice answer is fine. Chaining two or three if genuinely needed is tolerated. Burning ten calls to hedge a query is not. If the first call has zero hits, answer from what you already know and say so rather than firing three more.

## What is indexed

Every markdown file under `vault/memory/{people,decisions,projects,preferences,technical}/` that `core/import_claude.build_index` has seen. Content is the note body minus its H1 heading and frontmatter. Tags are the frontmatter `tags:` list, JSON-packed into the `tags` column.

Row count lives in `data/memory.db`:

```
sqlite3 /mnt/nvme/alfred/data/memory.db "SELECT memory_type, COUNT(*) FROM memories WHERE slug IS NOT NULL GROUP BY memory_type"
```

## What is NOT indexed (yet)

- Conversation files under `vault/conversations/`. Those stay raw.
- Reflection files under `vault/reflections/`. Also raw.
- Journal files. Raw.
- Imported conversation JSONs under `vault/imports/`. Raw; referenced only as source links from within memory notes.

If a past conversation is the best source for a question, grep via Bash rather than memory_search.

## Related

- `core/import_claude.py`: pipeline that writes the indexed notes.
- `core/memory.py`: embedding-based vector search (requires Ollama-backed local embedder; new vault rows are NULL-embedded so this path skips them).
- `skills/claude-import.md`: the import pipeline skill.
