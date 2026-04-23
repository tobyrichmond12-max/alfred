"""Brain dump parser. Stub implementation for Phase 1.

The real Phase 12/25/etc. flows may expand this. The contract with the
Telegram bot is:

    parse(text)                     -> dict with dump_id, summary_text,
                                       tasks, events, notes, decisions
    commit(dump_id, accept_all)     -> human-readable result string
    cancel(dump_id)                 -> None

For now this parses nothing: it stores the raw text and returns a
summary that tells the user the parser is not wired yet. The bot still
runs through its review/commit loop cleanly.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

ALFRED_HOME = "/mnt/nvme/alfred"
STORE_DIR = Path("/var/lib/alfred/braindumps")


def _store():
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    return STORE_DIR


def parse(text: str) -> dict:
    dump_id = "bd_" + secrets.token_hex(6)
    path = _store() / f"{dump_id}.json"
    record = {
        "dump_id": dump_id,
        "created_at": time.time(),
        "raw_text": text,
        "tasks": [],
        "events": [],
        "notes": [{"text": text, "ts": time.time()}],
        "decisions": [],
    }
    path.write_text(json.dumps(record, indent=2))
    summary_text = (
        f"Captured {len(text.splitlines()) or 1} lines. "
        "Parser is in passthrough mode for now, full extraction lands later. "
        "Confirm All to save this raw to your journal, Cancel to drop it."
    )
    record["summary_text"] = summary_text
    path.write_text(json.dumps(record, indent=2))
    return {
        "dump_id": dump_id,
        "summary_text": summary_text,
        "tasks": [],
        "events": [],
        "notes": record["notes"],
        "decisions": [],
    }


def commit(dump_id: str, accept_all: bool = True) -> str:
    path = _store() / f"{dump_id}.json"
    if not path.exists():
        return "Nothing to commit."
    data = json.loads(path.read_text())
    # Append raw note to today's journal
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    journal_dir = Path(ALFRED_HOME) / "vault" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal = journal_dir / f"{today}.md"
    body = data.get("raw_text", "")
    stamp = datetime.now().strftime("%H:%M")
    with journal.open("a") as f:
        if journal.stat().st_size == 0:
            f.write(f"# Journal {today}\n\n")
        f.write(f"## Brain dump {stamp}\n\n{body}\n\n")
    path.unlink()
    return f"Saved brain dump to {journal.name}."


def cancel(dump_id: str) -> None:
    path = _store() / f"{dump_id}.json"
    if path.exists():
        path.unlink()
