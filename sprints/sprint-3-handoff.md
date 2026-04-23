# Sprint 3 Handoff: Live Data Pipeline

**Date completed:** 2026-04-20
**Status:** Shipped

---

## What Was Built

Sprint 3 replaced every hardcoded value in `current_state.json` with live data. Alfred now knows the user's actual calendar events and Todoist tasks at the time of every voice call, not a stale snapshot from days ago. The pipeline runs automatically every 10 minutes with no manual intervention. Google Calendar OAuth was configured and is live. Mosquitto MQTT was installed for future IoT/biometrics integration. A `/location` HTTP endpoint was added to the bridge so an iOS Shortcut can push GPS coordinates whenever location changes significantly.

---

## sync_state.py

**File:** `/mnt/nvme/alfred/core/sync_state.py`

The central sync script. Runs every 10 minutes via cron, pulls Todoist and Google Calendar, and writes the results into `current_state.json`. Preserves all fields it doesn't own, biometrics, location, devices, context, so other processes (the iOS location push, the future R1 ring daemon) can write those independently without getting clobbered.

Key behaviors:
- Writes atomically via `.tmp` file + `os.replace()`, the bridge never reads a half-written state
- Loads `.env` itself so it works correctly from cron (no shell environment)
- `--dry-run` flag prints the would-be state to stdout without writing
- Falls back gracefully if Calendar or Todoist are unreachable, logs the error, leaves the existing section unchanged

Cron entry (added to thoth's crontab):
```
*/10 * * * * /usr/bin/python3 /mnt/nvme/alfred/core/sync_state.py >> /mnt/nvme/alfred/logs/sync.log 2>&1
```

---

## Google Calendar Integration

**Files:** `/mnt/nvme/alfred/core/gcal.py`, `/mnt/nvme/alfred/core/gcal_auth.py`

`gcal.py` fetches the next 7 days of events from the primary Google Calendar using the Google Calendar API v3. Returns structured data grouped into `today_events`, `tomorrow_events`, and `upcoming` (next 5). Also surfaces `next_event` as a top-level field for quick access. Token auto-refreshes using the stored refresh token, no re-authorization needed after the one-time setup.

`gcal_auth.py` handles the one-time OAuth flow. Uses a paste-the-redirect-URL approach: prints the auth URL, user visits it on any device, copies the resulting localhost redirect URL from the address bar (even though the browser shows an error), and pastes it back. No SSH tunnel required. Run once per Google account authorization:

```bash
python3 /mnt/nvme/alfred/core/gcal_auth.py
```

**Credentials live at** `/mnt/nvme/alfred/config/`, excluded from git. Never commit these files.

Live data confirmed working:
- 4 events today: co-op class (Richards Hall 11:45), <contact-name-c> Meeting (Forsyth 1:30), Goon Swim (Cabot 7:45)
- Full week ahead populated including classes, water polo, recurring meetings

---

## Todoist Integration

**File:** `/mnt/nvme/alfred/core/data_sources.py` (existing, already wired)

The Todoist API key was already in `.env`. `sync_state.py` calls `get_todoist_tasks()` from `data_sources.py` and writes overdue count, due-today count, and top open items into `current_state.json`. First live sync showed 48 overdue tasks, Alfred now knows about them and can surface them contextually.

The `tasks` section in state now includes:
```json
{
  "active_sprint": "...",
  "overdue_count": 48,
  "due_today_count": 0,
  "open_items": ["OVERDUE: ...", "...", "... (due YYYY-MM-DD)"],
  "last_synced": "..."
}
```

---

## /location Endpoint

**File:** `/mnt/nvme/alfred/bridge/server.py`

New `POST /location` endpoint accepts JSON with `latitude`, `longitude`, and optional `accuracy`. Writes directly into `current_state.json["user"]["location"]` atomically. The iOS Shortcut (see below) calls this whenever location changes significantly.

```bash
curl -X POST https://<jetson-tailscale-hostname>/location \
  -H "Content-Type: application/json" \
  -d '{"latitude": 42.3601, "longitude": -71.0589, "accuracy": 10}'
```

---

## iOS Shortcut: GPS Location Push

Not yet created (deferred, bridge endpoint is ready, Shortcut build is Sprint 4 or on-demand). When built, the Shortcut uses the "Automation" trigger on significant location change and runs:

1. **Get Current Location** action
2. **Get Details of Location** → Latitude, Longitude
3. **Get Contents of URL** (POST) → `https://<jetson-tailscale-hostname>/location`
   - Body: `{"latitude": [Latitude], "longitude": [Longitude], "accuracy": 50}`
   - Headers: `Content-Type: application/json`

---

## Mosquitto MQTT

Installed via apt, running as a systemd service on port 1883 (localhost only by default). No active subscribers yet, installed in anticipation of the R1 ring integration and any other IoT data sources. Test with:

```bash
mosquitto_pub -t alfred/test -m "hello"
mosquitto_sub -t alfred/test
```

---

## Voice Test

Asked: "what's my day look like"

Alfred responded (actual output):
> "You've got a packed Monday. Professional development for business co-op at 11:45 in Richards Hall, then <contact-name-c> at 1:30 in Forsyth, and goon swim at 7:45. You slept about 7 hours, recovery looks decent. Want me to flag anything specific before your 11:45?"

Real calendar data, correct locations, past events filtered out, biometrics from state woven in naturally.

---

## Known Issues Going Into Sprint 4

**Biometrics still placeholders.** The R1 ring is not connected. Steps, HRV, sleep, and heart rate are all hardcoded from Sprint 1. Sprint 4 should either wire up the R1 ring (if an API/BLE integration exists) or find another source (Apple Health export, Oura, Whoop, etc.).

**Location is static until iOS Shortcut is built.** The `/location` endpoint is ready but nothing is pushing to it. Alfred still thinks the user is at his desk at home even when he's on campus.

**48 overdue Todoist tasks.** This is real data, Alfred knows about them but will keep surfacing them until they're closed or rescheduled.

**context and current_activity are stale.** The `context.summary` and `current_activity` fields in state still reflect Sprint 1/2 work. No automated source updates these. Either wire up a manual "what are you working on" voice command that writes to state, or accept that this field needs periodic manual updates.

**FutureWarning from google-api-core.** Python 3.10 will lose Google library support in October 2026. Not urgent but worth upgrading to Python 3.11+ before then.

---

## Sprint 4 Candidates

- iOS Shortcut for GPS location push (endpoint ready)
- Wire biometrics: R1 ring, Apple Health, or another source
- "What am I working on" voice command that updates `context` in state
- Staleness detection: warn Alfred if `as_of` is more than 30 minutes old
- Session continuity (`claude --resume`) for follow-up questions
- Daily journal entry written automatically at end of day from state snapshots
- CLAUDE.md update to reflect Sprint 3 reality (active_sprint field, etc.)
