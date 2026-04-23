---
name: calendar-management
description: Reference for reading, creating, updating, and deleting events on the user's Google Calendar via core/gcal.py and core/triage_calendar.py. Covers auth, RRULE recurrence, all-day events, triage, and common scheduling patterns.
---

# Calendar Management

Modules: `core/gcal.py` (read, create, format) and `core/triage_calendar.py` (duplicates, conflicts, bulk delete). All times are America/New_York (Eastern). `EASTERN` is exported from `gcal` for convenience. Calendar ID defaults to `"primary"`.

## Auth

- Credentials: `/mnt/nvme/alfred/config/google_credentials.json` (OAuth client, from Google Cloud Console).
- Token: `/mnt/nvme/alfred/config/google_token.json` (refresh token, auto-refreshed on use).
- Scope: `https://www.googleapis.com/auth/calendar` (read and write).
- First-time setup: `python3 /mnt/nvme/alfred/core/gcal_auth.py` runs the OAuth flow and writes the token file.
- `get_credentials()` raises `RuntimeError` if the token is missing or irrecoverable. `is_configured()` only checks the creds file, it does not validate the token. Always wrap calls in try/except when failure must be soft (e.g. background state building).

## Functions in gcal.py

### `is_configured() -> bool`
True iff the creds file exists. Does not check token validity.

### `get_credentials() -> Credentials`
Returns valid OAuth credentials, refreshing the token if expired. Raises `RuntimeError` if no valid token is available.

### `get_calendar_events(days: int = 7) -> list[dict] | None`
Primary calendar, from now through `now + days`, single events expanded (recurring instances materialized), ordered by start time, max 20. Returns `None` if not configured or on auth error (stderr gets a message). Each item is the raw Google Calendar API event dict.

```python
from core.gcal import get_calendar_events
events = get_calendar_events(days=14)
```

### `create_event(summary, start, end, all_day=False, location="", description="", recurrence=None, calendar_id="primary") -> dict`

Creates an event and returns the API response (includes `id` and `htmlLink`).

- **Timed events**: `start` and `end` are `datetime` objects. Naive datetimes are assumed Eastern. Pass `tzinfo=EASTERN` to be explicit.
- **All-day events**: pass `all_day=True`, and `start`/`end` as `"YYYY-MM-DD"` strings. **`end` is exclusive**. A single-day all-day event on April 24 uses `start="2026-04-24"`, `end="2026-04-25"`.
- **Recurring events**: pass `recurrence` as a list of RRULE strings (see RRULE section below). Combines with all-day or timed.

### `get_calendar_for_state() -> dict | None`
Used by the state builder. Returns `{today, today_events, tomorrow_events, upcoming, next_event, last_synced}` with events pre-formatted via `_format_event`. Not for general querying. Use `get_calendar_events` instead.

### `_format_event(event) -> dict` (internal)
Normalizes a raw event into `{title, when, time, date, location}`. Detects all-day vs timed by the presence of `T` in the date string.

## Functions in triage_calendar.py

### `get_calendar_summary(days_back=30, days_forward=30) -> str`
Plain-English summary Alfred can speak. Covers total events, busiest day, empty days, duplicate pairs, events missing title or location. No markdown.

### `find_duplicates(days_back=30, days_forward=30) -> list[tuple[dict, dict]]`
Returns pairs of events whose normalized titles match (equal or substring) and whose starts fall within one hour. Each dict has `id`, `summary`, `start`, `location`. Weekly recurring classes do not self-trigger because their occurrences are on different days.

### `find_conflicts(days_back=0, days_forward=30) -> list[tuple[dict, dict]]`
Returns pairs of timed events whose intervals overlap. All-day events are ignored so they do not conflict with every timed event on the same day.

### `delete_event(event_id) -> bool`
Delete a single event. Returns True on success, False on error. Deleted events land in Google's trash and can be restored for 30 days by patching `status` to `confirmed`.

### `bulk_delete(event_ids) -> dict`
Delete multiple events with a one-second pause between calls. Returns `{'success': N, 'failed': N, 'failed_ids': [...]}`.

## Creating events

### Single timed event

```python
from datetime import datetime
from core.gcal import create_event, EASTERN

create_event(
    "DMV Appointment",
    datetime(2026, 4, 24, 9, 0, tzinfo=EASTERN),
    datetime(2026, 4, 24, 10, 0, tzinfo=EASTERN),
    location="Bakers Basin Road, Lawrence Township, NJ",
)
```

### All-day event

```python
create_event("NYC", "2026-04-24", "2026-04-25", all_day=True)
```

Multi-day all-day (Apr 24 through Apr 26 inclusive): `start="2026-04-24", end="2026-04-27"`.

### Recurring event

