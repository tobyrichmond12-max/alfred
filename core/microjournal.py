"""Midday micro check-in.

Cron at 13:00 calls fire_checkin(). It pushes a Telegram question and
opens a 30-minute response window. handle_reply (invoked from the bot
router before other session handlers) consumes the next message,
extracts mood keywords, logs the check-in, and clears the pending state.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

CONFIG_PATH = Path(ALFRED_HOME) / "data" / "checkin_config.json"
PENDING_PATH = Path("/var/lib/alfred/checkin_pending.json")
if not os.access(PENDING_PATH.parent, os.W_OK):
    PENDING_PATH = Path(ALFRED_HOME) / "data" / "checkin_pending.json"
PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)

CHECKINS_PATH = Path(ALFRED_HOME) / "vault" / "memory" / "checkins.jsonl"
JOURNAL_DIR = Path(ALFRED_HOME) / "vault" / "journal"

DEFAULT_CONFIG = {
    "enabled": True,
    "times": ["13:00"],
    "timeout_min": 30,
}

KEYWORD_RE = re.compile(r"\b(tired|stressed|good|productive|bored|focused|scattered|anxious|calm|energized|drained|meh)\b", re.IGNORECASE)


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    return cfg


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def pending_exists() -> bool:
    if not PENDING_PATH.exists():
        return False
    try:
        data = json.loads(PENDING_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    exp = data.get("expires_at")
    if not exp:
        return False
    try:
        exp_dt = datetime.fromisoformat(exp)
    except ValueError:
        return False
    return datetime.now() < exp_dt


def fire_checkin() -> Optional[str]:
    cfg = _load_config()
    if not cfg.get("enabled", True):
        return None
    timeout = int(cfg.get("timeout_min", 30))
    started = datetime.now()
    pending = {
        "started_at": started.isoformat(timespec="seconds"),
        "expires_at": (started + timedelta(minutes=timeout)).isoformat(timespec="seconds"),
    }
    PENDING_PATH.write_text(json.dumps(pending, indent=2))
    try:
        from notify import push_telegram  # type: ignore

        push_telegram("Quick midday check-in. How is today going so far?", priority="low")
    except Exception:
        pass
    return pending["expires_at"]


def skip_if_stale() -> None:
    if not PENDING_PATH.exists():
        return
    try:
        data = json.loads(PENDING_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        try:
            PENDING_PATH.unlink()
        except OSError:
            pass
        return
    try:
        exp = datetime.fromisoformat(data.get("expires_at", ""))
    except ValueError:
        exp = datetime.now()
    if datetime.now() >= exp:
        try:
            PENDING_PATH.unlink()
        except OSError:
            pass


def _extract_keywords(text: str) -> list[str]:
    return list({m.group(1).lower() for m in KEYWORD_RE.finditer(text)})


def handle_reply(text: str) -> Optional[str]:
    if not pending_exists():
        return None
    text = (text or "").strip()
    if not text:
        return None
    now = datetime.now()
    stamp = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    journal_path = JOURNAL_DIR / f"{today}.md"
    if not journal_path.exists():
        journal_path.write_text(f"# Journal {today}\n\n")
    with journal_path.open("a") as f:
        f.write(f"## Midday check-in {stamp}\n\n{text}\n\n")

    CHECKINS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CHECKINS_PATH.open("a") as f:
        f.write(json.dumps({
            "ts": _now_iso(),
            "text": text,
            "keywords": _extract_keywords(text),
        }) + "\n")

    try:
        PENDING_PATH.unlink()
    except OSError:
        pass
    return "Logged. Back to it."


def set_enabled(flag: bool) -> None:
    cfg = _load_config()
    cfg["enabled"] = bool(flag)
    _save_config(cfg)


def set_times(times: list[str]) -> None:
    cfg = _load_config()
    cfg["times"] = list(times)
    _save_config(cfg)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest", "fire", "skip"])
    ns = ap.parse_args()

    if ns.cmd == "selftest":
        # seed pending manually
        pending = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "expires_at": (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds"),
        }
        PENDING_PATH.write_text(json.dumps(pending, indent=2))
        reply = handle_reply("a bit tired but productive")
        ok = reply and CHECKINS_PATH.exists()
        count = 0
        if CHECKINS_PATH.exists():
            count = sum(1 for _ in CHECKINS_PATH.read_text().splitlines() if _.strip())
        print(f"Microjournal self-test: {count} checkin logged, keywords extracted")
        raise SystemExit(0 if ok else 1)
    elif ns.cmd == "fire":
        print(fire_checkin())
    elif ns.cmd == "skip":
        skip_if_stale()
        print("ok")
