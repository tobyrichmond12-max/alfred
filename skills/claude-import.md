---
name: claude-import
description: One-shot importer that pulls the user's Claude data export (conversations, memories, projects) into Alfred's vault and memory store. Extracts structured knowledge per conversation via Claude, writes category-organized notes with wikilinks, and indexes into the SQLite memory store for BM25/vector search.
---

# Claude Account Import

Module: `core/import_claude.py`. One-shot batch job that ingests a Claude data export (`.zip` downloaded from Settings, Privacy, Export Data) into Alfred's long-term memory. Not a sprint feature; runs after the PWA (Sprint 11) stabilizes.

## When to run

Once, after the user downloads a fresh export from claude.ai. Subsequent reruns are safe (category files append dated update blocks), but each run spends Claude credits for extraction, so treat it as an occasional refresh rather than a cron.

## Export format (confirmed 2026-04-22)

The zip contains four top-level JSON files:

- `users.json`: single account record (uuid, full_name, email, phone)
- `memories.json`: one pre-compiled bio under `conversations_memory`. Already structured, the user-specific, ready to land as a single bootstrap note.
- `projects.json`: list of Claude Projects (uuid, name, description, docs, prompt_template)
- `conversations.json`: list of conversations, each with:
  - top-level: `uuid`, `name` (title), `summary` (often empty), `created_at`, `updated_at`, `account`, `chat_messages`
  - each `chat_messages[i]` has: `uuid`, `sender` (`"human"` or `"assistant"`), `created_at`, `text` (flat concatenation), and `content[]` of typed blocks

Content blocks come in four types:

| Type | Keep? | Notes |
|---|---|---|
| `text` | yes | The actual readable reply or prompt |
| `thinking` | no | Assistant internal reasoning; do not expose |
| `tool_use` | no | Tool invocations |
| `tool_result` | no | Tool outputs |

The importer walks `content[]` and concatenates only `type == "text"` blocks, falling back to the flat top-level `text` field if no text blocks are present.

The 2026-04-22 snapshot had 70 conversations, 2175 messages (1090 human, 1085 assistant), 2665 text blocks, 63 thinking, 1063 tool_use, 1036 tool_result. Date range 2026-03-13 to 2026-04-22. Zip size 4.5 MB compressed, 20 MB extracted.

## Pipeline

Five stages, plus a bootstrap step:

1. **`unpack_export(zip_path)`**: extracts the zip into `vault/imports/claude-export/`, then splits `conversations.json` into one file per conversation under `vault/imports/claude-export/conversations/` named `<YYYY-MM-DD>--<title-slug>--<uuid8>.json`. Returns the sorted list of split paths.

2. **`write_account_memory()`**: copies the pre-compiled bio from `memories.json` into `vault/memory/claude-bootstrap.md` with frontmatter. One-shot bootstrap, not per-conversation.

3. **`parse_conversation(json_path)`**: reads one split file, normalizes `sender` (`human` -> `user`, `assistant` -> `alfred`), walks `content[]` keeping only text blocks, returns a `ParsedConversation` with a clean `messages[]` list.

4. **`extract_knowledge(conversation)`**: renders the transcript as plain text, sends it plus `EXTRACTION_SYSTEM_PROMPT` to `claude -p`, parses the returned JSON `{"items": [...]}`, filters out unknown categories, returns a list of `KnowledgeItem`. Transcript is truncated at 60k chars to protect the context window on 488-message conversations.

5. **`write_to_vault(items)`**: writes each item to `vault/memory/<category>/<slug>.md`. New files get a full frontmatter block and a `## Related` section built from `item.links`. Existing files get a dated `## Update, YYYY-MM-DD` block appended, so earlier content is preserved.

6. **`build_index(vault_memory_path)`**: indexes every memory note into `data/memory.db` `memories` table. Upserts by `(memory_type, slug)` using a unique index. Tags JSON-pack into the existing TEXT col, `valid_at` is the frontmatter `first_seen` date, source conversation uuids land in `source_episode_ids`. Embeddings stay NULL so keyword search via `core/memory_search.py` works immediately; a sentence-transformers backfill can populate the `embedding BLOB` later without re-walking the vault.

The orchestrator `run_full_import(zip_path, skip_index=False)` runs stages 1 through 6 in order and prints per-stage progress.

## Knowledge categories

Five top-level buckets, each a directory under `vault/memory/`:

| Category | What goes in |
|---|---|
| `people` | real people (colleagues, professors, friends, contacts) with role and relationship |
| `decisions` | choices the user made with the why, ideally dated |
| `projects` | things the user is building, studying, or running (name, goal, status) |
| `preferences` | stylistic or procedural preferences that should influence Alfred |
| `technical` | reusable tool, library, pattern, or fact relevant to the user's work |

