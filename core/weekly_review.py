"""Weekly review. Cron runs this Sunday at 7 PM.

Pulls tasks completed in the past seven days, still-overdue tasks, calendar
events from the past week, and calendar events for the coming week. Writes a
review note to vault/reflections/weekly-review-YYYY-MM-DD.md. Alfred speaks
the short version on Monday morning via get_review_summary.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ALFRED_HOME = "/mnt/nvme/alfred"
REFLECTIONS_DIR = os.path.join(ALFRED_HOME, "vault", "reflections")
EASTERN = ZoneInfo("America/New_York")
TODOIST_API_BASE = "https://api.todoist.com/api/v1"


def _load_env():
    env_file = os.path.join(ALFRED_HOME, ".env")
    if not os.path.exists(env_file):
        return
    for line in open(env_file):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _get_completed_since(since_dt: datetime) -> list:
    """Fetch tasks completed since the given datetime from Todoist."""
    from todoist import _get_token
    since = since_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S")
    params = {"since": since, "until": datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S"), "limit": "200"}
    url = (
        f"{TODOIST_API_BASE}/tasks/completed/by_completion_date?"
        + urllib.parse.urlencode(params)
    )
    results = []
    cursor = None
    try:
        while True:
            q = dict(params)
            if cursor:
                q["cursor"] = cursor
            req = urllib.request.Request(
                f"{TODOIST_API_BASE}/tasks/completed/by_completion_date?"
                + urllib.parse.urlencode(q),
                headers={"Authorization": f"Bearer {_get_token()}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            batch = data.get("items", []) if isinstance(data, dict) else data
            results.extend(batch)
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor:
                break
    except (urllib.error.HTTPError, urllib.error.URLError):
        pass
    return results


def _gather(now: datetime) -> dict:
    from triage_calendar import _list_events, _start_dt, _is_all_day
    from triage_todoist import get_overdue_tasks

    week_start = now - timedelta(days=7)
    completed = _get_completed_since(week_start)
    overdue = get_overdue_tasks()
    past_events = _list_events(days_back=7, days_forward=0)
    future_events = _list_events(days_back=0, days_forward=7)

    return {
        "now": now,
        "week_start": week_start,
        "completed": completed,
        "overdue": overdue,
        "past_events": past_events,
        "future_events": future_events,
        "_start_dt": _start_dt,
        "_is_all_day": _is_all_day,
    }


def _fmt_event_line(e, helpers):
    s = helpers["_start_dt"](e)
    title = e.get("summary", "Untitled")
    loc = e.get("location", "")
    if s is None:
        when = ""
    elif helpers["_is_all_day"](e):
        when = s.strftime("%a %b %-d")
    else:
        when = s.strftime("%a %b %-d, %-I:%M %p")
    parts = [f"- {when}: {title}" if when else f"- {title}"]
    if loc:
        parts.append(f" ({loc})")
    return "".join(parts)


def _render_markdown(data: dict) -> str:
    now = data["now"]
    week_start = data["week_start"]
    completed = data["completed"]
    overdue = data["overdue"]
    past_events = data["past_events"]
    future_events = data["future_events"]
    helpers = {"_start_dt": data["_start_dt"], "_is_all_day": data["_is_all_day"]}

    date_str = now.strftime("%Y-%m-%d")
    range_str = (
        f"{week_start.strftime('%b %-d')} to {now.strftime('%b %-d, %Y')}"
    )
    summary_line = _render_summary(data)

    lines = [
        "---",
        f"date: {date_str}",
        f"time: \"{now.strftime('%H:%M')}\"",
        "tags: [weekly-review]",
        "---",
        "",
        f"# Weekly Review, {range_str}",
        "",
        "## Spoken summary",
        "",
        summary_line,
        "",
        f"## Tasks completed this week ({len(completed)})",
        "",
    ]
    if not completed:
        lines.append("_None._")
    else:
        for t in completed:
            content = t.get("content") or t.get("item_name") or t.get("summary") or "Untitled"
            completed_at = t.get("completed_at") or t.get("completed_date") or ""
            stamp = completed_at[:10] if completed_at else ""
            lines.append(f"- {stamp}: {content}" if stamp else f"- {content}")
    lines += ["", f"## Still overdue ({len(overdue)})", ""]
    if not overdue:
        lines.append("_Nothing overdue._")
    else:
        for t in overdue[:20]:
            lines.append(f"- {t['due_date']}: {t['content']}")
        if len(overdue) > 20:
            lines.append(f"- ...and {len(overdue) - 20} more.")
    lines += ["", f"## Calendar, past week ({len(past_events)})", ""]
    if not past_events:
        lines.append("_No events._")
    else:
        for e in past_events:
            lines.append(_fmt_event_line(e, helpers))
    lines += ["", f"## Calendar, coming week ({len(future_events)})", ""]
    if not future_events:
        lines.append("_No events._")
    else:
        for e in future_events:
            lines.append(_fmt_event_line(e, helpers))
    lines.append("")
    return "\n".join(lines)


def _render_summary(data: dict) -> str:
    completed = len(data["completed"])
    overdue = len(data["overdue"])
    past = len(data["past_events"])
    future = len(data["future_events"])
    now = data["now"]
    monday_label = now.strftime("%B %-d")

    parts = []
    if completed:
        parts.append(f"You closed {completed} tasks this week.")
    else:
        parts.append("You closed zero tasks this week.")
    if overdue:
        oldest = data["overdue"][0]
        parts.append(
            f"{overdue} tasks are still overdue, oldest is '{oldest['content']}' "
            f"from {oldest['due_date']}."
        )
    else:
        parts.append("Nothing is overdue, the pile is clear.")
    parts.append(
        f"Past week had {past} calendar events, coming week has {future}."
    )
    if future:
        first = data["future_events"][0]
        s = data["_start_dt"](first)
        if s is not None:
            if data["_is_all_day"](first):
                when = s.strftime("%a %b %-d")
            else:
                when = s.strftime("%a %-I:%M %p")
            parts.append(
                f"First up is '{first.get('summary', 'Untitled')}' on {when}."
            )
    parts.append(f"Welcome to the week of {monday_label}.")
    return " ".join(parts)


def generate_weekly_review() -> str:
    """Pull data, write the review file, push a notification, return the path."""
    now = datetime.now(EASTERN)
    data = _gather(now)
    os.makedirs(REFLECTIONS_DIR, exist_ok=True)
    path = os.path.join(REFLECTIONS_DIR, f"weekly-review-{now.strftime('%Y-%m-%d')}.md")
    with open(path, "w") as f:
        f.write(_render_markdown(data))
    try:
        from notify import push_telegram
        summary = _render_summary(data)
        push_telegram(summary, priority="high")
    except ImportError:
        pass
    return path


def get_review_summary() -> str:
    """Plain-English summary Alfred can speak Monday morning.

    Prefers the most recent weekly-review file's Spoken Summary section. Falls
    back to a live computation if no review file exists yet.
    """
    import glob
    import re

    files = sorted(glob.glob(os.path.join(REFLECTIONS_DIR, "weekly-review-*.md")))
    if files:
        with open(files[-1]) as f:
            content = f.read()
        m = re.search(
            r"## Spoken summary\n\n(.+?)\n\n##", content, flags=re.DOTALL
        )
        if m:
            return m.group(1).strip()

    now = datetime.now(EASTERN)
    return _render_summary(_gather(now))


if __name__ == "__main__":
    _load_env()
    path = generate_weekly_review()
    print(f"[{datetime.now(EASTERN).isoformat(timespec='seconds')}] weekly review saved: {path}")
