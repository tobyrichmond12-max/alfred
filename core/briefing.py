"""Briefing assembly. Composes the 'anything I should know' response.

Phase 1 ships a minimal composition that pulls from the three sources
already wired in CLAUDE.md:
- triage_todoist.get_triage_summary()
- triage_calendar.get_calendar_summary(0, 7)
- the newest vault/reflections/*.md that is not weekly-review or
  skill-candidates

Phase 9 extends this with email, Canvas, coaching, and weather.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ALFRED_HOME = "/mnt/nvme/alfred"
_core = os.path.join(ALFRED_HOME, "core")
if _core not in sys.path:
    sys.path.insert(0, _core)

REFLECTIONS_DIR = Path(ALFRED_HOME) / "vault" / "reflections"


def _latest_reflection_body() -> str:
    if not REFLECTIONS_DIR.exists():
        return ""
    files = []
    for p in REFLECTIONS_DIR.glob("*.md"):
        n = p.name
        if n.startswith("weekly-review") or n == "skill-candidates.md":
            continue
        files.append(p)
    if not files:
        return ""
    files.sort(key=lambda p: p.name, reverse=True)
    try:
        body = files[0].read_text()
    except OSError:
        return ""
    # grab everything after the first "# Reflection" header if present
    lines = body.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("# Reflection"):
            start = i + 1
            break
    return "\n".join(lines[start:]).strip()


def _safe(fn, default=""):
    try:
        return fn() or default
    except Exception as exc:
        return f"({fn.__name__ if hasattr(fn, '__name__') else 'source'} failed: {exc})"


def get_briefing() -> str:
    from datetime import datetime

    try:
        from triage_todoist import get_triage_summary  # type: ignore
    except Exception:
        def get_triage_summary():
            return ""
    try:
        from triage_calendar import get_calendar_summary  # type: ignore
    except Exception:
        def get_calendar_summary(a=0, b=7):
            return ""

    today = datetime.now().strftime("%A, %B %d")
    parts = [f"Briefing for {today}."]

    cal = _safe(lambda: get_calendar_summary(0, 7))
    if cal:
        parts.append(cal)

    triage = _safe(get_triage_summary)
    if triage and "0 overdue" not in triage.lower():
        parts.append(triage)

    reflection = _latest_reflection_body()
    if reflection:
        # first 3 lines of the newest reflection
        parts.append(reflection.split("\n\n")[0][:400])

    # Phase 9 extensions, all best-effort
    try:
        from gmail import get_email_summary  # type: ignore

        summary = get_email_summary(hours=12)
        if summary:
            parts.append(summary)
    except Exception:
        pass

    try:
        from canvas import get_academic_summary  # type: ignore

        summary = get_academic_summary(days=7)
        if summary:
            parts.append(summary)
    except Exception:
        pass

    try:
        from optimize import generate_coaching_message  # type: ignore
        from sync_state import snapshot  # type: ignore

        state = snapshot()
        coaching = generate_coaching_message(state=state, calendar=None, tasks=None, emails=None)
        if coaching:
            parts.append(coaching)
    except Exception:
        pass

    try:
        from browser_tools import research  # type: ignore

        weather = research("weather in Boston today", depth="quick").summary
        if weather:
            parts.append(weather[:200])
    except Exception:
        pass

    return "\n\n".join(parts)


if __name__ == "__main__":
    print(get_briefing())
