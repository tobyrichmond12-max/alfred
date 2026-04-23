# Alfred: What's Next

---

## Making Alfred Usable Day-to-Day

### Do Today
- [x] Build the location tracking iOS Shortcut (instructions in sprint-6-handoff.md on Jetson)
- [x] Set Alfred voice Shortcut as Action Button (Settings > Action Button > Shortcut)
- [x] Test: "what's my day look like tomorrow?"
- [x] Test: "add a task to review co-op applications by Wednesday"
- [x] Test: "add a meeting at 3pm Thursday called study group"

### Do This Week
- [ ] Talk to Alfred 3x daily: morning, midday, evening
- [ ] Set up Syncthing between Jetson and laptop for Obsidian vault sync
- [ ] Clean up Todoist: ask Alfred "what are my oldest overdue tasks?"
- [ ] Clean up Google Calendar: ask Alfred "anything wrong with my calendar this week?"
- [ ] Check vault/reflections/ after 3 days to see what Alfred noticed

### Ongoing Habits
- Tell Alfred when things happen: "I just met with <advisor>," "I finished my assignment"
- Tell Alfred decisions: "I'm not applying to <example-company> anymore"
- Tell Alfred preferences: "Don't schedule anything before 10am"
- Evening check-in: "What did I do today?"

---

## V1 Status: COMPLETE

All 6 sprints done. Voice bridge, personality, data pipeline, memory/journal, session persistence, reflective cycles.

### Skipped (moved to future)
- InfluxDB (not needed, cron scripts work)
- State file caching on phone (latency acceptable)
- R1 ring biometrics (hardware not purchased)

---

## Sprint 7: Calendar and Todoist Cleanup - COMPLETE (Apr 20, 2026)

Commits: 1fd30ba, fc5b0a8, 6fb51f6, 534b88f

### Originally planned
Audit calendar for duplicates and outdated events. Triage 48 overdue Todoist tasks. Set up weekly review routine.

### Actually built
- Todoist triage tools: walk-through mode, bulk complete/reschedule/delete, summary command
- Calendar triage tools: duplicate finder, conflict detector, cleanup commands
- Weekly review cron (Sundays 7 PM), generates vault note, Alfred summarizes Monday morning
- Skill docs: skills/calendar-management.md, skills/todoist-management.md
- Skill-candidate tracking in reflect.py (logs repeated request patterns)
- Fixed get_tasks() filter bug (was silently returning all tasks)
- Fixed Alfred's ability to call triage functions (PYTHONPATH, CLAUDE.md tool docs)
- Em dash cleanup across all touched files

---

## Sprint 8: Push Notifications - COMPLETE (Apr 20, 2026)

Commit: 73163c2

Built ntfy self-hosted on Jetson (docker, Tailscale exposure), core/notify.py (push, push_urgent, push_routine), urgency gating in reflect.py, weekly review push, and "anything I should know?" briefing command in CLAUDE.md. Server-side works end-to-end. Client-side abandoned: going to Telegram instead for a richer channel (Sprint 9). ntfy container still running pending Sprint 9 cleanup.

---

## Sprint 10: Screen Awareness - COMPLETE (Apr 20, 2026)

Commit: 5180da2

Built the laptop-side MCP server (context-mcp on Windows, FastMCP, auto-starts on login at C:\Users\<username>\context-mcp\) exposing get_active_window, get_active_url, get_selected_text. Built core/screen.py on the Jetson with get_screen_state() and describe_screen() plus graceful failure. Updated CLAUDE.md so Alfred calls these on direct asks and uses them as context on "save this" / "what is this" style prompts. Verified end-to-end via claude -p.

---

## Next Sprints (V2)

### Development Tools

Codex CLI is installed on the Jetson and laptop as a second coding agent alongside Claude Code (or install via `npm i -g @openai/codex` if missing). Use whichever is faster per task. Both run on existing subscriptions at zero API cost. Codex supports MCP and can run parallel agents for grunt work like boilerplate, tests, and frontend scaffolding. Reach for Codex when the task is mechanical and well-specified, reach for Claude Code when the task needs judgment, codebase familiarity, or coordination across files.

