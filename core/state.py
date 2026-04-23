"""Shared helpers for reading current_state.json and flagging staleness.

The sync job refreshes current_state.json every 10 minutes. If the cron
service dies or the Jetson goes to sleep, the state can drift silently and
Alfred will confidently cite yesterday's calendar and overdue count. These
helpers let callers detect that condition and warn Alfred before he speaks.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

ALFRED_HOME = "/mnt/nvme/alfred"
STATE_FILE = os.path.join(ALFRED_HOME, "current_state.json")

# If current_state.as_of is older than this, Alfred should be warned.
STALE_THRESHOLD_MINUTES = 30


def load_state_raw() -> str:
    """Return the raw JSON text of current_state.json, or "" on error."""
    try:
        with open(STATE_FILE) as f:
            return f.read()
    except OSError:
        return ""


def load_state() -> dict[str, Any]:
    """Return the parsed state dict, or {} on error."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def staleness_minutes(state: dict[str, Any]) -> float | None:
    """How many minutes old is state.as_of? None if unparseable."""
    as_of = (state or {}).get("as_of")
    if not as_of:
        return None
    try:
        ts = datetime.fromisoformat(as_of)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(ts.tzinfo)
    return (now - ts).total_seconds() / 60.0


def staleness_warning(
    state: dict[str, Any], threshold_minutes: int = STALE_THRESHOLD_MINUTES
) -> str:
    """Return a plain-English warning if state is stale, else "".

    The warning is intended to be prepended to Alfred's prompt so he knows
    to caveat time-sensitive answers. Voice-rules-compatible (no em dashes,
    no markdown, one line).
    """
    age = staleness_minutes(state)
    if age is None:
        return ""
    if age < threshold_minutes:
        return ""
    if age < 60:
        age_str = f"{int(age)} minutes"
    elif age < 60 * 24:
        age_str = f"{age / 60:.1f} hours"
    else:
        age_str = f"{age / (60 * 24):.1f} days"
    return (
        f"NOTE: current_state.json is {age_str} old. "
        f"The calendar, task, and location fields may be stale. "
        f"Flag the staleness if you cite anything time-sensitive."
    )