```python
create_event(
    "Investments (FINA 3303)",
    datetime(2026, 5, 6, 9, 50, tzinfo=EASTERN),
    datetime(2026, 5, 6, 11, 30, tzinfo=EASTERN),
    location="Dodge Hall Room 470, Boston Campus",
    recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH;UNTIL=20260622T035959Z"],
)
```

The `start`/`end` define the first occurrence AND the time-of-day for all occurrences. The RRULE expands from there.

## RRULE cookbook

RRULEs follow RFC 5545. The `UNTIL` value is in UTC (append `Z`). Convert the local end-of-day cutoff to UTC yourself. For Eastern during DST (EDT, UTC minus 4), `2026-06-21 23:59:59` local equals `20260622T035959Z`.

| Pattern | RRULE |
|---|---|
| Weekdays (Mon through Fri) | `RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR` |
| Class schedule Mon through Thu through end date | `RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH;UNTIL=20260622T035959Z` |
| Every Tue and Thu, 12 occurrences | `RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=12` |
| Every other Monday | `RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO` |
| Monthly on the 15th | `RRULE:FREQ=MONTHLY;BYMONTHDAY=15` |
| First Monday of each month | `RRULE:FREQ=MONTHLY;BYDAY=1MO` |
| Daily for 30 days | `RRULE:FREQ=DAILY;COUNT=30` |

Start the event on a day that matches `BYDAY`. Otherwise some clients include the DTSTART as an extra occurrence outside the rule.

## Reading, updating, deleting

`core/gcal.py` exposes `get_calendar_events` for reading. `core/triage_calendar.py` exposes `delete_event` and `bulk_delete`. For updates or for richer queries (by ID, by query string, including past events, recurring parents), call the Google API client directly:

```python
from googleapiclient.discovery import build
from core.gcal import get_credentials

service = build("calendar", "v3", credentials=get_credentials())

from datetime import datetime, timedelta
from core.gcal import EASTERN
now = datetime.now(EASTERN)
hits = service.events().list(
    calendarId="primary",
    q="DMV",
    timeMin=now.isoformat(),
    timeMax=(now + timedelta(days=60)).isoformat(),
    singleEvents=True,
).execute().get("items", [])

event = service.events().get(calendarId="primary", eventId=event_id).execute()

service.events().patch(
    calendarId="primary",
    eventId=event_id,
    body={"location": "New location"},
).execute()

service.events().delete(calendarId="primary", eventId=event_id).execute()

service.events().patch(
    calendarId="primary",
    eventId=event_id,
    body={"status": "confirmed"},
).execute()
```

To query recurring parent events (not expanded instances), pass `singleEvents=False`. Note that `orderBy="startTime"` requires `singleEvents=True`.

Recurring-event caveats:
- Deleting the parent event deletes the whole series.
- To delete one instance, fetch it via `instances()` to get the instance-specific `id`, then `delete()` that id.
- To change one instance only, `patch` the instance id. To change the series going forward, end the parent with `UNTIL` and create a new event from the split date.

## Common patterns

**Weekly class schedule (semester)**: one `create_event` per class, with `start` on the first class meeting of the term and the RRULE's `UNTIL` set to end-of-day UTC of the last class date. Use `BYDAY` to list meeting days. Include room in `location` so the state builder surfaces it.

**All-day travel**: one all-day event per trip. For a day-trip, single-day all-day (`end = start + 1 day`). For a multi-day trip, span the full range. `end` is exclusive, so Apr 24 through Apr 26 uses `end="2026-04-27"`.

**Appointments with locations**: always populate `location`. `get_calendar_for_state` passes it through to Alfred's state, which drives nudges ("leave by X for Y"). Full street address enables map links in clients.

**Cleanup workflow**: call `get_calendar_summary()` first to frame the state, then `find_duplicates()` or `find_conflicts()` to list specific pairs, then confirm each with the user before calling `delete_event` or `bulk_delete`. Do not delete by title match alone. Always inspect the event id first to avoid wiping legitimate pre-existing events.

**Bulk create from a list**: loop over a list of dicts and call `create_event` for each. The API has generous per-user quota, no need to batch.

**Avoiding duplicates**: `create_event` has no idempotency guard. Before a bulk-create, query with the same summary/date range and skip existing. To remove an accidental duplicate, find by id (returned from create) or by `q=` search, then `delete_event`.

## Timezone notes

- `EASTERN = tz.gettz("America/New_York")` in `gcal.py`, `ZoneInfo("America/New_York")` in `triage_calendar.py`. Both handle EST/EDT transitions automatically and interoperate fine.
- All event bodies written by `create_event` set `timeZone: "America/New_York"`. Events are stored in Eastern and rendered in the viewer's local zone.
- RRULE `UNTIL` must be UTC with a `Z` suffix when DTSTART has a timezone. During EDT (March through November), subtract 4 hours from Eastern to get UTC. During EST, subtract 5.
