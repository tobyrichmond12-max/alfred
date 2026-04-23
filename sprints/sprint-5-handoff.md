# Sprint 5 Handoff, Session Persistence

**Date:** 2026-04-20
**Status:** Complete

## What Was Built

Session persistence so voice conversations feel continuous rather than stateless.

### core/session.py (new)

Manages `data/session.json`, a single-record JSON file tracking the active Claude CLI session:

```json
{
  "session_id": "uuid",
  "started_at": "ISO timestamp",
  "last_activity": "ISO timestamp",
  "turn_count": 3,
  "timeout_minutes": 7
}
```

Key functions:
- `get_session_info()`, returns `{session_id, is_new, expired_session}`. Checks elapsed time against 7-minute timeout. If within window: `is_new=False`, same UUID. If timed out: `is_new=True`, fresh UUID, old session data in `expired_session`.
- `touch_session(session_id, started_at=None)`, called after every successful response. Increments `turn_count`, updates `last_activity`. Pass `started_at` on new sessions to reset count to 1.
- `load_session()`, `clear_session()`, `session_age_seconds()`, helpers.

### bridge/server.py (modified)

`run_claude()` now:
1. Calls `get_session_info()`
2. If `expired_session` present → fires `_summarize_expired_session_async()` in daemon thread
3. Builds `session_flag = ["--session-id", uuid]` (new) or `["--resume", uuid]` (resume)
4. Passes flag to `claude -p` subprocess
5. On success, calls `touch_session()`

`_summarize_session(session_data)` (background):
- Resumes the expired session with `claude -p --resume OLD_UUID`
- Sends summary prompt: "Summarize this conversation in 2-3 concise sentences for my personal journal."
- Saves result to `vault/conversations/YYYY/MM/YYYY-MM-DD-HHMMSS-summary.md` with frontmatter
- Backlinks to journal note via `← [[journal/YYYY/MM/YYYY-MM-DD]]`

## How It Works

```
Call 1 (new session):
  get_session_info() → is_new=True, fresh UUID
  claude -p --session-id UUID "..."
  touch_session(UUID, started_at=now)  → session.json turn_count=1

Call 2 (within 7 min):
  get_session_info() → is_new=False, same UUID
  claude -p --resume UUID "..."        ← has full conversation context
  touch_session(UUID)                  → turn_count=2

Call 3 (after 7 min timeout):
  get_session_info() → is_new=True, new UUID, expired_session=old data
  _summarize_expired_session_async(old data)  ← daemon thread
  claude -p --session-id NEW_UUID "..."
  touch_session(NEW_UUID, started_at=now)

Background thread:
  claude -p --resume OLD_UUID "Please summarize..."
  → vault/conversations/YYYY/MM/YYYY-MM-DD-HHMMSS-summary.md
```

## Smoke Test Results

```
POST /ask "My name is the user. Just say 'Noted, the user' and nothing else."
→ "Noted, the user."

POST /ask "What is my name? Answer in one word."
→ "the user."
```

Session file after two calls: same `session_id`, `turn_count: 2`. ✓

## Pending

- **Session summary backlinks in journal**, journal.py could be extended to pick up `-summary.md` files alongside regular conversation links. Low priority.
- **iOS Shortcut for GPS location**, `/location` endpoint is live, Shortcut not yet built.
- **Syncthing**, vault sync to Mac/iPad still not configured.
- **Biometrics**, R1 ring not connected, `sleep_hours` and `hrv_ms` still placeholders.
