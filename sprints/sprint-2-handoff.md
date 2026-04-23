# Sprint 2 Handoff: Voice Intelligence and Context Injection

**Date completed:** 2026-04-20
**Status:** Shipped

---

## What Was Built

Sprint 2 made Alfred feel like an actual personal assistant rather than a generic Claude wrapper. The core addition is state injection: every call now receives the user's current context (location, calendar, biometrics, active work) before processing the message. This means Alfred always knows what's going on without being told. Sprint 2 also hardened the iOS Shortcut integration by making the `/ask` endpoint work regardless of how the Shortcut formats its request.

---

## State File Injection

**File:** `/mnt/nvme/alfred/current_state.json`

Every `claude -p` call is now prepended with the contents of this file. The `load_state()` function in `server.py` reads it at request time (not at startup), so edits to the file take effect immediately without restarting the service.

Format:
```json
{
  "as_of": "2026-04-19T20:30:00-04:00",
  "user": { "name": "the user", "location": { ... } },
  "context": { "summary": "...", "current_activity": "..." },
  "calendar": { "next_event": { ... } },
  "biometrics": { "sleep_hours_last_night": 7.1, "hrv_ms": 54, ... },
  "tasks": { "active_sprint": "...", "open_items": [ ... ] },
  "devices": { "airpods": "connected", "r1_ring": "not connected" }
}
```

Alfred uses this silently, never announces it, just knows it. The CLAUDE.md was updated to describe this behavior explicitly: "use it the way a chief of staff would already know the boss's day before the boss walks in."

Biometrics are currently placeholder values. The R1 ring is not connected. When it is, this file should be auto-updated by a daemon; Alfred will pick up the changes on the next request.

---

## /status Command

**File:** `/mnt/nvme/alfred/.claude/commands/status.md`

A Claude Code slash command that instructs Alfred to give a natural spoken-style status brief when the user asks for his status. The command explicitly bans data readouts and field names, requires biometrics in plain English, caps at 3-4 sentences, and ends with an open question.

Example output (actual response from testing):
> "Sunday evening, you're at your desk at home in Boston. Body's in decent shape, slept about seven hours and HRV is solid, but you've barely moved today so you're stiff and sedentary. Nothing on the calendar tonight, classes pick back up tomorrow morning. You just shipped Sprint 1 of the voice bridge and you're rolling into Sprint 2, voice intelligence and context injection."

The `/status` slash command works when running Claude interactively from `/mnt/nvme/alfred/`. For voice, the user just says "what's my status" naturally and Alfred responds from the injected state, no special command needed.

---

## URL Encoding Fix and /ask Endpoint Hardening

The iOS Shortcut was consistently sending `GET /ask` with no query parameter at all, resulting in 422 errors. Diagnosis came from improving the request logging middleware to capture the full URL (including query string) rather than just the path, which revealed the Shortcut was hitting `/ask` bare with no message attached.

**Three input methods now supported on GET /ask:**

| Method | Example | Notes |
|--------|---------|-------|
| Query parameter | `/ask?message=what+is+2+plus+2` | Original GET handler |
| Path segment | `/ask/what+is+2+plus+2` | New, works regardless of Shortcut config |
| No message | `/ask` | Falls back to a brief greeting |

All three apply `unquote_plus()` for URL decoding, handling both `+` (form encoding) and `%20` (percent encoding) for spaces. POST `/ask` still accepts a raw plain-text body and is unchanged.

The path-segment method (`/ask/<message>`) is the most Shortcut-friendly because the URL can be constructed by simple string concatenation in the Shortcut, no query string syntax, no special encoding step required.

---

## CLAUDE.md Changes

Three substantive updates this sprint:

1. **State injection described accurately.** Added explanation that `current_state.json` is prepended to every call, how to use it silently, and what to do when biometrics are placeholders.

2. **Confirmation loop caveated.** Added a note that the architecture is currently stateless, each voice call is a fresh session, so the confirmation loop as described cannot function until session continuity is added. Documented to avoid confusion.

3. **Response length recalibrated.** Original guidance ("2-3 sentences, never more than 4") was producing telegraphic responses that felt like text messages. Updated to 3-5 sentences for most responses, with explicit instruction to sound like "a sharp friend who knows your life" and explain reasoning rather than just pinging back the minimum viable answer. Hard ceiling of 6 sentences for iOS compatibility.

4. **Glasses routing deferred.** Removed the glasses routing section from active guidance since the device isn't connected. Noted as future sprint work.

---

## Request Logging Improvement

The middleware now logs `request.url` (full URL including query string) instead of `request.url.path`. This was essential for debugging the Shortcut, without it, GET requests appeared to have no message even when one was present in the query string.

---

## Known Issues Going Into Sprint 3

**Stateless sessions.** Every voice call is a fresh `claude -p` subprocess with no memory of prior turns. Follow-up questions ("and add that to my tasks") lose context. The confirmation loop in CLAUDE.md is aspirational until this is resolved.

**current_state.json is manually maintained.** Biometrics are placeholders. Calendar is hardcoded. The file has to be edited by hand. Sprint 3 should wire up at least the calendar (Google Calendar API) and ideally the R1 ring for biometrics.

**No acknowledgment of state staleness.** Alfred will answer confidently based on `current_state.json` even if it hasn't been updated in days. There's no freshness check or staleness warning.

**Shortcut still not confirmed working end-to-end with dictated text.** All successful test calls in Sprint 2 were curl from the terminal. The Shortcut hits `/ask` bare with no message, the path-segment handler is a workaround, but the Shortcut itself still needs to be reconfigured to append the dictated text to the URL.

---

## Sprint 3 Candidates

- Wire Google Calendar into `current_state.json` auto-update
- R1 ring biometrics integration
- Session continuity for follow-up questions (`claude --resume`)
- Fix iOS Shortcut to correctly append dictated text to `/ask/` path
- Staleness detection: if `current_state.json` is older than N hours, note it in responses
- Add timestamp to every Alfred response log for easier debugging
