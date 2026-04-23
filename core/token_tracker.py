"""Token accounting + conservation-mode gate.

Every LLM call records a line to vault/memory/token-usage.jsonl. Daily
and weekly budgets live in config/token_quota.json. When either budget
crosses the conservation threshold, `conservation_mode()` flips true
and downstream code (run_claude, ingest, etc.) takes the cheaper path.

Fields per record:
    {ts, source, request_chars, response_chars, estimated_tokens}
source is one of claude_p, codex, embeddings.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
USAGE_PATH = Path(ALFRED_HOME) / "vault" / "memory" / "token-usage.jsonl"
QUOTA_PATH = Path(ALFRED_HOME) / "config" / "token_quota.json"
NOTIFIED_PATH = Path(ALFRED_HOME) / "data" / "token_conservation_notified.json"

DEFAULT_QUOTA = {
    "daily_tokens": 1_500_000,
    "weekly_tokens": 9_000_000,
    "conservation_threshold": 80,
}

_lock = threading.Lock()
_last_status = {"ts": 0.0, "value": None}


def _ensure_paths() -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not QUOTA_PATH.exists():
        QUOTA_PATH.write_text(json.dumps(DEFAULT_QUOTA, indent=2))


def _load_quota() -> dict:
    _ensure_paths()
    try:
        data = json.loads(QUOTA_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    out = dict(DEFAULT_QUOTA)
    out.update({k: v for k, v in data.items() if k in DEFAULT_QUOTA})
    return out


def record(source: str, request_chars: int, response_chars: int, estimated_tokens: Optional[int] = None) -> None:
    _ensure_paths()
    if estimated_tokens is None:
        estimated_tokens = max(0, (int(request_chars) + int(response_chars)) // 4)
    entry = {
        "ts": time.time(),
        "source": str(source),
        "request_chars": int(request_chars),
        "response_chars": int(response_chars),
        "estimated_tokens": int(estimated_tokens),
    }
    with _lock:
        with USAGE_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")


def _iter_entries_since(cutoff: float):
    if not USAGE_PATH.exists():
        return
    with USAGE_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("ts", 0) >= cutoff:
                yield e


def usage_window(hours: int) -> dict:
    cutoff = time.time() - hours * 3600
    totals = {"claude_p": 0, "codex": 0, "embeddings": 0, "other": 0}
    for e in _iter_entries_since(cutoff):
        src = e.get("source") or "other"
        bucket = src if src in totals else "other"
        totals[bucket] += int(e.get("estimated_tokens", 0))
    return totals


def quota_status() -> dict:
    quota = _load_quota()
    day_total = sum(usage_window(24).values())
    week_total = sum(usage_window(24 * 7).values())
    day_pct = round(100 * day_total / max(1, quota["daily_tokens"]), 1)
    week_pct = round(100 * week_total / max(1, quota["weekly_tokens"]), 1)
    threshold = quota.get("conservation_threshold", 80)
    return {
        "percent_used_day": day_pct,
        "percent_used_week": week_pct,
        "threshold_hit": day_pct >= threshold or week_pct >= threshold,
        "day_tokens": day_total,
        "week_tokens": week_total,
        "daily_budget": quota["daily_tokens"],
        "weekly_budget": quota["weekly_tokens"],
        "conservation_threshold": threshold,
    }


def conservation_mode() -> bool:
    now = time.time()
    if now - _last_status["ts"] < 60 and _last_status["value"] is not None:
        return bool(_last_status["value"])
    status = quota_status()
    _last_status["ts"] = now
    _last_status["value"] = bool(status["threshold_hit"])

    if _last_status["value"]:
        _maybe_notify_switch()
    return _last_status["value"]


def _maybe_notify_switch() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        data = json.loads(NOTIFIED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if data.get("last_notified") == today:
        return
    data["last_notified"] = today
    NOTIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTIFIED_PATH.write_text(json.dumps(data))
    try:
        from notify import push_telegram  # type: ignore

        push_telegram(
            "Running low on Claude capacity. Switching to efficient mode until reset.",
            priority="normal",
        )
    except Exception:
        pass


def reset_cache() -> None:
    _last_status["ts"] = 0.0
    _last_status["value"] = None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "fake", "conservation"])
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--chars", type=int, default=500)
    ns = ap.parse_args()

    if ns.cmd == "status":
        print(json.dumps(quota_status(), indent=2))
    elif ns.cmd == "fake":
        for _ in range(ns.count):
            record("claude_p", ns.chars, ns.chars)
        print("wrote", ns.count, "entries")
    elif ns.cmd == "conservation":
        reset_cache()
        print("conservation_mode:", conservation_mode())