### Sprint 11: Alfred PWA - SHIPPED (Apr 21-22, 2026)

Commits: b35eb49, 7407ac1, f959923, 9f3567b, e8545d2

Built a Progressive Web App hosted on the Jetson over Tailscale at `https://<jetson-tailscale-hostname>`. Now the primary rich interface alongside the Action Button Shortcut. Telegram plan dropped, kept entirely.

Shipped:
- React frontend served from the Jetson, chat UI with text input plus voice memo recording via browser MediaRecorder.
- Conversation history stored server-side and rendered in the PWA, form-encoded POST bodies parsed correctly so /ask works from the chat input.
- Voice memo upload to `POST /api/voice-memo`, transcribed on the Jetson via faster-whisper (base model, int8 CPU), routed through the existing Alfred pipeline. Returns transcript plus reply plus timings.
- Web Push via VAPID. `core/notify.py` gained `push_web()`, reflect.py and weekly_review.py call it alongside ntfy. Subscriptions persist in `data/push_subscriptions.json` (gitignored), 404/410 endpoints prune on the fly.
- Service worker handles push events and notificationclick (focuses or opens the PWA).
- Claude memory import pipeline (`core/import_claude.py`): unpacks the export zip, splits per-conversation JSON, parses, runs per-chunk extraction via `claude -p`, writes to `vault/memory/<category>/`, augments wikilinks. Index step stubbed pending upsert strategy (see TODO in `build_index`).
- Killed the openclaw zombie process, repointed `/` at the bridge.

Hardening pass landed 2026-04-22 overnight (~25 commits):
- Journal summary backlinks (dbafaa5), sprint-field staleness (cf9c675), state staleness detection (ec2e4ca), screen session caching (0be70a4), reflection retention policy (75b41d6), build_index upsert implemented (f77b737), retry script for failed imports (bc32cfc), /health reports staleness (a88e33f).
- Empty-message hardening for /ask GET variants (94e658b), keyword memory search + CLAUDE.md wiring (7f20bb8), guarded ntfy teardown script (d0de660), journal force-regen preserves Alfred's Notes (cbc6ff6), end-to-end smoke test script (bf4e274), end-of-day synthesis pass + 22:30 cron (891ed9e), sync_state refreshes context.summary + current_activity from calendar (529e3b8), memory-recall skill doc (2c0454b), nudge_state.json 14-day retention (454c61d), /brief excludes weekly-review (6851b22).
- Sprint 11 handoff doc written (sprints/sprint-11-handoff.md).

Still open (deferred, not blocking ship):
- Tear down ntfy container and the `tailscale serve --https=8443` rule once PWA push is validated by the user tapping "Enable notifications" on the installed PWA. `data/push_subscriptions.json` is `[]` as of 2026-04-22 so the fallback stays live.
- Browser sub end-to-end check requires iOS 16.4+ Add to Home Screen flow. Server side verified, client side needs the user in-hand confirmation.
- Even Realities G2 browser compatibility check on the same frontend.
- Embedding backfill in `memories.db`. `build_index` leaves the embedding BLOB NULL; sentence-transformers pass can backfill without re-walking the vault.

### Migration: Claude Memory Import

One-time import of all Claude chat history and memories onto the Jetson. Not a sprint, runs as a background batch job after Sprint 11 ships.

- Import raw Claude data export (JSON) to `/mnt/nvme/alfred/vault/imports/claude-export/` with zero condensing.
- Build a processing pipeline that reads each conversation and extracts: facts about the user, people mentioned, decisions made, preferences stated, projects discussed, technical knowledge shared.
- Write extracted knowledge to structured vault notes under `vault/memory/people/`, `vault/memory/decisions/`, `vault/memory/projects/`, and `vault/memory/preferences/`.
- Build the Obsidian wikilink graph connecting entities across conversations so the graph view reflects the full history, not just post-Alfred activity.
- Index everything into the existing SQLite memory store for Alfred's BM25 and vector search.
- Alfred gains deep context on the user's history, relationships, decisions, and thinking patterns without being told twice.
- Source conversations stay preserved under `vault/imports/` for reference and re-processing if the extraction pipeline changes.

### Sprint 12: Brain Dump and Proactive Alfred

