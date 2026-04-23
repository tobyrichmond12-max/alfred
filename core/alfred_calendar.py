"""Calendar convenience wrappers used by the Telegram bot.

Delegates to triage_calendar for summary and gcal for raw events.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)


def week_summary(days: int = 7) -> str:
    """Spoken summary of the next `days` days."""
    try:
        from triage_calendar import get_calendar_summary  # type: ignore

        return get_calendar_summary(days_back=0, days_forward=max(1, int(days)))
    except Exception as exc:
        return f"Calendar lookup failed: {exc}"


def next_event() -> dict | None:
    """Return the next upcoming event as a dict, or None."""
    try:
        from gcal import get_calendar_events  # type: ignore

        events = get_calendar_events(days=7)
        if not events:
            return None
        now = datetime.now().astimezone()
        for e in events:
            start = e.get("start")
            if not start:
                continue
            start_dt = start.get("dateTime") or start.get("date")
            if not start_dt:
                continue
            try:
                if len(start_dt) == 10:
                    dt = datetime.fromisoformat(start_dt)
                else:
                    dt = datetime.fromisoformat(start_dt)
            except ValueError:
                continue
            if dt.astimezone() >= now:
                return {
                    "title": e.get("summary", "(no title)"),
                    "start": start_dt,
                    "location": e.get("location"),
                    "id": e.get("id"),
                }
        return None
    except Exception:
        return None


def events_in_window(hours: int = 4) -> list:
    try:
        from gcal import get_calendar_events  # type: ignore

        events = get_calendar_events(days=1) or []
    except Exception:
        return []
    cutoff = datetime.now().astimezone() + timedelta(hours=hours)
    out = []
    for e in events:
        start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
        if not start:
            continue
        try:
            dt = datetime.fromisoformat(start)
        except ValueError:
            continue
        if dt.astimezone() <= cutoff:
            out.append({
                "id": e.get("id"),
                "title": e.get("summary", "(no title)"),
                "start": dt,
                "location": e.get("location"),
            })
    return out
