"""
Analyze Todoist completion patterns.

Pulls completed tasks from the Sync API and derives a few behavioral
signals: when the user finishes things, what kind of work they finish,
and whether items ship early or near the deadline.

Auth: `TODOIST_API_TOKEN` env var. The token is a personal API token
from Todoist Integrations settings.

Standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

log = logging.getLogger("alfred.todoist_patterns")

SYNC_URL = "https://api.todoist.com/sync/v9/completed/get_all"
COMPLETED_V1_URL = "https://api.todoist.com/api/v1/tasks/completed/by_completion_date"
REST_URL = "https://api.todoist.com/rest/v2"
REQUEST_TIMEOUT = 15

DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TIME_BLOCKS = [
    ("early morning", 5, 8),
    ("morning", 8, 12),
    ("afternoon", 12, 17),
    ("evening", 17, 21),
    ("late night", 21, 29),  # wraps past midnight via modulo
]

# Task type rules. First match wins when scanning in order.
TYPE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("writing",   ("write", "draft", "edit", "blog", "essay", "letter", "post",
                   "journal", "article", "paper")),
    ("technical", ("code", "debug", "deploy", "refactor", "implement", "fix",
                   "build", "ship", "merge", "pr ", " pr", "test", "review",
                   "commit", "bug")),
    ("meeting",   ("meet", "call", "sync", "standup", "1:1", "1-on-1",
                   "interview", "talk with")),
    ("admin",     ("schedule", "book", "pay", "file", "submit", "renew",
                   "email ", "reply", "organize", "plan ")),
    ("errand",    ("pick up", "grocery", "laundry", "drop off", "mail ",
                   "store", "buy")),
]


# ---- data model -------------------------------------------------------------

@dataclass
class CompletedTask:
    id: str
    task_id: str
    content: str
    completed_at: datetime
    project_id: Optional[str] = None
    due: Optional[datetime] = None
    task_type: str = "other"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "content": self.content,
            "completed_at": self.completed_at.isoformat(),
            "project_id": self.project_id,
            "due": self.due.isoformat() if self.due else None,
            "task_type": self.task_type,
        }


# ---- http -------------------------------------------------------------------

def _token() -> str:
    tok = os.environ.get("TODOIST_API_KEY") or os.environ.get("TODOIST_API_TOKEN")
    if not tok:
        raise RuntimeError("TODOIST_API_KEY is not set")
    return tok


def _sync_get(params: dict) -> dict:
    url = SYNC_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT,
                                context=ssl.create_default_context()) as resp:
        return json.loads(resp.read().decode())


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ---- classification ---------------------------------------------------------

def classify_task(content: str) -> str:
    """Bucket a task's title into a coarse type. Returns 'other' if unknown."""
    text = " " + (content or "").lower() + " "
    for name, keywords in TYPE_RULES:
        for kw in keywords:
            if kw in text:
                return name
    return "other"


# ---- fetch ------------------------------------------------------------------

def _v1_get(params: dict) -> dict:
    url = COMPLETED_V1_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT,
                                context=ssl.create_default_context()) as resp:
        return json.loads(resp.read().decode())


