"""HUD writer and helpers.

- activity(text): append to a ring buffer + publish SSE `activity`
- status(task, started_at): update the current-task indicator + SSE `status`
- feed(kind, text, telegram_deeplink): append to hud_feed.jsonl + SSE `feed`

The bridge subscribes via `register_sse_queue()` and drains events from a
local queue. One queue per open SSE connection; each write pushes onto
every live queue so the browser sees the event. Dead queues drop messages.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
FEED_PATH = Path(ALFRED_HOME) / "vault" / "memory" / "hud_feed.jsonl"
RING_MAX = 500
FEED_KINDS = {"git", "task", "conf", "stt", "note", "read"}

_activity_ring: deque = deque(maxlen=RING_MAX)
_current_status: dict = {"task": "idle", "started_at": None}
_ring_lock = threading.Lock()
_status_lock = threading.Lock()

# One queue per open SSE connection
_sse_queues: list[queue.Queue] = []
_sse_lock = threading.Lock()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _emit(event: str, data: dict) -> None:
    payload = {"event": event, "data": data}
    dead: list[queue.Queue] = []
    with _sse_lock:
        snapshot = list(_sse_queues)
    for q in snapshot:
        try:
            q.put_nowait(payload)
        except queue.Full:
            dead.append(q)
    if dead:
        with _sse_lock:
            for q in dead:
                if q in _sse_queues:
                    _sse_queues.remove(q)


def activity(text: str) -> None:
    """Record a short activity line. Safe to call from any thread."""
    if not text:
        return
    entry = {"ts": _now_iso(), "text": str(text)[:200]}
    with _ring_lock:
        _activity_ring.append(entry)
    _emit("activity", entry)


def status(task: str, started_at: Optional[float] = None) -> None:
    with _status_lock:
        _current_status["task"] = str(task) if task else "idle"
        _current_status["started_at"] = started_at or (time.time() if task and task != "idle" else None)
    _emit("status", dict(_current_status))


def feed(kind: str, text: str, telegram_deeplink: Optional[str] = None) -> None:
    if kind not in FEED_KINDS:
        kind = "note"
    item = {
        "id": f"ev_{int(time.time() * 1000):x}",
        "ts": _now_iso(),
        "kind": kind,
        "text": str(text)[:600],
        "unread": True,
        "telegram_deeplink": telegram_deeplink,
    }
    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FEED_PATH.open("a") as f:
        f.write(json.dumps(item) + "\n")
    _emit("feed", item)


def last_action() -> dict:
    with _ring_lock:
        return dict(_activity_ring[-1]) if _activity_ring else {"ts": None, "text": "booted"}


def current_status() -> dict:
    with _status_lock:
        return dict(_current_status)


def recent_activity(limit: int = 50) -> list[dict]:
    with _ring_lock:
        return list(list(_activity_ring)[-limit:])


def read_feed(since_iso: Optional[str] = None, limit: int = 100) -> list[dict]:
    if not FEED_PATH.exists():
        return []
    out: list[dict] = []
    for line in FEED_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since_iso and item.get("ts", "") <= since_iso:
            continue
        out.append(item)
    return out[-limit:]


def reading_queue() -> list[dict]:
    """Return the read-kind items, newest first, for phase 13."""
    items = [i for i in read_feed(limit=500) if i.get("kind") == "read"]
    items.reverse()
    return items


def register_sse_queue() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=128)
    with _sse_lock:
        _sse_queues.append(q)
    return q


def unregister_sse_queue(q: queue.Queue) -> None:
    with _sse_lock:
        if q in _sse_queues:
            _sse_queues.remove(q)


def _ticker_array() -> list[str]:
    from datetime import datetime, timezone

    items: list[str] = []
    # weather
    try:
        from browser_tools import research  # type: ignore

        w = research("weather in Boston today", depth="quick")
        summary = (w.summary or "").strip().replace("\n", " ")
        if summary:
            items.append(f"Weather: {summary[:60]}")
    except Exception:
        pass
    # next event
    try:
        from alfred_calendar import next_event  # type: ignore

        ev = next_event()
        if ev:
            items.append(f"Next: {ev.get('title', '?')}")
    except Exception:
        pass
    # overdue tasks
    try:
        from triage_todoist import get_overdue_tasks  # type: ignore

        n = len(get_overdue_tasks() or [])
        items.append(f"Overdue: {n} tasks")
    except Exception:
        pass
    items.append(f"Alfred: {current_status().get('task', 'idle')}")
    la = last_action()
    items.append(f"Last: {la.get('text', '-')[:58]}")
    return items


def dashboard_snapshot() -> dict:
    from datetime import datetime

    next_ev = None
    try:
        from alfred_calendar import next_event  # type: ignore

        ev = next_event()
        if ev:
            start = ev.get("start")
            next_ev = {
                "title": ev.get("title"),
                "start": str(start),
            }
    except Exception:
        pass

    overdue = 0
    try:
        from triage_todoist import get_overdue_tasks  # type: ignore

        overdue = len(get_overdue_tasks() or [])
    except Exception:
        pass

    ticker = _ticker_array()
    idx = int(time.time() // 60) % max(1, len(ticker))

    la = last_action()
    st = current_status()
    return {
        "now": _now_iso(),
        "next_event": next_ev,
        "overdue_count": overdue,
        "alfred_status": st.get("task", "idle"),
        "last_action": la.get("text"),
        "last_action_at": la.get("ts"),
        "ticker": ticker,
        "ticker_index": idx,
    }
