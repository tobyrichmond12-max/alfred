"""
Alfred optimization engine.

Pulls signals from email, canvas, todoist_patterns, and vault/memory/
historical records, then produces:

  - a structured daily optimization (gap-matching, overload warnings, etc.)
  - one actionable coaching line for the morning briefing

Design principles:
  - Never fabricate facts. If a signal is missing, skip the suggestion.
  - Prefer concrete recommendations (specific time, specific task) over
    generic advice ("try to focus more").
  - Every suggestion carries its reasoning so Alfred can explain why
    when asked.

Standard library only. Optional imports of sibling core modules are
lazy so this file can be unit-tested off-Jetson.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("alfred.optimize")

VAULT_MEMORY = Path("vault/memory")

# Matches peak productivity windows from todoist_patterns for gap detection.
TIME_BLOCKS = {
    "early morning": (5, 8),
    "morning": (8, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
    "late night": (21, 24),
}


# ---- data model -------------------------------------------------------------

@dataclass
class Suggestion:
    headline: str
    reason: str
    priority: str = "normal"  # "low", "normal", "high"

    def as_dict(self) -> dict:
        return {"headline": self.headline, "reason": self.reason, "priority": self.priority}


@dataclass
class Optimization:
    date: str
    suggestions: list[Suggestion] = field(default_factory=list)
    coaching: Optional[Suggestion] = None

    def as_dict(self) -> dict:
        return {
            "date": self.date,
            "suggestions": [s.as_dict() for s in self.suggestions],
            "coaching": self.coaching.as_dict() if self.coaching else None,
        }


# ---- calendar gap detection -------------------------------------------------

def _to_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    if isinstance(value, str):
        v = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


def _find_gaps(events: list[dict], window_start: datetime, window_end: datetime,
               min_minutes: int = 60) -> list[tuple[datetime, datetime]]:
    """Return free intervals of at least min_minutes between events."""
    blocks: list[tuple[datetime, datetime]] = []
    for ev in events:
        s = _to_dt(ev.get("start"))
        e = _to_dt(ev.get("end"))
        if s and e and e > window_start and s < window_end:
            blocks.append((max(s, window_start), min(e, window_end)))
    blocks.sort()
    merged: list[tuple[datetime, datetime]] = []
    for s, e in blocks:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    gaps: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for s, e in merged:
        if s - cursor >= timedelta(minutes=min_minutes):
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if window_end - cursor >= timedelta(minutes=min_minutes):
        gaps.append((cursor, window_end))
    return gaps


def _overlaps_block(gap: tuple[datetime, datetime], block_hours: tuple[int, int]) -> Optional[tuple[datetime, datetime]]:
    """Clip the gap to a peak-hour block if any overlap exists."""
    start_h, end_h = block_hours
    day = gap[0].astimezone()
    block_start = day.replace(hour=start_h, minute=0, second=0, microsecond=0)
    block_end = day.replace(hour=end_h % 24, minute=0, second=0, microsecond=0)
    if end_h == 24:
        block_end = block_end + timedelta(hours=24)
    s = max(gap[0], block_start)
    e = min(gap[1], block_end)
    if e - s >= timedelta(minutes=60):
        return (s, e)
    return None


# ---- vault memory -----------------------------------------------------------

def detect_patterns(vault_memory_path: Path = VAULT_MEMORY) -> dict[str, Any]:
    """Scan vault/memory/ for behavioral signals. Returns a dict of rough
    metrics the rest of the engine can consult.

    Looks for:
      - sleep.jsonl: lines of {"date": "YYYY-MM-DD", "hours": float}
      - reflect/*.json: {"date": ..., "mood": int (1-5), "energy": int (1-5)}
      - peak_hours.json: {"peak_day": "Tuesday", "peak_block": "morning"}
        (written by todoist_patterns)

    Missing files are silently skipped. Unknown files are ignored.
    """
    out: dict[str, Any] = {}
    root = Path(vault_memory_path)
    if not root.exists():
        return out

    sleep_file = root / "sleep.jsonl"
    if sleep_file.exists():
        hours: list[float] = []
        for line in sleep_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                hours.append(float(entry.get("hours", 0)))
            except (json.JSONDecodeError, ValueError):
                continue
        recent = hours[-7:]
        if recent:
            out["avg_sleep_last_7"] = sum(recent) / len(recent)
            out["min_sleep_last_7"] = min(recent)

    peak_file = root / "peak_hours.json"
    if peak_file.exists():
        try:
            out["peak"] = json.loads(peak_file.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    reflect_dir = root / "reflect"
    if reflect_dir.exists():
        moods: list[int] = []
        energy: list[int] = []
        for fp in sorted(reflect_dir.glob("*.json"))[-7:]:
            try:
                entry = json.loads(fp.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(entry.get("mood"), int):
                moods.append(entry["mood"])
            if isinstance(entry.get("energy"), int):
                energy.append(entry["energy"])
        if moods:
            out["avg_mood_last_7"] = sum(moods) / len(moods)
        if energy:
            out["avg_energy_last_7"] = sum(energy) / len(energy)

    return out


# ---- optimization -----------------------------------------------------------

def get_daily_optimization(
    state: Optional[dict] = None,
    calendar: Optional[list[dict]] = None,
    tasks: Optional[list[dict]] = None,
    emails: Optional[list[dict]] = None,
    *,
    now: Optional[datetime] = None,
    vault_memory_path: Path = VAULT_MEMORY,
) -> Optimization:
    """Produce a structured optimization for today.

    Inputs are intentionally loose dicts so each caller can feed whatever
    it has. None is valid for any of them.

    calendar items: {"title": str, "start": iso, "end": iso}
    task items: {"name": str, "due": iso|None, "overdue": bool, "priority": int}
    email items: {"bucket": "action_needed"|..., "subject": str}
    state keys used: "last_sleep_hours", "mood" (1-5), "energy" (1-5)
    """
    now = now or datetime.now(timezone.utc).astimezone()
    state = state or {}
    calendar = calendar or []
    tasks = tasks or []
    emails = emails or []
    patterns = detect_patterns(vault_memory_path)

    suggestions: list[Suggestion] = []

    # 1. Gap matching against peak productivity block
    peak = patterns.get("peak") or {}
    peak_block = (state.get("peak_block") or peak.get("peak_block") or "").lower()
    if peak_block in TIME_BLOCKS:
        window_start = now.replace(minute=0, second=0, microsecond=0)
        window_end = window_start.replace(hour=0, minute=0) + timedelta(days=1)
        gaps = _find_gaps(calendar, window_start, window_end, min_minutes=60)
        hardest = _pick_hardest_task(tasks)
        for gap in gaps:
            clipped = _overlaps_block(gap, TIME_BLOCKS[peak_block])
            if clipped:
                s, e = clipped
                when = f"{s.strftime('%H:%M')} to {e.strftime('%H:%M')}"
                target = hardest["name"] if hardest else "your hardest task"
                suggestions.append(Suggestion(
                    headline=f"Block {when} for {target}",
                    reason=(
                        f"That window is inside your {peak_block} peak and there is nothing on the calendar. "
                        "Your completion rate is highest here."
                    ),
                    priority="high" if hardest else "normal",
                ))
                break

    # 2. Sleep / recovery
    sleep = state.get("last_sleep_hours") or patterns.get("avg_sleep_last_7")
    if isinstance(sleep, (int, float)) and sleep < 6:
        earliest_clear_evening = _earliest_clear_evening(calendar, now)
        if earliest_clear_evening:
            suggestions.append(Suggestion(
                headline=f"Protect sleep tonight after {earliest_clear_evening.strftime('%H:%M')}",
                reason=(
                    f"You have been averaging {sleep:.1f}h of sleep. "
                    "Your calendar clears this evening, so an early wind-down is feasible."
                ),
                priority="high",
            ))
        else:
            suggestions.append(Suggestion(
                headline="Cut one thing from tonight to protect sleep",
                reason=f"Recent sleep is {sleep:.1f}h and the evening is booked.",
                priority="normal",
            ))

    # 3. Overdue task load
    overdue = [t for t in tasks if t.get("overdue")]
    if len(overdue) >= 5:
        top = overdue[0].get("name") or "your oldest overdue item"
        suggestions.append(Suggestion(
            headline=f"Triage the {len(overdue)} overdue tasks before taking new ones",
            reason=f"Start with \"{top}\". Carrying this many overdue items drags completion rate for the whole week.",
            priority="high" if len(overdue) >= 10 else "normal",
        ))

    # 4. Actionable emails
    action_emails = [e for e in emails if e.get("bucket") == "action_needed"]
    if len(action_emails) >= 3:
        suggestions.append(Suggestion(
            headline=f"Batch-reply the {len(action_emails)} action emails in one block",
            reason="Switching in and out of email across the day is more expensive than one focused pass.",
            priority="normal",
        ))

    # 5. Low energy / mood
    energy = state.get("energy") or patterns.get("avg_energy_last_7")
    mood = state.get("mood") or patterns.get("avg_mood_last_7")
    if isinstance(energy, (int, float)) and energy <= 2:
        suggestions.append(Suggestion(
            headline="Take a 20-minute walk before your next block",
            reason=f"Reported energy is {energy:.1f}/5. Light movement outperforms caffeine for afternoon dips.",
            priority="normal",
        ))
    elif isinstance(mood, (int, float)) and mood <= 2:
        suggestions.append(Suggestion(
            headline="Start with something small and visible",
            reason=f"Mood is {mood:.1f}/5. A quick completion before the hard task helps the rest of the day.",
            priority="normal",
        ))

    coaching = _pick_coaching(suggestions)
    return Optimization(
        date=now.date().isoformat(),
        suggestions=suggestions,
        coaching=coaching,
    )


def generate_coaching_message(
    state: Optional[dict] = None,
    calendar: Optional[list[dict]] = None,
    tasks: Optional[list[dict]] = None,
    emails: Optional[list[dict]] = None,
    *,
    now: Optional[datetime] = None,
    vault_memory_path: Path = VAULT_MEMORY,
) -> str:
    """Return a single plain-English actionable insight for the briefing.

    Example output:
      "You have a gap at 14:00 that matches your peak productivity window.
       Schedule your hardest task there."
    """
    opt = get_daily_optimization(
        state, calendar, tasks, emails,
        now=now, vault_memory_path=vault_memory_path,
    )
    if opt.coaching is None:
        return "Nothing obvious to optimize today. Good."
    return f"{opt.coaching.headline}. {opt.coaching.reason}"


# ---- helpers ----------------------------------------------------------------

def _pick_hardest_task(tasks: list[dict]) -> Optional[dict]:
    """Pick the task most likely to deserve the peak block."""
    if not tasks:
        return None
    def score(t: dict) -> tuple:
        return (
            1 if t.get("priority", 0) >= 3 else 0,
            1 if any(k in (t.get("name", "") or "").lower()
                     for k in ("write", "draft", "design", "refactor", "study", "deep")) else 0,
            -int(t.get("overdue", False)),
        )
    return sorted(tasks, key=score, reverse=True)[0]


def _earliest_clear_evening(events: list[dict], now: datetime) -> Optional[datetime]:
    """Return the local time after which the rest of today is free, if any."""
    today = now.astimezone().date()
    evening_start = datetime.combine(today, time(19, 0), tzinfo=now.tzinfo)
    day_end = datetime.combine(today + timedelta(days=1), time(0, 0), tzinfo=now.tzinfo)
    latest_end = evening_start
    for ev in events:
        s = _to_dt(ev.get("start"))
        e = _to_dt(ev.get("end"))
        if s and e and s < day_end and e > evening_start:
            latest_end = max(latest_end, e)
    if day_end - latest_end >= timedelta(hours=2):
        return latest_end.astimezone()
    return None


def _pick_coaching(suggestions: list[Suggestion]) -> Optional[Suggestion]:
    if not suggestions:
        return None
    priority_order = {"high": 0, "normal": 1, "low": 2}
    return sorted(suggestions, key=lambda s: priority_order.get(s.priority, 9))[0]


# ---- test block -------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if "--offline" in sys.argv:
        now = datetime(2026, 4, 22, 9, 0, tzinfo=timezone.utc).astimezone()
        calendar = [
            {"title": "Standup",       "start": now.replace(hour=10).isoformat(),
             "end": now.replace(hour=10, minute=30).isoformat()},
            {"title": "Lunch",         "start": now.replace(hour=12).isoformat(),
             "end": now.replace(hour=13).isoformat()},
            {"title": "Advisor",       "start": now.replace(hour=16).isoformat(),
             "end": now.replace(hour=16, minute=30).isoformat()},
        ]
        tasks = [
            {"name": "Draft thesis chapter 3", "due": None, "overdue": False, "priority": 4},
            {"name": "Reply to <advisor>",        "due": None, "overdue": True,  "priority": 2},
        ]
        emails = [
            {"bucket": "action_needed", "subject": "review my PR"},
            {"bucket": "action_needed", "subject": "RSVP by Friday"},
            {"bucket": "action_needed", "subject": "password reset"},
            {"bucket": "newsletter",    "subject": "Substack digest"},
        ]
        state = {
            "last_sleep_hours": 5.1,
            "peak_block": "afternoon",
            "energy": 2,
        }
        opt = get_daily_optimization(state, calendar, tasks, emails, now=now,
                                     vault_memory_path=Path("nonexistent"))
        print(json.dumps(opt.as_dict(), indent=2))
        print()
        print("coaching:", generate_coaching_message(state, calendar, tasks, emails, now=now,
                                                     vault_memory_path=Path("nonexistent")))
        sys.exit(0)

    # Live mode expects callers to have assembled the inputs and passed them in.
    # Running standalone just demonstrates the shape.
    print(get_daily_optimization().as_dict())
