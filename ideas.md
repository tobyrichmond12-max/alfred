# Alfred Ideas

Consolidated backlog of deferred ideas, feature requests, and known limitations. Replaces `not-now.md`.

Each item has a one-line description and the source it was pulled from. "Source" points at the handoff doc, roadmap, or conversation where it was first raised. Inclusion here is neutral: not a commitment to build, not a rejection, just a single place to see every open thread.

---

## Interface and UX

- **PWA frontend.** A browser-accessible app for Alfred to complement voice and Telegram. Source: user conversation.
- **Telegram bot.** Replaces ntfy as the rich channel. Bot token acquired 2026-04-22. Sprint 9 is partially unblocked. Open question: does Telegram replace or supplement the PWA as a notification and chat channel? Source: sprints/sprint-8-handoff.md, user conversation.
  - *Blocker, 2026-04-22*: Jetson's current network (<campus> dorm) performs SNI-based filtering that resets the TLS handshake to `api.telegram.org`. TCP connects, ClientHello goes out, peer resets. Google and other HTTPS targets work fine, so it is Telegram-specific. No Tailscale exit nodes are advertised on the tailnet today (iphone174 and <laptop-hostname> are peers only). Sprint 9 needs either an HTTPS proxy scoped to Telegram (Cloudflare Worker relay, wired via `HTTPS_PROXY_TELEGRAM` env var that `core/telegram_bot.py` already honors) or webhook mode on `https://<jetson-tailscale-hostname>/telegram/webhook` so Telegram pushes to us via Tailscale Serve. Scaffold is at `core/telegram_bot.py`.
- **Voice memo support via Telegram.** Incoming OGG, Whisper on Jetson, transcript into the voice pipeline. Source: sprints/sprint-8-handoff.md (Sprint 9 plan).
- **Live voice call via Telegram (WebRTC).** Real-time multi-turn without the Action Button. Source: alfred-whats-next.md (Sprint 13), sprints/sprint-8-handoff.md.
- **Glasses UI on Even Realities G2.** Visual notification relay and HUD for ambient context. Source: alfred-whats-next.md (Sprint 12), sprints/sprint-2-handoff.md (deferred glasses routing).
- **Wake word detection.** Remove the Action Button requirement for spoken invocation. Source: sprints/sprint-1-handoff.md.
- **Processing chime on iOS Shortcut.** Audio or visual feedback during the 4 to 9 second wait. Source: sprints/sprint-1-handoff.md.
- **Matrix-style "working" screen for Claude Code.** While Alfred is mid-tool-call in Claude Code, show a Matrix-rain visual; switch back to the normal view when the run completes. Easy at-a-glance signal for working vs idle. Source: user conversation 2026-04-22.
- **Streaming responses on the voice bridge.** Claude currently runs to completion before the Shortcut gets anything back. Source: sprints/sprint-1-handoff.md.
- **Thread-per-topic decision in Telegram.** Solo chat does not support topics, so pick group-with-one or flatten into inline tags. Source: sprints/sprint-8-handoff.md (open questions).
- **Action Button kept as hands-free fallback.** Once Telegram lands, the Shortcut stays for quick voice-only invocations. Source: sprints/sprint-8-handoff.md.
- **Tailnet-independent reach.** Nothing external-facing today by design; revisit if travel or device sharing makes tailnet-only painful. Source: sprints/sprint-1-handoff.md.

---

## Intelligence

