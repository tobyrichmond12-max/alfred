# Sprint 11 Handoff, Alfred PWA

**Date:** 2026-04-21 to 2026-04-22
**Status:** Shipped, validation pending (the user to enable push on installed PWA)
**Commits:** b35eb49, 7407ac1, f959923, 9f3567b, e8545d2, plus the 2026-04-22
overnight hardening pass (dbafaa5, cf9c675, ec2e4ca, 0be70a4, 75b41d6, f77b737,
bc32cfc, a88e33f)

## What Was Built

Alfred gained a Progressive Web App, served from the Jetson at
`https://<jetson-tailscale-hostname>/`, that complements the Action Button voice
flow with a rich text/voice chat surface. The old Telegram plan was dropped;
the PWA covers the same ground with less operational surface area.

### React PWA (web/ source, bridge/static/ built output)

- Vite + React + TypeScript scaffold under `web/`. `npm run build` emits to
  `bridge/static/` which FastAPI mounts at `/`.
- Chat UI with text input. Messages POST to `/api/message`, conversation
  history loads from `/api/history`.
- Voice memo recording via browser `MediaRecorder`, uploads to
  `/api/voice-memo`. Faster-whisper on the Jetson transcribes base-model
  int8 CPU, routes through the existing Alfred pipeline, returns transcript,
  reply, and timings.
- Service worker handles push events and `notificationclick` (focus-or-open).
- Installable via iOS 16.4+ Add to Home Screen. Icons and manifest land
  under `bridge/static/` too.

### Server-side additions to `bridge/server.py`

- `POST /api/message` , text chat. Form-encoded and JSON-body both parse.
- `GET /api/history` , paginated recent conversation turns for the PWA.
- `POST /api/voice-memo` , WebM/Opus upload, faster-whisper transcribe,
  standard Alfred reply pipeline.
- `GET /api/push/public-key` , VAPID public key for subscription signup.
- `POST /api/push/subscribe` , persists endpoint/keys to
  `data/push_subscriptions.json` (gitignored).
- Form-encoded body parser on `POST /ask` so PWA and Shortcut share one
  path.
- `GET /` serves the built PWA. `/manifest.webmanifest`, `/sw.js`, and the
  icons are explicit routes so they get cached correctly.

### Web Push via VAPID

- `core/notify.py` gained `push_web(message, title=None)` that iterates every
  stored subscription, sends a VAPID-signed notification, and prunes any
  endpoint that returns 404 or 410.
- `core/reflect.py` (urgent and routine paths) and `core/weekly_review.py`
  call `push_web` alongside the existing `push` / `push_routine` (ntfy).
  ntfy stays wired as a fallback until the user validates the PWA push path
  on his phone.

### Claude memory import pipeline (`core/import_claude.py`)

Stand-alone script, not a bridge endpoint. Runs once per Claude data
export zip:

1. `unpack_export(zip_path)` extracts four top-level JSON files and splits
   `conversations.json` into one per-conversation file under
   `vault/imports/claude-export/conversations/`.
2. `write_account_memory()` lands the compiled bio from `memories.json`
   at `vault/memory/claude-bootstrap.md`. Used by the extractor prompt as
   "facts Alfred should already know".
3. `parse_conversation(path)` normalizes sender roles (`human` -> `user`,
   `assistant` -> `alfred`), strips tool/thinking blocks, drops empty turns.
4. `extract_knowledge(conv)` chunks the transcript, runs `claude -p` with
   a category prompt (people, decisions, projects, preferences, technical),
   dedupes by slug, and augments cross-references.
5. `write_to_vault(items)` writes one markdown note per slug under
   `vault/memory/<category>/`. New files get frontmatter, existing slugs
   get a dated Update block appended.
6. `build_index()` walks `vault/memory/` and upserts rows into
   `data/memory.db` keyed on `(memory_type, slug)`. Added a `slug`
   column and unique index for O(1) upsert.

Retry behaviour: the extractor retries once after a 30s sleep on
`rc != 0` / timeout / malformed JSON (commit fa16d7a). The reflection
loop has the same retry (commit 66d2125) so a transient rate-limit
blip does not eat a three-hour observation window.

A companion batch script, `scripts/retry_failed_imports.py`, diffs the
unpacked conversation UUIDs against the uuids already referenced in
`vault/memory/` notes and re-runs extraction on the misses with 30s
cooldowns. Used tonight to retry 56 conversations that had not landed
any memory items on the initial import.

### Other housekeeping

- Killed the old `openclaw` zombie process that was still squatting on
  the Tailscale Serve root; `/` now points unambiguously at the bridge.
- `alfred-bridge.service` systemd unit continues to run on port 8765.
- The existing `alfred-whats-next.md` roadmap was rewritten to reflect
  the PWA-first plan (commit 999de5e) and later marked Sprint 11 as
  shipped (commit 3075d73).

## Follow-ups landed overnight 2026-04-22

