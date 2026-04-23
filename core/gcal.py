"""Google Calendar integration for Alfred."""
import json
import os
import sys
from datetime import datetime, timedelta

from dateutil import tz

ALFRED_HOME = "/mnt/nvme/alfred"
CREDS_FILE = os.path.join(ALFRED_HOME, "config", "google_credentials.json")
TOKEN_FILE = os.path.join(ALFRED_HOME, "config", "google_token.json")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
EASTERN = tz.gettz("America/New_York")


def is_configured():
    return os.path.exists(CREDS_FILE)


def get_credentials(scopes=None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    requested = scopes or SCOPES
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, requested)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("No valid token. Run: python3 /mnt/nvme/alfred/core/gcal_auth.py")

    return creds


def get_calendar_events(days=7):
    if not is_configured():
        return None
    try:
        creds = get_credentials()
    except Exception as e:
        print(f"gcal: {e}", file=sys.stderr)
        return None

    from googleapiclient.discovery import build

    service = build("calendar", "v3", credentials=creds)
    now = datetime.now(EASTERN)
    result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        )
        .execute()
    )
    return result.get("items", [])


def create_event(
    summary,
    start,
    end,
    all_day=False,
    location="",
    description="",
    recurrence=None,
    calendar_id="primary",
):
    """Create a calendar event and return the API response.

    start/end: for all_day, "YYYY-MM-DD" strings (end is exclusive per Google's API).
    For timed events, datetime objects (naive is assumed Eastern).
    recurrence: list of RRULE strings, e.g. ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH;UNTIL=20260622T035959Z"].
    """
    if not is_configured():
        raise RuntimeError("Calendar not configured")
    creds = get_credentials()

    from googleapiclient.discovery import build

    service = build("calendar", "v3", credentials=creds)

    body = {"summary": summary}
    if location:
        body["location"] = location
    if description:
        body["description"] = description

    if all_day:
        body["start"] = {"date": start}
        body["end"] = {"date": end}
    else:
        if start.tzinfo is None:
            start = start.replace(tzinfo=EASTERN)
        if end.tzinfo is None:
            end = end.replace(tzinfo=EASTERN)
        body["start"] = {"dateTime": start.isoformat(), "timeZone": "America/New_York"}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": "America/New_York"}

    if recurrence:
        body["recurrence"] = recurrence

    return service.events().insert(calendarId=calendar_id, body=body).execute()


def _format_event(event):
    start = event.get("start", {})
    dt_str = start.get("dateTime") or start.get("date", "")

    if "T" in dt_str:
        parsed = datetime.fromisoformat(dt_str).astimezone(EASTERN)
        when = parsed.strftime("%A %b %-d at %-I:%M %p")
        time_only = parsed.strftime("%-I:%M %p")
        date = parsed.strftime("%Y-%m-%d")
    else:
        parsed = datetime.strptime(dt_str, "%Y-%m-%d")
        when = parsed.strftime("%A %b %-d (all day)")
        time_only = "all day"
        date = dt_str

    return {
        "title": event.get("summary", "Untitled"),
        "when": when,
        "time": time_only,
        "date": date,
        "location": event.get("location", ""),
    }


def get_calendar_for_state():
    """Return a dict ready to merge into current_state.json['calendar'], or None."""
    events = get_calendar_events(days=7)
    if events is None:
        return None

    now = datetime.now(EASTERN)
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    today_events, tomorrow_events, upcoming = [], [], []
    for e in events:
        start = e.get("start", {})
        dt_str = start.get("dateTime") or start.get("date", "")
        date_part = dt_str[:10]
        fmt = _format_event(e)
        if date_part == today:
            today_events.append(fmt)
        elif date_part == tomorrow:
            tomorrow_events.append(fmt)
        else:
            upcoming.append(fmt)

    all_events = today_events + tomorrow_events + upcoming
    return {
        "today": now.strftime("%A, %B %-d %Y"),
        "today_events": today_events,
        "tomorrow_events": tomorrow_events,
        "upcoming": upcoming[:5],
        "next_event": all_events[0] if all_events else None,
        "last_synced": now.isoformat(),
    }


if __name__ == "__main__":
    import json

    cal = get_calendar_for_state()
    if cal is None:
        print("Calendar not configured or token missing.")
    else:
        print(json.dumps(cal, indent=2))
