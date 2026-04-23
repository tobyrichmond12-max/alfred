"""Alfred's proactive nudge system, checks in throughout the day."""
import json
import os
import sys
import time
import threading
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

_env_path = '/mnt/nvme/alfred/.env'
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

from database import get_db
from config import DB_PATH, DATA_DIR
from data_sources import get_todoist_tasks
from commitments import get_pending_commitments
from goals import get_active_goals


# ============================================================
# ACTIVITY TRACKING (receives pings from iOS automations)
# ============================================================

ACTIVITY_LOG = os.path.join(DATA_DIR, "activity.jsonl")

def log_activity(activity_type, detail, source="ios_automation"):
    """Log an activity event from iOS automation or manual input."""
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "type": activity_type,
        "detail": detail,
        "source": source
    }
    with open(ACTIVITY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def get_recent_activity(hours=2):
    """Get recent activity events."""
    if not os.path.exists(ACTIVITY_LOG):
        return []
    
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    recent = []
    
    with open(ACTIVITY_LOG, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("ts", "") >= cutoff:
                    recent.append(entry)
            except json.JSONDecodeError:
                continue
    
    return recent


def get_activity_summary():
    """Get formatted activity summary for Alfred's context."""
    recent = get_recent_activity(hours=4)
    if not recent:
        return ""
    
    lines = ["## Recent Activity"]
    for a in recent[-10:]:
        ts = a.get("ts", "")[:16].replace("T", " ")
        lines.append(f"- [{ts}] {a.get('type', '?')}: {a.get('detail', '?')}")
    
    return "\n".join(lines)


# ============================================================
# NUDGE GENERATION
# ============================================================

def generate_nudges():
    """Check current state and generate any needed nudges.
    Returns a list of nudge messages with priority levels."""
    from dateutil import tz
    now = datetime.now(tz.gettz("America/New_York"))
    hour = now.hour
    nudges = []
    
    # Don't nudge during sleep hours
    if hour < 7 or hour > 23:
        return nudges
    
    today = now.strftime("%Y-%m-%d")
    
    # Check overdue todoist tasks
    try:
        tasks = get_todoist_tasks()
        if isinstance(tasks, list):
            overdue = [t for t in tasks if t.get("due_date") and t["due_date"] < today]
            if overdue:
                task_names = ", ".join([t["content"] for t in overdue[:3]])
                nudges.append({
                    "priority": "active",
                    "message": f"Sir, you have {len(overdue)} overdue tasks: {task_names}. Shall we address these?"
                })
            
            due_today = [t for t in tasks if t.get("due_date") == today]
            if due_today and hour >= 14:
                task_names = ", ".join([t["content"] for t in due_today[:3]])
                nudges.append({
                    "priority": "info",
                    "message": f"Reminder: {len(due_today)} tasks due today: {task_names}"
                })
    except Exception:
        pass
    
    # Check pending commitments
    try:
        commitments = get_pending_commitments()
        old_commitments = [c for c in commitments 
                          if c.get("ts_created") and c["ts_created"] < (now - timedelta(days=3)).isoformat()]
        if old_commitments:
            nudges.append({
                "priority": "active",
                "message": f"Sir, you have {len(old_commitments)} commitments older than 3 days that remain unresolved."
            })
    except Exception:
        pass
    
    # Check goals - weekly reminder
    if now.weekday() == 0 and hour == 9:  # Monday 9 AM
        try:
            goals = get_active_goals()
            if goals:
                nudges.append({
                    "priority": "info",
                    "message": f"Monday morning, sir. You have {len(goals)} active goals. Shall we review progress?"
                })
        except Exception:
            pass
    
    # Inactivity nudge - if no activity logged in 3+ hours during waking hours
    if 9 <= hour <= 22:
        recent = get_recent_activity(hours=3)
        if not recent:
            nudges.append({
                "priority": "ambient",
                "message": "Sir, I haven't heard from you in a while. Everything proceeding according to plan?"
            })
    
    # Evening review prompt
    if hour == 21:
        nudges.append({
            "priority": "info",
            "message": "Good evening, sir. Shall we do a quick review of today before winding down?"
        })
    
    # Morning briefing
    if hour == 8:
        nudges.append({
            "priority": "active",
            "message": "Good morning, sir. Ready for your daily briefing when you are."
        })
    
    return nudges


NUDGE_STATE_RETENTION_DAYS = 14


def _prune_nudge_state(state):
    """Drop any last_nudge_ts entry older than NUDGE_STATE_RETENTION_DAYS.

    Keeps the file from growing unbounded with unique message keys that will
    never fire again (overdue-task messages shift text every time the count
    changes, for example).
    """
    cutoff = datetime.utcnow() - timedelta(days=NUDGE_STATE_RETENTION_DAYS)
    keep = {}
    for key, ts in (state.get("last_nudge_ts") or {}).items():
        try:
            last_dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if last_dt >= cutoff:
            keep[key] = ts
    state["last_nudge_ts"] = keep
    return state


def get_pending_nudges():
    """Get nudges that should be delivered now."""
    nudge_state_file = os.path.join(DATA_DIR, "nudge_state.json")

    if os.path.exists(nudge_state_file):
        with open(nudge_state_file) as f:
            state = json.loads(f.read())
    else:
        state = {"last_nudge_ts": {}}
    state = _prune_nudge_state(state)

    all_nudges = generate_nudges()
    pending = []

    for nudge in all_nudges:
        msg_key = nudge["message"][:50]
        last_sent = state["last_nudge_ts"].get(msg_key, "")

        # Don't repeat the same nudge within 2 hours
        if last_sent:
            try:
                last_dt = datetime.fromisoformat(last_sent)
                if datetime.utcnow() - last_dt < timedelta(hours=2):
                    continue
            except ValueError:
                pass

        pending.append(nudge)
        state["last_nudge_ts"][msg_key] = datetime.utcnow().isoformat()

    with open(nudge_state_file, "w") as f:
        f.write(json.dumps(state))

    return pending


if __name__ == "__main__":
    print("=== Nudges ===")
    for n in generate_nudges():
        print(f"  [{n['priority']}] {n['message']}")
    
    print("\n=== Recent Activity ===")
    for a in get_recent_activity():
        print(f"  {a}")
