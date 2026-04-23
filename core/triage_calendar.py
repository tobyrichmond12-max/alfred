"""Triage tool for Google Calendar events.

Alfred calls these during voice conversations to surface duplicates, conflicts,
empty days, and cleanup candidates. Bulk operations pause one second between
calls to stay well under Google's per-user write quota.
"""
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gcal import get_credentials

EASTERN = ZoneInfo("America/New_York")
BULK_DELAY_S = 1.0
DUPLICATE_WINDOW_S = 3600


def _service():
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=get_credentials())


def _list_events(days_back: int, days_forward: int) -> list:
    now = datetime.now(EASTERN)
    time_min = (now - timedelta(days=days_back)).isoformat()
    time_max = (now + timedelta(days=days_forward)).isoformat()
    svc = _service()
    events = []
    page_token = None
    while True:
        result = svc.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            pageToken=page_token,
        ).execute()
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events


def _start_dt(event):
    start = event.get("start", {})
    dt_str = start.get("dateTime") or start.get("date")
    if not dt_str:
        return None
    if "T" in dt_str:
        return datetime.fromisoformat(dt_str).astimezone(EASTERN)
    return datetime.strptime(dt_str, "%Y-%m-%d").replace(tzinfo=EASTERN)


def _end_dt(event):
    end = event.get("end", {})
    dt_str = end.get("dateTime") or end.get("date")
    if not dt_str:
        return None
    if "T" in dt_str:
        return datetime.fromisoformat(dt_str).astimezone(EASTERN)
    return datetime.strptime(dt_str, "%Y-%m-%d").replace(tzinfo=EASTERN)


def _is_all_day(event):
    return "date" in event.get("start", {})


def _normalize_title(title: str) -> str:
    return "".join(c.lower() for c in (title or "") if c.isalnum())


def _event_brief(e):
    s = _start_dt(e)
    return {
        "id": e["id"],
        "summary": e.get("summary", ""),
        "start": s.isoformat() if s else "",
        "location": e.get("location", ""),
    }


def find_duplicates(days_back: int = 30, days_forward: int = 30) -> list:
    """Return pairs of events with same or similar titles whose starts fall within one hour.

    Each pair is a tuple of two event-brief dicts (id, summary, start, location).
    """
    events = _list_events(days_back, days_forward)
    enriched = []
    for e in events:
        s = _start_dt(e)
        norm = _normalize_title(e.get("summary", ""))
        if s is None or not norm:
            continue
        enriched.append((s, norm, e))
    pairs = []
    for i in range(len(enriched)):
        s_a, norm_a, a = enriched[i]
        for j in range(i + 1, len(enriched)):
            s_b, norm_b, b = enriched[j]
            if (s_b - s_a).total_seconds() > DUPLICATE_WINDOW_S:
                break
            if norm_a == norm_b or norm_a in norm_b or norm_b in norm_a:
                pairs.append((_event_brief(a), _event_brief(b)))
    return pairs


def find_conflicts(days_back: int = 0, days_forward: int = 30) -> list:
    """Return pairs of timed events whose intervals overlap. All-day events are ignored."""
    events = _list_events(days_back, days_forward)
    timed = []
    for e in events:
        if _is_all_day(e):
            continue
        s, end = _start_dt(e), _end_dt(e)
        if s is None or end is None:
            continue
        timed.append((s, end, e))
    timed.sort(key=lambda t: t[0])
    conflicts = []
    for i in range(len(timed)):
        s_a, e_a, a = timed[i]
        for j in range(i + 1, len(timed)):
            s_b, e_b, b = timed[j]
            if s_b >= e_a:
                break
            if e_b > s_a:
                conflicts.append((_event_brief(a), _event_brief(b)))
    return conflicts


def get_calendar_summary(days_back: int = 30, days_forward: int = 30) -> str:
    """Plain-English summary Alfred can speak. No markdown, no em dashes."""
    events = _list_events(days_back, days_forward)
    total = len(events)
    if total == 0:
        return (
            f"Your calendar has no events between {days_back} days ago "
            f"and {days_forward} days ahead."
        )

    now = datetime.now(EASTERN).date()
    window_start = now - timedelta(days=days_back)
    window_end = now + timedelta(days=days_forward)

    per_day = Counter()
    for e in events:
        s = _start_dt(e)
        if s is not None:
            per_day[s.date()] += 1

    all_days = set()
    d = window_start
    while d <= window_end:
        all_days.add(d)
        d += timedelta(days=1)
    empty_days = all_days - set(per_day)

    busiest_day, busiest_count = None, 0
    if per_day:
        busiest_day, busiest_count = max(per_day.items(), key=lambda kv: kv[1])

    missing_title = sum(1 for e in events if not e.get("summary"))
    missing_location = sum(
        1 for e in events if not _is_all_day(e) and not e.get("location")
    )

    dupes = find_duplicates(days_back, days_forward)

    parts = [
        f"You have {total} events between {days_back} days ago "
        f"and {days_forward} days ahead."
    ]
    if busiest_day and busiest_count >= 2:
        parts.append(
            f"Busiest day is {busiest_day.strftime('%A %B %-d')} "
            f"with {busiest_count} events."
        )
    if empty_days:
        parts.append(f"{len(empty_days)} days in the window have nothing scheduled.")
    if dupes:
        parts.append(f"Found {len(dupes)} possible duplicate pairs.")
    if missing_title:
        parts.append(f"{missing_title} events have no title.")
    if missing_location:
        parts.append(f"{missing_location} timed events have no location.")

    return " ".join(parts)


def delete_event(event_id: str) -> bool:
    """Delete a single event. Returns True on success, False on any error."""
    try:
        _service().events().delete(calendarId="primary", eventId=event_id).execute()
        return True
    except Exception:
        return False


def bulk_delete(event_ids: list) -> dict:
    """Delete multiple events with a one-second pause between calls.

    Returns {'success': N, 'failed': N, 'failed_ids': [...]}.
    """
    success = 0
    failed_ids = []
    for i, eid in enumerate(event_ids):
        if i > 0:
            time.sleep(BULK_DELAY_S)
        if delete_event(eid):
            success += 1
        else:
            failed_ids.append(eid)
    return {"success": success, "failed": len(failed_ids), "failed_ids": failed_ids}


if __name__ == "__main__":
    print(get_calendar_summary())
