"""Pulls live data (Todoist, Google Calendar) and refreshes current_state.json.

Runs every 10 minutes via cron. Preserves fields it doesn't own (biometrics,
location, devices, context) so other processes can write those independently.

Usage:
  python3 /mnt/nvme/alfred/core/sync_state.py
  python3 /mnt/nvme/alfred/core/sync_state.py --dry-run
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

from dateutil import tz

ALFRED_HOME = "/mnt/nvme/alfred"
STATE_FILE = os.path.join(ALFRED_HOME, "current_state.json")
ROADMAP_FILE = os.path.join(ALFRED_HOME, "alfred-whats-next.md")
EASTERN = tz.gettz("America/New_York")

# Matches "### Sprint N: <title> - <status> (optional date)" in the roadmap.
# Anchored statuses (case-insensitive): SHIPPED, COMPLETE, DONE. Anything
# else (IN PROGRESS, planned) does not count as done.
_SPRINT_HEADING_RE = re.compile(
    r"^###\s+Sprint\s+(\d+):\s*([^\n]+?)\s+-\s+(SHIPPED|COMPLETE|DONE)\b",
    re.IGNORECASE | re.MULTILINE,
)
_SPRINT_NEXT_RE = re.compile(
    r"^###\s+Sprint\s+(\d+):\s*([^\n]+?)(?:\s+-\s+([^\n]+))?$",
    re.MULTILINE,
)

sys.path.insert(0, os.path.join(ALFRED_HOME, "core"))


def load_env():
    env_file = os.path.join(ALFRED_HOME, ".env")
    if not os.path.exists(env_file):
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


def load_state():
    with open(STATE_FILE) as f:
        return json.load(f)


def derive_active_sprint() -> str:
    """Compute the active_sprint string from alfred-whats-next.md.

    Returns "<last shipped sprint>; next: <next sprint>" or the best partial.
    Falls back to "" if the roadmap cannot be read, so nothing in the state
    file is ever blanked out silently: the setdefault below keeps whatever
    was there before.
    """
    try:
        with open(ROADMAP_FILE) as f:
            text = f.read()
    except OSError:
        return ""

    shipped = _SPRINT_HEADING_RE.findall(text)
    if not shipped:
        return ""
    shipped_n, shipped_title, shipped_status = max(
        shipped, key=lambda row: int(row[0])
    )
    last_n = int(shipped_n)

    next_sprint = None
    for n, title, status in _SPRINT_NEXT_RE.findall(text):
        if int(n) <= last_n:
            continue
        status_norm = (status or "").strip().upper()
        if status_norm in {"SHIPPED", "COMPLETE", "DONE"}:
            continue
        next_sprint = (int(n), title.strip())
        break

    parts = [f"Sprint {last_n} ({shipped_title.strip()}) {shipped_status.lower()}"]
    if next_sprint:
        parts.append(f"next: Sprint {next_sprint[0]} ({next_sprint[1]})")
    return "; ".join(parts)


def save_state(state, dry_run=False):
    if dry_run:
        print(json.dumps(state, indent=2))
        return
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def sync_todoist(state):
    from data_sources import get_todoist_tasks

    tasks = get_todoist_tasks()
    if isinstance(tasks, dict) and "error" in tasks:
        print(f"Todoist error: {tasks['error']}", file=sys.stderr)
        return state

    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    overdue = [t for t in tasks if t.get("due_date") and t["due_date"] < today]
    due_today = [t for t in tasks if t.get("due_date") == today]
    upcoming = [t for t in tasks if t.get("due_date") and t["due_date"] > today]

    open_items = []
    for t in overdue[:3]:
        open_items.append(f"OVERDUE: {t['content']}")
    for t in due_today[:5]:
        open_items.append(t["content"])
    for t in upcoming[:5]:
        open_items.append(f"{t['content']} (due {t['due']})")

    tasks = state.setdefault("tasks", {})
    tasks.update({
        "overdue_count": len(overdue),
        "due_today_count": len(due_today),
        "open_items": open_items,
        "last_synced": datetime.now(EASTERN).isoformat(),
    })
    derived_sprint = derive_active_sprint()
    if derived_sprint:
        tasks["active_sprint"] = derived_sprint
    else:
        tasks.setdefault("active_sprint", "")
    print(f"  Todoist: {len(overdue)} overdue, {len(due_today)} due today, {len(upcoming)} upcoming")
    return state


def _event_datetime(event: dict, today_iso: str) -> datetime | None:
    """Best-effort parse of an event's start. Returns None for all-day events."""
    if not event:
        return None
    time_str = (event.get("time") or "").strip()
    date_str = (event.get("date") or today_iso).strip()
    if not time_str or time_str.lower() == "all day":
        return None
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %I:%M %p").replace(
            tzinfo=EASTERN
        )
    except ValueError:
        return None


def refresh_context(state):
    """Update state.context.summary and context.current_activity from the calendar.

    Runs after sync_calendar. Purely a display-side refresh: nothing here
    changes user-entered fields. Preserves any existing context.energy
    value since that is manual-only.
    """
    now = datetime.now(EASTERN)
    today_iso = now.strftime("%Y-%m-%d")
    cal = state.get("calendar") or {}
    today_events = cal.get("today_events") or []

    in_progress = None
    upcoming = None
    for ev in today_events:
        start = _event_datetime(ev, today_iso)
        if not start:
            continue
        minutes_until = (start - now).total_seconds() / 60
        if minutes_until <= 0 and minutes_until >= -90:
            in_progress = (ev, minutes_until)
        elif minutes_until > 0 and upcoming is None:
            upcoming = (ev, minutes_until)

    if in_progress:
        ev, _ = in_progress
        current = f"In: {ev.get('title', 'calendar event')}"
    elif upcoming and upcoming[1] <= 30:
        ev, minutes = upcoming
        current = f"Heading to: {ev.get('title', 'next event')} in ~{int(minutes)} min"
    else:
        current = "Free time."

    time_bucket = (
        "early morning" if now.hour < 7
        else "morning" if now.hour < 12
        else "afternoon" if now.hour < 17
        else "evening" if now.hour < 22
        else "late night"
    )
    summary_parts = [f"{now.strftime('%A')} {time_bucket}."]
    next_event = cal.get("next_event") or (upcoming[0] if upcoming else None)
    if next_event and next_event.get("title"):
        summary_parts.append(f"Next up: {next_event['title']} at {next_event.get('time', '')}.".strip())
    summary = " ".join(p for p in summary_parts if p).strip()

    ctx = state.setdefault("context", {})
    ctx["summary"] = summary
    ctx["current_activity"] = current
    ctx.setdefault("energy", "moderate")
    return state


def sync_calendar(state):
    try:
        from gcal import get_calendar_for_state

        cal = get_calendar_for_state()
        if cal is None:
            print("  Calendar: not configured (run gcal_auth.py to set up)")
            return state
        state["calendar"] = cal
        today_count = len(cal.get("today_events", []))
        next_evt = cal.get("next_event", {}) or {}
        print(f"  Calendar: {today_count} events today, next: {next_evt.get('title', 'none')}")
    except Exception as e:
        print(f"  Calendar error: {e}", file=sys.stderr)
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print result without writing")
    args = parser.parse_args()

    load_env()
    state = load_state()

    print(f"[{datetime.now(EASTERN).strftime('%H:%M:%S')}] Syncing state...")
    state = sync_todoist(state)
    state = sync_calendar(state)
    state = refresh_context(state)
    state["as_of"] = datetime.now(EASTERN).isoformat()

    save_state(state, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"  Saved to {STATE_FILE}")


if __name__ == "__main__":
    main()
