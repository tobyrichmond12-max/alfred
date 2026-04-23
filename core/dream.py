"""Alfred's dream mode. Overnight memory consolidation and next-day prep.

Runs 03:00 via cron. Three phases, all driven by `claude -p` subprocess so we
ride the Claude Max subscription instead of billing per token via the API.
Mirrors the pattern in reflect.py: one `call_claude` helper with a 30-second
retry, bounded timeout, plain JSON in/out.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import get_db, init_databases
from memory import store_memory
from config import DB_PATH, MEMORY_DB_PATH

ALFRED_HOME = "/mnt/nvme/alfred"
CLAUDE_BIN = "/home/thoth/.local/bin/claude"
CLAUDE_RETRY_DELAY_SECONDS = 30
CLAUDE_TIMEOUT_SECONDS = 240


def call_claude(prompt_text: str) -> str:
    """Run `claude -p` with one 30-second retry. Returns stdout stripped."""
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", prompt_text, "--output-format", "text"],
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT_SECONDS,
                cwd=ALFRED_HOME,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"claude rc={result.returncode} stderr={result.stderr!r}"
                )
            return result.stdout.strip()
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            last_error = e
            if attempt == 1:
                print(
                    f"call_claude failed (attempt 1/2), retrying in "
                    f"{CLAUDE_RETRY_DELAY_SECONDS}s: {e}",
                    file=sys.stderr,
                )
                time.sleep(CLAUDE_RETRY_DELAY_SECONDS)
                continue
            break
    raise RuntimeError(f"claude failed after retry: {last_error}")


def _extract_json_block(text: str, opener: str, closer: str):
    start = text.find(opener)
    end = text.rfind(closer) + 1
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None


def get_todays_conversations():
    """Get all conversations from today."""
    conn = get_db(DB_PATH)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT role, content, ts FROM conversations WHERE ts >= ? ORDER BY ts",
        (today,),
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"], "ts": r["ts"]} for r in rows]


def get_all_memories():
    """Get stored memories for reflection, newest first."""
    conn = get_db(MEMORY_DB_PATH)
    rows = conn.execute(
        "SELECT content, memory_type, importance, ts_created FROM memories "
        "ORDER BY ts_created DESC LIMIT 200"
    ).fetchall()
    conn.close()
    return [
        {
            "content": r["content"],
            "type": r["memory_type"],
            "importance": r["importance"],
            "created": r["ts_created"],
        }
        for r in rows
    ]


def consolidate_memories():
    """Extract key facts from today's conversations and store them."""
    convos = get_todays_conversations()
    if not convos:
        print("No conversations today to consolidate.")
        return []

    convo_text = "\n".join(
        f"[{c['ts']}] {c['role']}: {c['content']}" for c in convos
    )

    prompt = (
        "You are Alfred processing the day's conversations into long-term memory.\n\n"
        "Extract key facts, commitments, preferences, and relationship information "
        "from the conversations below. Return a JSON array where each item has:\n"
        '- "fact": the key information\n'
        '- "type": one of "commitment", "goal", "preference", "relationship", '
        '"decision", "event", "observation"\n'
        '- "importance": 0.0 to 1.0\n\n'
        "Only extract genuinely important or useful information. Skip small talk "
        "and pleasantries. Output only the JSON array, no preamble or commentary.\n\n"
        f"Conversations:\n{convo_text}"
    )

    text = call_claude(prompt)
    facts = _extract_json_block(text, "[", "]") or []

    stored = []
    for fact in facts:
        if fact.get("fact") and fact.get("importance", 0) > 0.3:
            store_memory(
                content=fact["fact"],
                memory_type=fact.get("type", "observation"),
                importance=fact.get("importance", 0.5),
            )
            stored.append(fact)
            print(f"  Stored: [{fact.get('type')}] {fact['fact'][:80]}")

    return stored


def generate_reflections():
    """Generate insights by reflecting on accumulated memories."""
    memories = get_all_memories()
    if len(memories) < 5:
        print("Not enough memories for reflection yet.")
        return []

    mem_text = "\n".join(
        f"[{m['type']}, importance={m['importance']}] {m['content']}"
        for m in memories[:100]
    )

    prompt = (
        "You are Alfred reflecting overnight on what you know about the user.\n\n"
        "Based on the memories below, generate 3 to 5 reflective insights. Look for "
        "patterns, connections, potential issues, or opportunities. Return a JSON "
        "array where each item has:\n"
        '- "insight": the reflection\n'
        '- "type": one of "pattern", "concern", "opportunity", "connection"\n'
        '- "importance": 0.0 to 1.0\n\n'
        "Output only the JSON array, no preamble or commentary.\n\n"
        f"Memories:\n{mem_text}"
    )

    text = call_claude(prompt)
    insights = _extract_json_block(text, "[", "]") or []

    for insight in insights:
        if insight.get("insight"):
            store_memory(
                content=f"[Reflection] {insight['insight']}",
                memory_type="reflection",
                importance=insight.get("importance", 0.6),
            )
            print(f"  Reflection: {insight['insight'][:80]}")

    return insights


def generate_morning_briefing():
    """Generate tomorrow's morning briefing and refresh the core profile."""
    memories = get_all_memories()
    mem_text = "\n".join(f"- [{m['type']}] {m['content']}" for m in memories[:50])

    now = datetime.utcnow()
    tomorrow = (now + timedelta(days=1)).strftime("%A, %B %d, %Y")

    prompt = (
        "You are Alfred preparing tomorrow's opener for the user.\n\n"
        "Based on the memories below about the user, produce two things:\n"
        f"1. A morning briefing for {tomorrow}, 2 to 3 sentences covering what is "
        "important for the day.\n"
        "2. An updated core profile, a concise paragraph summarizing who the user is, "
        "his goals, current projects, key relationships, and preferences.\n\n"
        'Return JSON with keys "briefing" and "core_profile". Output only the JSON '
        "object, no preamble or commentary.\n\n"
        f"Memories:\n{mem_text}"
    )

    text = call_claude(prompt)
    result = _extract_json_block(text, "{", "}") or {}

    if result.get("core_profile"):
        conn = get_db(DB_PATH)
        conn.execute(
            "UPDATE core_profile SET content = ?, ts_updated = datetime('now') "
            "WHERE id = 1",
            (result["core_profile"],),
        )
        conn.commit()
        conn.close()
        print("  Core profile updated")

    if result.get("briefing"):
        store_memory(
            content=f"[Morning Briefing for {tomorrow}] {result['briefing']}",
            memory_type="briefing",
            importance=0.9,
        )
        print(f"  Briefing: {result['briefing'][:100]}")

    return result


def run_dream_mode():
    print(f"\n{'=' * 50}")
    print(f"ALFRED DREAM MODE, {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 50}\n")

    print("Phase 1: Memory consolidation...")
    facts = consolidate_memories()
    print(f"  Extracted {len(facts)} facts\n")

    print("Phase 2: Reflections...")
    insights = generate_reflections()
    print(f"  Generated {len(insights)} insights\n")

    print("Phase 3: Morning briefing and profile update...")
    generate_morning_briefing()
    print("  Done\n")

    print(f"{'=' * 50}")
    print("Dream mode complete. Good night, sir.")
    print(f"{'=' * 50}\n")


if __name__ == "__main__":
    init_databases()
    run_dream_mode()