- Brain dump flow: free-form capture via PWA text or voice. Alfred routes each item into Todoist, calendar, or journal automatically, with confirmation per routing decision.
- Morning daily briefing auto-pushed to the PWA. Scheduled variant of the existing "Anything I should know?" flow.
- Pending confirmation follow-ups. If the user asked for an action and never said "yes," Alfred re-raises it instead of letting the 7-minute window lapse silently.
- "Anything I should know?" already wired in CLAUDE.md. This sprint routes its output through PWA push rather than the voice bridge alone.

### Sprint 13: Auto-Scheduling

- Alfred reads Todoist tasks plus calendar availability and proposes a daily schedule each morning.
- Approve or reject via the PWA. Alfred creates events on approval.
- Depends on Sprint 12 usage patterns so Alfred knows what the user actually wants blocked out and what stays flexible.

### Sprint 14: Vision and Meeting Awareness

- Wire screenshot decoding to a vision-capable model call so Alfred can describe screen content, not just window titles and URLs.
- Meeting mode: transcription during calls, surface relevant vault notes in real time.
- Builds on the existing screen awareness tools from the completed Sprint 10. Collapses the former "Cluely / Natively" concept into a concrete sprint.

### Sprint 15: Even Realities and Biometrics

- G2 glasses with the PWA as the display interface.
- R1 ring biometrics flowing into `current_state.json`, replacing every placeholder field (sleep, HRV, heart rate, steps, body temp).
- Notification relay from PWA push to glasses HUD.
- Gated on hardware purchase.

### Sprint 16: Live Voice Call

- Real-time streaming audio via WebRTC or WebSocket from the PWA.
- Multi-turn conversation without pressing buttons.
- Action Button Shortcut becomes the fallback for hands-free quick asks. Live call becomes the default conversational mode.

### Sprint 17: Agent Orchestration

- Multi-agent dashboard inside the PWA.
- Alfred spawns parallel agents for complex tasks (research, triage, summarization, code work).
- Shared message bus with dependency tracking.
- Coordinator plus specialized subagent pattern: a lead agent plans, delegates, and merges outputs.

---

## Added During Sprint 7

Features not in the original plan:
- skills/ directory with structured reference docs for calendar and todoist
- Skill-candidate tracking: reflect.py watches for 3+/week repeated patterns and logs to vault/reflections/skill-candidates.md
- Weekly review cron (was loosely planned but not specced)
- Calendar event creation capabilities (all-day, recurring with RRULE)
- Todoist filter bug fix (get_tasks was silently ignoring filters)

---

## Cluely/Natively Concept

Not the cheating angle. The pattern: real-time screen and mic awareness with contextual information surfacing.

For Alfred:
- During meetings: transcribe, pull up relevant vault notes
- While coding: see active file, offer context
- While reading email: surface related tasks
- During class: capture key points for journal

Use Natively (open-source, local-first, Ollama-compatible) as starting point. Ethical approach: Alfred sees YOUR screen only, your personal tool.

---

## Priority Order

1. ~~Use Alfred daily~~ (ongoing habit)
2. ~~Sprint 7 (calendar/Todoist cleanup)~~ DONE
3. ~~Sprint 8 (push notifications, server-side)~~ DONE
4. ~~Sprint 10 (screen awareness, laptop MCP)~~ DONE
5. ~~Sprint 11 (Alfred PWA)~~ DONE. Validation gate (the user enables push on installed PWA) is the only thing remaining; Sprint 12 unblocks once that confirms.
6. Sprint 12 (brain dump plus proactive Alfred, depends on PWA for capture UI and push) is the critical path next.
7. Sprint 13 (auto-scheduling, needs Sprint 12 usage patterns and PWA approve/reject flow)
8. Sprint 14 (vision plus meeting awareness, builds on screen awareness already shipped and the PWA for display)
9. Sprint 15 (Even Realities glasses plus R1 ring, gated on hardware purchase and on PWA G2 compatibility)
10. Sprint 16 (live voice call, replaces the Action Button round-trip, needs PWA as the audio client)
11. Sprint 17 (agent orchestration, needs the PWA as the dashboard surface)
