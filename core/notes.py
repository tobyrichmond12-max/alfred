"""Note-taking flow. /note start opens a mode where every text/voice
message appends a timestamped line to vault/notes/<date>.md until
/note stop. finalize runs a claude -p cleanup pass.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

NOTES_DIR = Path(ALFRED_HOME) / "vault" / "notes"
RAW_DIR = NOTES_DIR / ".raw"
STATE_PATH = Path("/var/lib/alfred/note_state.json")
if not os.access(STATE_PATH.parent, os.W_OK):
    STATE_PATH = Path(ALFRED_HOME) / "data" / "note_state.json"
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/thoth/.local/bin/claude")
_lock = threading.Lock()


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(data: dict) -> None:
    STATE_PATH.write_text(json.dumps(data, indent=2))


def active_chats() -> list[int]:
    return [int(k) for k, v in _load_state().items() if v.get("active")]


def is_active(chat_id: int) -> bool:
    return bool(_load_state().get(str(chat_id), {}).get("active"))


def start(chat_id: int) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    state = _load_state()
    state[str(chat_id)] = {"active": True, "date": today}
    _save_state(state)
    return today


def stop(chat_id: int) -> dict:
    state = _load_state()
    entry = state.pop(str(chat_id), None)
    _save_state(state)
    if not entry:
        return {}
    date = entry.get("date")
    if not date:
        return {}
    _rag_ingest_bg(date)
    return {
        "path": str(NOTES_DIR / f"{date}.md"),
        "cleanup_delta": finalize(date),
    }


def append(date: str, text: str) -> Path:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = NOTES_DIR / f"{date}.md"
    if not path.exists():
        path.write_text(f"# Notes {date}\n\n")
    stamp = datetime.now().strftime("%H:%M")
    with _lock:
        with path.open("a") as f:
            f.write(f"- [{stamp}] {text}\n")
    _rag_ingest_bg(date)
    return path


def _backup_raw(date: str) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    src = NOTES_DIR / f"{date}.md"
    if src.exists():
        shutil.copy2(src, RAW_DIR / f"{date}.md")


def finalize(date: str) -> int:
    """Run a claude cleanup pass. Returns chars delta (cleanup - raw)."""
    path = NOTES_DIR / f"{date}.md"
    if not path.exists():
        return 0
    raw = path.read_text()
    _backup_raw(date)
    prompt = (
        "You are cleaning up dictated notes. Keep everything the user said. "
        "Add H2 headers where topics change. Fix obvious dictation artifacts "
        "(uh, um, repeated words, you-know). Do not rewrite meaning. Return "
        "the cleaned markdown only, no preamble.\n\n" + raw
    )
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p"],
            input=prompt,
            capture_output=True, text=True, timeout=90,
            cwd=ALFRED_HOME, env=env,
        )
        cleaned = (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 0
    if cleaned:
        path.write_text(cleaned + "\n")
    return len(cleaned) - len(raw)


def _rag_ingest_bg(date: str) -> None:
    """Debounced background RAG ingest so new note lines are queryable."""
    path = NOTES_DIR / f"{date}.md"
    if not path.exists():
        return
    # simple debounce: check last-ingest time in module-level dict
    key = str(path)
    now_ts = os.path.getmtime(path)
    last = _rag_ingest_bg._last.get(key, 0)  # type: ignore
    if now_ts - last < 30:
        return
    _rag_ingest_bg._last[key] = now_ts  # type: ignore

    def _run():
        try:
            import rag  # type: ignore

            rag.ingest_document(str(path))
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


_rag_ingest_bg._last = {}  # type: ignore


def maybe_capture(chat_id: int, text: str) -> Optional[str]:
    """Router hook from main_telegram.on_text. Returns an ack if consumed."""
    state = _load_state()
    entry = state.get(str(chat_id))
    if not entry or not entry.get("active"):
        return None
    date = entry.get("date") or datetime.now().strftime("%Y-%m-%d")
    path = append(date, text)
    line_count = sum(1 for _ in path.read_text().splitlines() if _.startswith("- ["))
    return f"Noted (line {line_count})"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest"])
    ns = ap.parse_args()
    if ns.cmd == "selftest":
        start(42)
        for line in ("today's plan", "finish the resume edits", "sleep by 11"):
            maybe_capture(42, line)
        today = datetime.now().strftime("%Y-%m-%d")
        raw_lines = 0
        try:
            raw_lines = sum(1 for _ in (NOTES_DIR / f"{today}.md").read_text().splitlines() if _.startswith("- ["))
        except FileNotFoundError:
            pass
        info = stop(42)
        delta = info.get("cleanup_delta", 0)
        print(f"Notes self-test: {raw_lines} lines captured, cleanup delta {delta}")
        raise SystemExit(0 if raw_lines == 3 else 1)
