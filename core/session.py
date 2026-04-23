"""Session management for Alfred voice bridge.

Tracks active claude -p sessions. Within SESSION_TIMEOUT_MINUTES, calls use
--resume to maintain conversation context. After timeout, the expired session
is summarized and saved to the vault, then a fresh session starts.

Session state lives in data/session.json:
{
  "session_id": "uuid",
  "started_at": "ISO timestamp",
  "last_activity": "ISO timestamp",
  "turn_count": 3,
  "timeout_minutes": 7
}
"""
import json
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

ALFRED_HOME = "/mnt/nvme/alfred"
SESSION_FILE = os.path.join(ALFRED_HOME, "data", "session.json")
EASTERN = ZoneInfo("America/New_York")
try:
    from sessions import SESSION_WINDOW_SECONDS  # type: ignore
except ImportError:
    SESSION_WINDOW_SECONDS = 900
SESSION_TIMEOUT_MINUTES = SESSION_WINDOW_SECONDS // 60


def _now():
    return datetime.now(EASTERN)


def load_session():
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_session(data):
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SESSION_FILE)


def clear_session():
    try:
        os.remove(SESSION_FILE)
    except OSError:
        pass


def get_session_info():
    """Determine whether to start a new session or resume an existing one.

    Returns a dict:
      session_id     , UUID to use for this call
      is_new         , True: use --session-id; False: use --resume
      expired_session, dict of old session data if one just timed out, else None
    """
    session = load_session()
    now = _now()

    if session:
        last = datetime.fromisoformat(session["last_activity"])
        elapsed = (now - last).total_seconds() / 60
        timeout = session.get("timeout_minutes", SESSION_TIMEOUT_MINUTES)

        if elapsed < timeout:
            return {
                "session_id": session["session_id"],
                "is_new": False,
                "expired_session": None,
            }
        else:
            return {
                "session_id": str(uuid.uuid4()),
                "is_new": True,
                "expired_session": session,
            }

    return {
        "session_id": str(uuid.uuid4()),
        "is_new": True,
        "expired_session": None,
    }


def touch_session(session_id: str, started_at: str = None):
    """Record activity for session_id. Call after every successful Claude response."""
    now = _now()
    existing = load_session() or {}

    _save_session({
        "session_id": session_id,
        "started_at": started_at or existing.get("started_at") or now.isoformat(),
        "last_activity": now.isoformat(),
        "turn_count": existing.get("turn_count", 0) + 1 if not started_at else 1,
        "timeout_minutes": SESSION_TIMEOUT_MINUTES,
    })


def session_age_seconds(session: dict) -> float:
    """Seconds since a session's last activity."""
    try:
        last = datetime.fromisoformat(session["last_activity"])
        return (datetime.now(EASTERN) - last).total_seconds()
    except Exception:
        return float("inf")