- **Vision pass on screenshots.** Decode the base64 PNG, hand it to a vision-capable model, speak the result. Source: sprints/sprint-10-handoff.md (follow-up recommended next sprint).
- **Meeting transcription.** Considered and explicitly rejected at the laptop-MCP level in Sprint 10. Captured here in case that decision gets revisited. Source: sprints/sprint-10-handoff.md.
- **Continuous screen streaming.** Push window-focus change events from the laptop via SetWinEventHook instead of polling on demand. Source: sprints/sprint-10-handoff.md.
- **Browser extension URL fallback.** 30-line Chrome extension posting to a local port as a safety net if uiautomation breaks on a new browser build. Source: sprints/sprint-10-handoff.md.
- **Selection distinct from clipboard.** Today `get_selected_text` reads the clipboard only, so highlighting without copying is invisible. Source: sprints/sprint-10-handoff.md.
- ~~**Session caching in screen.py.** Reuse the MCP session id across calls so `get_screen_state()` drops from roughly 200 ms to 80 ms.~~ Shipped 2026-04-22 (commit 0be70a4). Source: sprints/sprint-10-handoff.md.
- **Auto-skill discovery.** Reflective cycle drafts full skill candidates from repeated request patterns, not just flagging them. Detection is live, drafting is not. Source: not-now.md, sprints/sprint-7-handoff.md.
- **Skill-candidate tracking verification.** Prompt instruction is live but no three-peat has fired yet; file does not exist. Worth a spot-check. Source: sprints/sprint-7-handoff.md, sprints/sprint-8-handoff.md.
- **Obsidian MCP service.** Journal and reflection reads/writes through Obsidian's API rather than raw filesystem. Source: not-now.md.
- ~~**Import Claude account memories.** Pull context from claude.ai web and Claude Code projects into Alfred's long-term memory store.~~ Shipped 2026-04-21 (pipeline e8545d2, build_index f77b737, retry script bc32cfc). Source: not-now.md.
- **Cluely / Natively style ambient awareness.** Real-time screen plus mic context during meetings, coding, email, and class. Natural endpoint of Sprint 10 + Sprint 13. Source: alfred-whats-next.md.
- **Reel and short-form video ingestion.** iOS share sheet posts an Instagram, TikTok, or YouTube Shorts URL to a new Alfred endpoint. Jetson downloads via yt-dlp, transcribes audio with faster-whisper, samples keyframes for vision, returns a spoken summary. For private or saved Instagram reels, use the user's session cookies rather than standing up a separate Alfred IG account (TOS risk, ban risk). Source: user conversation 2026-04-21.

---

## Productivity

- **Brain-dump flow.** Free-form voice capture that routes each item into Todoist, calendar, or journal based on shape. Source: sprints/sprint-7-handoff.md.
- **Auto-scheduling.** Read tasks plus availability, propose daily calendar blocks, the user approves, Alfred creates events. Source: alfred-whats-next.md (Sprint 11), sprints/sprint-7-handoff.md.
- **Morning briefing as a scheduled push.** "Anything I should know?" briefing exists on demand; scheduled morning variant does not. Source: CLAUDE.md (briefing section), user conversation.
- **Proactive follow-up on unanswered confirmations.** If the user says "add a meeting Thursday" and never says "yes," the 7-minute window lapses silently. No re-raise mechanism. Source: user conversation.
- ~~**End-of-day reflective pass populating "Alfred's Notes".** Journal section exists and is preserved across regeneration, but nothing writes to it automatically beyond reflect.py bullets.~~ Shipped 2026-04-22 (`core/day_summary.py`, 22:30 cron). Synthesizes 2 to 3 sentences and splices a "## Day summary" block above the accumulated bullets. Source: sprints/sprint-4-handoff.md.
- **"What am I working on" voice command.** Writes free-form update into `current_state.json` context and current_activity fields, which are otherwise manual. Source: sprints/sprint-3-handoff.md.
- ~~**Session summary backlinks in journal.** Extend journal.py to pick up `-summary.md` files alongside regular conversation links.~~ Shipped 2026-04-22 (commit dbafaa5). Source: sprints/sprint-5-handoff.md, sprints/sprint-6-handoff.md.
- ~~**<contact-name-c> people note.** Calendar auto-links to `[[people/<contact-c>]]` but the file does not exist.~~ Landed via Claude import pipeline 2026-04-21 (`vault/memory/people/<contact-c>.md`). Source: sprints/sprint-4-handoff.md.

---

## Infrastructure

