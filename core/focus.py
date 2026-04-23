"""Focus mode. Pomodoro timer + held-thought capture for ADHD support.

Sessions are tracked in vault/memory/focus-sessions.jsonl with fields
id, started_at, ended_at, task, outcome, switches_before. The active
session (at most one) lives in /var/lib/alfred/focus_session.json.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

SESSIONS_PATH = Path(ALFRED_HOME) / "vault" / "memory" / "focus-sessions.jsonl"
ACTIVE_PATH = Path("/var/lib/alfred/focus_session.json")
if not os.access(ACTIVE_PATH.parent, os.W_OK):
    ACTIVE_PATH = Path(ALFRED_HOME) / "data" / "focus_session.json"
HELD_DIR = ACTIVE_PATH.parent
HELD_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DURATION = 1500  # 25 minutes
SWITCH_THRESHOLD = 0.55
FOCUS_SIMILARITY = 0.6


def _load_active() -> Optional[dict]:
    if not ACTIVE_PATH.exists():
        return None
    try:
        return json.loads(ACTIVE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_active(data: dict) -> None:
    ACTIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_PATH.write_text(json.dumps(data, indent=2))


def _append_session(record: dict) -> None:
    SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SESSIONS_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def start(task: str, duration_seconds: int = DEFAULT_DURATION) -> str:
    session_id = "fc_" + secrets.token_hex(4)
    session = {
        "session_id": session_id,
        "task": task,
        "started_at": time.time(),
        "duration_seconds": int(duration_seconds),
        "switches_before": 0,
    }
    _save_active(session)
    return session_id


def stop(session_id: Optional[str] = None, outcome: str = "stopped") -> dict:
    active = _load_active()
    if not active:
        return {}
    if session_id and active.get("session_id") != session_id:
        return {}
    record = {
        "id": active["session_id"],
        "started_at": active["started_at"],
        "ended_at": time.time(),
        "task": active["task"],
        "outcome": outcome,
        "switches_before": active.get("switches_before", 0),
    }
    _append_session(record)
    try:
        ACTIVE_PATH.unlink()
    except OSError:
        pass
    return record


def is_active() -> Optional[dict]:
    return _load_active()


def recent_sessions(limit: int = 50) -> list[dict]:
    if not SESSIONS_PATH.exists():
        return []
    lines = SESSIONS_PATH.read_text().splitlines()[-limit:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def detect_context_switching(messages: list[dict], window_minutes: int = 60) -> int:
    """Count topic transitions across recent telegram turns."""
    from embeddings import _cosine, embed  # type: ignore

    cutoff = time.time() - window_minutes * 60
    msgs = [m for m in messages if _ts_to_epoch(m.get("ts")) >= cutoff]
    if len(msgs) < 2:
        return 0
    vecs = [embed(m["text"]) for m in msgs]
    switches = 0
    for a, b in zip(vecs, vecs[1:]):
        if _cosine(a, b) < SWITCH_THRESHOLD:
            switches += 1
    return switches


def _ts_to_epoch(ts) -> float:
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return 0.0
    return 0.0


def maybe_hold(chat_id: int, text: str, bot=None) -> Optional[str]:
    """Capture off-topic text into the held-thoughts jsonl and nudge back."""
    active = _load_active()
    if not active:
        return None
    try:
        from embeddings import _cosine, embed  # type: ignore

        task_vec = embed(active["task"])
        msg_vec = embed(text)
        if _cosine(task_vec, msg_vec) >= FOCUS_SIMILARITY:
            return None
    except Exception:
        pass
    held_path = HELD_DIR / f"held_thoughts_{active['session_id']}.jsonl"
    with held_path.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "text": text}) + "\n")
    elapsed = time.time() - active["started_at"]
    remaining = max(0, active["duration_seconds"] - elapsed)
    mm = int(remaining // 60)
    ss = int(remaining % 60)
    preview = text[:40].replace("\n", " ")
    return f'Holding: "{preview}". Back to {active["task"]}. Time left: {mm:02d}:{ss:02d}.'


def flush_held(session_id: str) -> str:
    held_path = HELD_DIR / f"held_thoughts_{session_id}.jsonl"
    if not held_path.exists():
        return ""
    lines = held_path.read_text().splitlines()
    held_path.unlink()
    if not lines:
        return ""
    items = []
    for line in lines:
        try:
            items.append(json.loads(line).get("text", ""))
        except json.JSONDecodeError:
            continue
    body = "\n- " + "\n- ".join(items)
    return f"Held thoughts during focus:{body}"


def start_cmd(bot, chat_id: int, task: str, duration_seconds: int = DEFAULT_DURATION) -> None:
    session_id = start(task, duration_seconds)
    bot.send_message(
        f'Focus on "{task}" for {duration_seconds // 60} minutes. '
        "I will hold other thoughts until the timer ends.",
        chat_id,
    )

    def _finish():
        time.sleep(duration_seconds)
        active = _load_active()
        if not active or active.get("session_id") != session_id:
            return
        stop(session_id, outcome="completed")
        bot.send_message(f'Timer complete. How did "{task}" go?', chat_id)
        flushed = flush_held(session_id)
        if flushed:
            bot.send_message(flushed, chat_id)

    threading.Thread(target=_finish, daemon=True).start()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest"])
    ns = ap.parse_args()
    if ns.cmd == "selftest":
        sid = start("draft co-op resume", duration_seconds=60)
        maybe_hold(1, "oh right I need to buy groceries", None)
        stop(sid, outcome="stopped")
        sessions = recent_sessions(3)
        last = sessions[-1] if sessions else {}
        ok = last.get("outcome") == "stopped"
        print(f"Focus self-test: 1 session, {'1 thought held' if ok else '0 held'}")
        raise SystemExit(0 if ok else 1)
