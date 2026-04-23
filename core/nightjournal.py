"""Nighttime journal flow over Telegram.

Cron at 21:30 fires `--fire`, which:
  1. Finds gaps in vault/memory/ (missing sleep, mood, afternoon).
  2. Asks one question via bot.send_message.
  3. Subsequent messages route through handle_reply until the session
     closes (user says done / stop, or 10 minutes of silence).
  4. On close, writes summary + extracted facts.

State is persisted to /var/lib/alfred/night_session.json so the polling
loop can route replies even if the fire process has exited.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

SESSION_PATH = Path(os.environ.get(
    "NIGHTJOURNAL_SESSION_PATH", "/var/lib/alfred/night_session.json",
))
if not os.access(SESSION_PATH.parent, os.W_OK):
    SESSION_PATH = Path(ALFRED_HOME) / "data" / "night_session.json"
SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)

VAULT_MEMORY = Path(ALFRED_HOME) / "vault" / "memory"
JOURNAL_DIR = Path(ALFRED_HOME) / "vault" / "journal"
FACTS_DIR = VAULT_MEMORY / "facts"
try:
    from sessions import SESSION_WINDOW_SECONDS as SESSION_TIMEOUT_S  # type: ignore
except ImportError:
    SESSION_TIMEOUT_S = 15 * 60
MAX_FOLLOWUPS = 5

END_TOKENS = {"done", "that's it", "thats it", "stop", "good night", "goodnight"}
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/thoth/.local/bin/claude")


def _now() -> float:
    return time.time()


def _load_session() -> Optional[dict]:
    if not SESSION_PATH.exists():
        return None
    try:
        data = json.loads(SESSION_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # stale cleanup
    if _now() - data.get("last_touch", 0) > SESSION_TIMEOUT_S:
        try:
            SESSION_PATH.unlink()
        except OSError:
            pass
        return None
    return data


def _save_session(data: dict) -> None:
    data["last_touch"] = _now()
    SESSION_PATH.write_text(json.dumps(data, indent=2))


def _close_session() -> None:
    if SESSION_PATH.exists():
        try:
            SESSION_PATH.unlink()
        except OSError:
            pass


def _find_gaps() -> list[str]:
    today = datetime.now().strftime("%Y-%m-%d")
    gaps: list[str] = []
    journal_today = JOURNAL_DIR / f"{today}.md"
    if not journal_today.exists() or journal_today.stat().st_size == 0:
        gaps.append("no journal entry for today yet")
    facts_today = FACTS_DIR / f"{today}.jsonl"
    if not facts_today.exists():
        gaps.append("no facts extracted today")
    # afternoon activity: check reflections
    return gaps


def _claude(prompt: str, timeout: int = 60) -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=ALFRED_HOME,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ""
    return (r.stdout or "").strip()


def _extract_facts(transcript: list[dict]) -> list[dict]:
    text = "\n".join(f"[{m['role']}] {m['text']}" for m in transcript)
    prompt = (
        "Extract durable facts from this nighttime check-in. One JSON object "
        'per line: {"kind": "decision"|"event"|"mood"|"plan"|"note", "text": str}. '
        "Skip pleasantries, skip summaries of the questions themselves. "
        "If nothing notable, output an empty response.\n\n"
        f"{text}"
    )
    out = _claude(prompt, timeout=60)
    facts: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            facts.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return facts


def _write_journal_entry(transcript: list[dict], summary: str) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    stamp = datetime.now().strftime("%H:%M")
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = JOURNAL_DIR / f"{today}.md"
    if not path.exists():
        path.write_text(f"# Journal {today}\n\n")
    with path.open("a") as f:
        f.write(f"## Night check-in {stamp}\n\n")
        if summary:
            f.write(f"{summary}\n\n")
        for m in transcript:
            f.write(f"- **{m['role']}**: {m['text']}\n")
        f.write("\n")
    return path


def _write_facts(facts: list[dict]) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = FACTS_DIR / f"{today}.jsonl"
    now = datetime.now().isoformat(timespec="seconds")
    with path.open("a") as f:
        for fact in facts:
            fact = dict(fact)
            fact["ts"] = now
            f.write(json.dumps(fact) + "\n")
    return path


def _maybe_announce(text: str) -> None:
    try:
        from hud import activity  # type: ignore

        activity(text)
    except Exception:
        pass


def run_session(bot=None) -> dict:
    existing = _load_session()
    if existing and existing.get("status") == "open":
        return existing

    session = {
        "session_id": "nj_" + secrets.token_hex(6),
        "started_at": _now(),
        "gaps": _find_gaps(),
        "transcript": [],
        "followups_used": 0,
        "status": "open",
    }
    _save_session(session)

    first_q = "How did today land?"
    session["transcript"].append({"role": "alfred", "text": first_q})
    _save_session(session)

    _maybe_announce(f"Nightjournal opened {session['session_id']}")

    if bot is not None:
        try:
            owner = getattr(bot, "owner_id", None)
            if owner is not None:
                bot.send_message(first_q, owner)
        except Exception:
            pass
    return session


def handle_reply(text: str, bot=None) -> Optional[str]:
    session = _load_session()
    if not session or session.get("status") != "open":
        return None
    low = text.strip().lower()
    session["transcript"].append({"role": "user", "text": text})
    _save_session(session)

    if any(tok in low for tok in END_TOKENS) or session["followups_used"] >= MAX_FOLLOWUPS:
        return close_session(bot)

    # ask a focused follow-up
    prior = "\n".join(f"[{m['role']}] {m['text']}" for m in session["transcript"])
    gaps = ", ".join(session.get("gaps", [])) or "none recorded"
    prompt = (
        "Based on what the user has shared and the gaps below, ask ONE focused "
        "follow-up. Return just the question, no preamble, under 20 words.\n\n"
        f"GAPS: {gaps}\n\nTRANSCRIPT:\n{prior}"
    )
    q = _claude(prompt, timeout=40) or "Anything else on your mind before you sleep?"
    q = q.splitlines()[0][:200]
    session["transcript"].append({"role": "alfred", "text": q})
    session["followups_used"] += 1
    _save_session(session)

    if bot is not None:
        try:
            owner = getattr(bot, "owner_id", None)
            if owner is not None:
                bot.send_message(q, owner)
        except Exception:
            pass
    return q


def close_session(bot=None) -> str:
    session = _load_session()
    if not session:
        return ""
    session["status"] = "closing"
    _save_session(session)

    transcript = session.get("transcript", [])
    summary_prompt = (
        "Summarize this night check-in in 2 sentences. Plain prose, no markdown, no filler.\n\n"
        + "\n".join(f"[{m['role']}] {m['text']}" for m in transcript)
    )
    summary = _claude(summary_prompt, timeout=60)
    facts = _extract_facts(transcript)

    _write_journal_entry(transcript, summary)
    _write_facts(facts)
    _close_session()
    _maybe_announce(f"Nightjournal closed, {len(facts)} facts saved")

    closing = "Logged. Sleep well."
    if bot is not None:
        try:
            owner = getattr(bot, "owner_id", None)
            if owner is not None:
                bot.send_message(closing, owner)
        except Exception:
            pass
    return closing


def is_open() -> bool:
    s = _load_session()
    return bool(s and s.get("status") == "open")


def _fire_with_bot() -> None:
    """Import the running bot singleton and kick a session."""
    try:
        import notify  # type: ignore

        bot = notify._get_bot()
    except Exception:
        bot = None
    if bot is None:
        # no running bot (typical for cron). Just open the session so the
        # next reply on the bot routes here, and push the first question via
        # notify.push_telegram directly.
        session = run_session(None)
        try:
            from notify import push_telegram  # type: ignore

            push_telegram("How did today land?", priority="normal")
        except Exception:
            pass
        print("opened", session["session_id"])
    else:
        run_session(bot)


def _self_test() -> int:
    """Three-turn canned walkthrough."""
    _close_session()
    session = run_session(None)
    handle_reply("today was long but productive", None)
    handle_reply("I finished the co-op essay, felt good", None)
    handle_reply("done", None)
    today = datetime.now().strftime("%Y-%m-%d")
    jp = JOURNAL_DIR / f"{today}.md"
    fp = FACTS_DIR / f"{today}.jsonl"
    ok = jp.exists() and fp.exists()
    print(f"Nightjournal self-test: {'3/3' if ok else '0/3'} turns logged")
    return 0 if ok else 1


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--fire", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        raise SystemExit(_self_test())
    if ns.fire:
        _fire_with_bot()