- **Syncthing vault sync to Mac or iPad.** Carried forward every sprint since Sprint 4. Source: sprints/sprint-4-handoff.md through sprints/sprint-7-handoff.md.
- ~~**Reflection and weekly review retention.** Files accumulate at roughly 8 per day with no pruning or archival.~~ Shipped 2026-04-22 (commit 75b41d6): daily 04:00 cron archives reflections older than 30 days and weekly reviews older than 26 weeks. Source: sprints/sprint-6-handoff.md, sprints/sprint-7-handoff.md.
- ~~**State staleness detection.** Warn Alfred if `current_state.json as_of` is older than 30 minutes.~~ Shipped 2026-04-22 (commit ec2e4ca): `core/state.py` injects a warning into the prompt, `/health` reports `state_stale`. Source: sprints/sprint-2-handoff.md through sprint-5-handoff.md.
- ~~**sync_state.py sprint-field overwrite.** Every sync resets `active_sprint` to a stale value.~~ Shipped 2026-04-22 (commit cf9c675): now derived from the most recent SHIPPED heading in alfred-whats-next.md. Source: sprints/sprint-6-handoff.md.
- ~~**sync_state.py context auto-update.** `context.summary` and `current_activity` are manual-edit-only; nothing writes them.~~ Shipped 2026-04-22: `refresh_context` derives both from the calendar each sync (in/heading-to/free, day of week + time bucket + next event). Source: sprints/sprint-3-handoff.md.
- **Auth and rate limiting on `/ask`.** Any tailnet device can consume Claude credits. Acceptable today, revisit if the tailnet grows. Source: sprints/sprint-1-handoff.md.
- **Python 3.11+ upgrade.** google-api-core drops 3.10 support in October 2026. Not urgent, but dated. Source: sprints/sprint-3-handoff.md.
- **Direct Anthropic API vs `claude -p` subprocess.** Would cut 1 to 2 seconds of cold start and enable streaming. Source: sprints/sprint-1-handoff.md.
- **ntfy teardown.** Container and Tailscale `:8443` rule still running pending Sprint 9 cleanup. Source: sprints/sprint-8-handoff.md.
- **InfluxDB for time series.** Explicitly skipped; revisit when a feature actually needs historical metrics. Source: sprints/sprint-6-handoff.md.
- **Phone-side state cache.** Explicitly skipped; revisit if voice round-trip latency gets painful. Source: sprints/sprint-6-handoff.md.
- **Codex for coding, Claude for Alfred conversation.** Use Codex (subsidized by ChatGPT Pro) as the primary coding tool when iterating on Alfred's own codebase; keep Claude as the conversational and orchestration layer. Revisit if Codex quality drops or if a single-model workflow becomes materially simpler. Source: user preference, 2026-04-21.

---

## Hardware

- **R1 ring biometrics.** Sleep, HRV, heart rate, steps, body temp all placeholder since Sprint 1. Flagged every sprint. Source: sprints/sprint-2-handoff.md onward.
- **G2 glasses purchase and integration.** Notification relay and HUD. Gated on hardware purchase. Source: alfred-whats-next.md (Sprint 12).
- **Apple Health, Oura, or Whoop as biometric fallback.** Alternative source if the R1 ring never lands or adds second opinion data. Mentioned once, never moved anywhere durable. Source: sprints/sprint-3-handoff.md, sprints/sprint-4-handoff.md.

---

## Agent Orchestration

- **Multi-agent dashboard.** A visual surface for watching and steering multiple Alfred sub-agents at once. Source: user conversation (not yet tracked in handoffs).
- **Parallel agent execution.** Run sub-agents concurrently for independent work (research, triage, summarization) instead of serially in one Claude process. Source: user conversation.
- **Shared message bus.** Common channel for agents to publish observations and pick up work, instead of each one writing to its own file. Source: user conversation.
- **Coordinator pattern.** A lead agent that plans, delegates to specialized sub-agents, and merges their outputs. Source: user conversation.

---

## Housekeeping

- When an item ships, strike it from this file in the same commit as the code, and move the one-liner into the relevant sprint handoff's "What Was Built" section.
- When an item is rejected for a reason that matters, leave it here with a short *Rejected, because ...* note rather than silently deleting it. Old rejections have saved sprints already (see meeting transcription in sprint-10-handoff.md).
- New ideas land here first, not in `alfred-whats-next.md`. The roadmap only picks up what we have actually sized and committed to.