The extraction prompt lives in `EXTRACTION_SYSTEM_PROMPT` and enforces kebab-case slugs, no em dashes, no first-person voice, no extraction of chit-chat or meta-commentary.

## How to run

```bash
cd /mnt/nvme/alfred
# Place the export at /mnt/nvme/alfred/vault/imports/claude-export.zip, then:
python3 -m core.import_claude vault/imports/claude-export.zip

# Skip the SQLite indexer if you only want the vault notes to update:
python3 -m core.import_claude vault/imports/claude-export.zip --skip-index

# Retry just the conversations that did not land any memory items on a
# prior run. Uses a 30s cooldown between extractions. Logs to
# logs/retry_imports.log.
python3 scripts/retry_failed_imports.py
```

Budget: extracting 70 conversations through `claude -p` takes roughly 10 to 20 minutes depending on conversation length and rate limits. The longest conversation in the 2026-04-22 snapshot is 488 messages; that one alone will be the bulk of the cost.

## Idempotence and re-runs

- `unpack_export` re-extracts on top of itself (no state check).
- Split filenames are deterministic (`<date>--<slug>--<uuid8>.json`), so reruns overwrite in place rather than duplicating.
- `write_to_vault` detects existing slugs and appends an `## Update, YYYY-MM-DD` block instead of overwriting. Old content is never lost.
- `build_index` (when implemented) should upsert on `(memory_type, slug)` rather than insert.

Safe to run twice. Safe to run after adding a handful of new conversations via an incremental export.

## Open follow-ups

1. ~~**`build_index` schema and upsert.**~~ Done 2026-04-22 (commit f77b737). Added a `slug` TEXT column plus a unique `(memory_type, slug)` index; upsert takes the shipping path.

2. **Embeddings.** The `embedding BLOB` column is still NULL for every row landed by `build_index`. `sentence-transformers all-MiniLM-L6-v2` is the usual pick (~100 MB download, CPU-fine on the Jetson). `core/memory_search.py` covers the gap with keyword + slug + tag scoring in the meantime.

3. **Prompt tuning.** The first extraction pass on the 2026-04-22 snapshot over- or under-extracted on a handful of conversations (see `logs/retry_imports.log` for the 56 retries). Re-runs are safe; tweak `EXTRACTION_SYSTEM_PROMPT` and re-run when a pattern emerges.

4. **Rate limiting.** `extract_knowledge` now retries once after a 30s sleep on `rc != 0` / timeout / malformed JSON (commit fa16d7a). `scripts/retry_failed_imports.py` also adds a 30s cooldown between conversations, so the import spaces itself out naturally.

5. **Projects integration.** `projects.json` is unpacked but not currently parsed into vault notes. Each project (Alfred, Thoth, and How-to-Use-Claude in the 2026-04-22 snapshot) could become a `projects/*.md` seed note. Cheap to add, not urgent.

## Output layout after a successful run

```
vault/
├── imports/
│   ├── claude-export.zip               (the original, preserved)
│   └── claude-export/
│       ├── users.json
│       ├── memories.json
│       ├── projects.json
│       ├── conversations.json          (original, preserved)
│       └── conversations/
│           ├── 2026-03-13--maximizing-claude-for-business-productivity--e094b888.json
│           ├── 2026-03-14--...
│           └── ...                      (one per conversation)
└── memory/
    ├── claude-bootstrap.md             (from memories.json)
    ├── people/
    ├── decisions/
    ├── projects/
    ├── preferences/
    └── technical/
```

Source conversation files under `imports/` stay on disk forever as the canonical record. Vault memory notes can be regenerated from them.

## Troubleshooting

- **`claude -p` returns non-JSON**: the extraction prompt asks for a bare JSON object but Claude occasionally wraps in code fences. The importer strips triple-backtick fences automatically. Anything else fails the conversation and is logged; the pipeline continues.
- **Empty items for most conversations**: normal. A conversation about a coding bug or a quick question often yields zero durable knowledge. Do not tune the prompt up until you see genuine misses.
- **`parse_conversation` errors**: almost always malformed `created_at` or missing `chat_messages`. Those conversations are skipped with a logged warning; the file stays on disk under `imports/claude-export/conversations/` for manual inspection.
- **Slug collisions across conversations**: the append-on-existing behavior in `write_to_vault` handles them. Unrelated items that happen to share a slug will end up in the same file; re-slug in the next prompt pass if that turns out to be a real problem.