def get_completed_tasks(days: int = 30) -> list[CompletedTask]:
    """Fetch every completed task in the last `days`, newest first.

    Uses the v1 tasks/completed/by_completion_date endpoint with cursor
    pagination. Falls back silently to the legacy v9 sync endpoint for
    environments that still support it.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    until = datetime.now(timezone.utc)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
    until_str = until.strftime("%Y-%m-%dT%H:%M:%S")
    out: list[CompletedTask] = []
    cursor: Optional[str] = None

    while True:
        params = {"since": since_str, "until": until_str, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            payload = _v1_get(params)
        except urllib.error.URLError as exc:
            log.warning("todoist_patterns: v1 fetch failed: %s", exc)
            break

        items = payload.get("items", [])
        if not items:
            break
        for it in items:
            completed_at = _parse_iso(it.get("completed_at"))
            if completed_at is None:
                continue
            due_raw = it.get("due")
            due = None
            if isinstance(due_raw, dict):
                due = _parse_iso(due_raw.get("date"))
            content = it.get("content", "")
            out.append(CompletedTask(
                id=str(it.get("id", "")),
                task_id=str(it.get("task_id", "")),
                content=content,
                completed_at=completed_at,
                project_id=str(it.get("project_id", "")) or None,
                due=due,
                task_type=classify_task(content),
            ))
        cursor = payload.get("next_cursor")
        if not cursor:
            break
        offset += len(items)
    out.sort(key=lambda t: t.completed_at, reverse=True)
    return out


# ---- pattern analysis -------------------------------------------------------

def _time_block(hour: int) -> str:
    for name, start, end in TIME_BLOCKS:
        if end <= 24 and start <= hour < end:
            return name
        if end > 24 and (hour >= start or hour < end - 24):
            return name
    return "late night"


@dataclass
class Patterns:
    total: int
    by_day_of_week: dict[str, int] = field(default_factory=dict)
    by_time_block: dict[str, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)
    early_vs_late: dict[str, int] = field(default_factory=dict)
    peak_day: Optional[str] = None
    peak_block: Optional[str] = None
    avg_latency_days: Optional[float] = None

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


def analyze_productivity_patterns(tasks: Iterable[CompletedTask]) -> Patterns:
    """Aggregate completions into day-of-week, time-of-day, type, and
    early/late-vs-deadline counts."""
    tasks = list(tasks)
    by_day: Counter = Counter()
    by_block: Counter = Counter()
    by_type: Counter = Counter()
    early_late: Counter = Counter({"early": 0, "on_day": 0, "overdue": 0, "no_due": 0})
    latencies: list[float] = []

    for t in tasks:
        local = t.completed_at.astimezone()
        by_day[DOW[local.weekday()]] += 1
        by_block[_time_block(local.hour)] += 1
        by_type[t.task_type] += 1

        if t.due is None:
            early_late["no_due"] += 1
            continue
        completed_local_date = local.date()
        due_local_date = t.due.astimezone().date()
        delta_days = (completed_local_date - due_local_date).days
        if delta_days < 0:
            early_late["early"] += 1
        elif delta_days == 0:
            early_late["on_day"] += 1
        else:
            early_late["overdue"] += 1
        latencies.append(float(delta_days))

    peak_day = by_day.most_common(1)[0][0] if by_day else None
    peak_block = by_block.most_common(1)[0][0] if by_block else None
    avg_latency = sum(latencies) / len(latencies) if latencies else None

    return Patterns(
        total=len(tasks),
        by_day_of_week=dict(by_day),
        by_time_block=dict(by_block),
        by_type=dict(by_type),
        early_vs_late=dict(early_late),
        peak_day=peak_day,
        peak_block=peak_block,
        avg_latency_days=avg_latency,
    )


# ---- summary ----------------------------------------------------------------

def _format_peak_window(tasks: list[CompletedTask], block_name: str) -> str:
    """Given the winning time block, report the specific hours the user hits."""
    hours = [t.completed_at.astimezone().hour for t in tasks]
    if not hours:
        return block_name
    block_map = {name: (start, end) for (name, start, end) in TIME_BLOCKS}
    start, end = block_map.get(block_name, (0, 24))
    in_block = [h for h in hours if start <= h < min(end, 24)]
    if not in_block:
        return block_name
    low = min(in_block)
    high = max(in_block)
    if low == high:
        return f"{block_name} around {low}:00"
    return f"{block_name} between {low}:00 and {high + 1}:00"


def get_pattern_summary(days: int = 30) -> str:
    """One-paragraph behavioral summary for the morning briefing."""
    try:
        tasks = get_completed_tasks(days=days)
    except Exception as exc:
        log.exception("todoist_patterns: fetch failed")
        return f"Todoist pattern data unavailable: {exc}"

    patterns = analyze_productivity_patterns(tasks)
    if patterns.total == 0:
        return f"No completed tasks in the last {days} days."

    parts: list[str] = []
    if patterns.peak_day:
        parts.append(f"You are most productive on {patterns.peak_day}s")
    if patterns.peak_block:
        window = _format_peak_window(tasks, patterns.peak_block)
        if parts:
            parts[-1] = parts[-1] + f" in the {window}"
        else:
            parts.append(f"Peak completion window: {window}")

    type_counts = patterns.by_type
    type_counts.pop("other", None)
    if len(type_counts) >= 2:
        top_two = sorted(type_counts.items(), key=lambda kv: kv[1], reverse=True)[:2]
        if top_two[1][1] > 0:
            ratio = top_two[0][1] / top_two[1][1]
            if ratio >= 1.5:
                parts.append(
                    f"{top_two[0][0].capitalize()} tasks get finished {ratio:.1f}x more often than {top_two[1][0]} tasks"
                )

    el = patterns.early_vs_late
    scheduled = el["early"] + el["on_day"] + el["overdue"]
    if scheduled:
        overdue_pct = 100 * el["overdue"] / scheduled
        early_pct = 100 * el["early"] / scheduled
        if overdue_pct >= 40:
            parts.append(f"{overdue_pct:.0f}% of scheduled tasks land after their due date")
        elif early_pct >= 50:
            parts.append(f"{early_pct:.0f}% of scheduled tasks ship before their due date")

    if patterns.avg_latency_days is not None:
        delta = patterns.avg_latency_days
        if delta < -0.5:
            parts.append(f"On average you finish {abs(delta):.1f} days before due")
        elif delta > 0.5:
            parts.append(f"On average you finish {delta:.1f} days after due")

    return ". ".join(parts) + "."


# ---- test block -------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if "--offline" in sys.argv:
        now = datetime.now(timezone.utc)
        sample = [
            CompletedTask("c1", "t1", "Write thesis intro",
                          now.replace(hour=10), "p", now.replace(hour=12) + timedelta(days=-1),
                          classify_task("Write thesis intro")),
            CompletedTask("c2", "t2", "Debug sync_state race",
                          now.replace(hour=11), "p", None,
                          classify_task("Debug sync_state race")),
            CompletedTask("c3", "t3", "Refactor email module",
                          now.replace(hour=13) - timedelta(days=2), "p", None,
                          classify_task("Refactor email module")),
            CompletedTask("c4", "t4", "Pick up laundry",
                          now.replace(hour=18) - timedelta(days=1), "p",
                          now.replace(hour=0) - timedelta(days=2),
                          classify_task("Pick up laundry")),
            CompletedTask("c5", "t5", "Schedule DMV appointment",
                          now.replace(hour=9) - timedelta(days=3), "p", None,
                          classify_task("Schedule DMV appointment")),
        ]
        patterns = analyze_productivity_patterns(sample)
        print(json.dumps(patterns.as_dict(), indent=2, default=str))
        print("classify:", classify_task("write a blog post about"))
        print("classify:", classify_task("deploy the new version"))
        print("time block 10:", _time_block(10))
        print("time block 22:", _time_block(22))
        sys.exit(0)

    if not (os.environ.get("TODOIST_API_KEY") or os.environ.get("TODOIST_API_TOKEN")):
        raise SystemExit("set TODOIST_API_KEY, or run with --offline")

    print(get_pattern_summary(30))