- **Journal summary backlinks** (`core/journal.py`): session summary notes
  pair with their raw conversation line instead of listing as a duplicate
  entry. Covers both fresh regen and the cron-mode incremental update.
- **Sprint-field staleness** (`core/sync_state.py`): `tasks.active_sprint`
  now derives from the most recent `SHIPPED/COMPLETE/DONE` heading in
  `alfred-whats-next.md`, so the state no longer advertises "Sprint 5
  complete" after we are five sprints past that.
- **State staleness detection** (`core/state.py` + bridge wiring): the
  bridge injects a plain-English warning between the state block and the
  user message whenever `current_state.json.as_of` is older than 30
  minutes. `/health` surfaces the same signal for external monitors.
- **Screen session caching** (`core/screen.py`): MCP session id is now
  module-level and reused across calls, with one retry on session errors.
  Drops a typical `get_screen_state` from four HTTPS round trips to one
  per tool call.
- **Reflection retention** (`core/retention.py` + 04:00 cron): reflection
  files older than 30 days and weekly reviews older than 26 weeks move
  to `vault/reflections/archive/YYYY-MM/`. `skill-candidates.md` is
  protected.
- **build_index upsert**: the claude-import build step is no longer a
  stub. 152 existing vault notes bootstrapped into memory.db.
- **Keyword memory search** (`core/memory_search.py`): substring + slug +
  tag scoring over data/memory.db, no embeddings needed. Wired into
  CLAUDE.md so Alfred reaches for it on person/project/decision asks.
- **Empty-message hardening** (`bridge/server.py`): /ask GET variants
  now route through `_normalize_user_text` so the literal 'message='
  placeholder the iOS Shortcut sends on empty dictation falls back to
  the default greeting instead of leaking into vault logs.
- **Day summary** (`core/day_summary.py` + 22:30 cron): synthesizes 2 to
  3 sentences from today's reflection bullets and conversations, splices
  a '## Day summary' block above the raw bullets inside '## Alfred's
  Notes'. Idempotent, preserves everything else.
- **Journal regen preservation** (`core/journal.py`): force regeneration
  now captures and re-injects an existing Alfred's Notes body instead of
  clobbering it with the placeholder.
- **Context auto-refresh** (`core/sync_state.py`): context.summary and
  context.current_activity derive from the calendar each sync (in /
  heading-to / free), energy stays user-settable.
- **Smoke test** (`scripts/smoke_test.sh`): one-shot Alfred health
  check covering bridge, systemd, cron, state parse, memory.db counts,
  push subs, laptop MCP.
- **Retry script** (`scripts/retry_failed_imports.py`): diffs
  unprocessed conversation uuids against vault/memory/ sources and
  re-extracts with 30s cooldowns. Logs to `logs/retry_imports.log`.
- **Guarded ntfy teardown** (`scripts/teardown_ntfy.sh`): refuses to
  run while `data/push_subscriptions.json` is empty to avoid leaving
  Alfred push-less.

## Still Open

Deferred, not blocking ship:

- **ntfy teardown.** Container and `tailscale serve --https=8443` rule
  stay up until the user enables notifications on the installed PWA and
  confirms the Web Push path works end to end. `data/push_subscriptions.json`
  is currently `[]`.
- **iOS push end-to-end check** requires the user in hand: install the PWA,
  tap "Enable notifications", wait for the next reflection push. The
  server side (`push_web` + subscription persist + 404/410 prune) is
  already verified.
- **G2 browser compatibility.** Unknown until the glasses land.
- **Embedding backfill in `memories.db`.** `build_index` leaves the
  embedding BLOB NULL. Vector search still returns results for existing
  rows; new vault-derived rows fall out of vector queries until a
  sentence-transformers pass is run.

## File Inventory

```
web/                                 (Vite + React source)
bridge/static/                       (built PWA)
bridge/server.py                     (new /api endpoints, form body parser, staleness /health)
core/notify.py                       (push_web via VAPID)
core/import_claude.py                (unpack, parse, extract, write_to_vault, build_index)
core/screen.py                       (session caching)
core/state.py                        (new, staleness helpers)
core/sync_state.py                   (derive_active_sprint)
core/journal.py                      (summary backlinks)
core/retention.py                    (new, daily archive of old reflections)
scripts/retry_failed_imports.py      (new, rerun misses with 30s cooldown)
data/push_subscriptions.json         (gitignored)
data/memory.db                       (slug column + unique index added)
sprints/sprint-11-handoff.md         (this file)
skills/claude-import.md              (pipeline reference)
skills/codex-delegation.md           (new, two-agent routing policy)
```

No systemd unit changes. Cron gained the 04:00 retention entry.

## Recommended Next Sprint

Sprint 12 (Brain Dump plus Proactive Alfred) is the critical path. The
PWA is now the natural capture surface: a free-form text or voice input
that Alfred routes into Todoist, calendar, or journal with per-item
confirmation. The proactive side adds a morning briefing push and a
follow-up mechanism for unanswered "good?" prompts so the 7-minute
resume window does not swallow pending actions silently.
