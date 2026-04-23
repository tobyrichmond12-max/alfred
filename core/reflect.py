"""Reflective cycle. Runs every 3 hours via cron.

Reads current_state.json plus the last 3 hours of voice conversations, hands
the combined context to `claude -p`, and asks Alfred to surface anything the user
should know about. The output is written to vault/reflections/ and a short
pointer + bullets is appended to today's journal under Alfred's Notes.

Manual run: python3 reflect.py
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ALFRED_HOME = "/mnt/nvme/alfred"
VAULT_DIR = os.path.join(ALFRED_HOME, "vault")
STATE_FILE = os.path.join(ALFRED_HOME, "current_state.json")
REFLECTIONS_DIR = os.path.join(VAULT_DIR, "reflections")
CLAUDE_BIN = "/home/thoth/.local/bin/claude"
EASTERN = ZoneInfo("America/New_York")
WINDOW_HOURS = 3
CLAUDE_RETRY_DELAY_SECONDS = 30

URGENT_PATTERN = re.compile(
    r"\b(in\s+\d+\s+min|starts\s+(at|in)|overdue|conflict|overlap|"
    r"deadline|due\s+(today|tomorrow))",
    re.IGNORECASE,
)
NOTHING_FLAGGED = "Nothing flagged."

PROMPT_HEADER = """You are Alfred, the user's personal cognitive operating system.

Review the last 3 hours of the user's context below: his current state (calendar, tasks, location, biometrics) and any voice conversations from this window.

Identify:
- Calendar conflicts or tight transitions coming up
- Overdue tasks that need attention today
- Things the user mentioned but hasn't acted on
- Patterns worth flagging (skipped meals, long sedentary stretches, commitments slipping)
- Repeated asks: the same kind of request the user has made 3+ times in the past week (e.g. "add to calendar", "summarize this thread", "draft a reply to X"). Cross-reference vault/reflections/skill-candidates.md for prior counts. If a pattern crosses the 3/week threshold, append a new entry to that file with the pattern description and a one-line sketch of what a skill for it would do. Skip if the pattern is already listed there.
- Anything else a sharp chief of staff would surface

Output: a short bulleted list of observations, markdown `-` bullets. No preamble, no summary, no headers. Each bullet must be actionable or informative, nothing generic. Never use em dashes or en dashes in any form. Use commas, periods, or restructure. If there is genuinely nothing worth flagging, output the single line `Nothing flagged.` and stop.

---

"""


def load_state():
    with open(STATE_FILE) as f:
        return json.load(f)


def recent_conversations(now):
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    conv_root = os.path.join(VAULT_DIR, "conversations")
    results = []
    for day in (now, now - timedelta(days=1)):
        conv_dir = os.path.join(conv_root, f"{day.year:04d}", f"{day.month:02d}")
        if not os.path.isdir(conv_dir):
            continue
        for fname in sorted(os.listdir(conv_dir)):
            if not fname.endswith(".md") or fname.endswith("-summary.md"):
                continue
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})\.md$", fname)
            if not m:
                continue
            y, mo, d, hh, mm, ss = map(int, m.groups())
            try:
                ts = datetime(y, mo, d, hh, mm, ss, tzinfo=EASTERN)
            except ValueError:
                continue
            if ts < cutoff or ts > now:
                continue
            with open(os.path.join(conv_dir, fname)) as f:
                results.append((ts, fname, f.read()))
    results.sort()
    return results


def build_prompt(state, convs, now):
    parts = [PROMPT_HEADER]
    parts.append(f"## Right now\n{now.strftime('%A %B %d %Y, %-I:%M %p %Z')}\n\n")
    parts.append("## current_state.json\n```json\n")
    parts.append(json.dumps(state, indent=2))
    parts.append("\n```\n\n")
    parts.append(f"## Voice conversations in the last {WINDOW_HOURS} hours ({len(convs)})\n")
    if not convs:
        parts.append("_No voice conversations in this window._\n")
    else:
        for _, fname, content in convs:
            parts.append(f"\n### {fname}\n{content}\n")
    return "".join(parts)


def call_claude(prompt_text):
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", prompt_text, "--output-format", "text"],
                capture_output=True,
                text=True,
                timeout=240,
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


def save_reflection(now, observations):
    os.makedirs(REFLECTIONS_DIR, exist_ok=True)
    fname = now.strftime("%Y-%m-%d-%H%M") + ".md"
    path = os.path.join(REFLECTIONS_DIR, fname)
    body = (
        "---\n"
        f"date: {now.strftime('%Y-%m-%d')}\n"
        f"time: \"{now.strftime('%H:%M')}\"\n"
        "tags: [reflection]\n"
        "---\n\n"
        f"# Reflection, {now.strftime('%A %B %d, %-I:%M %p')}\n\n"
        f"{observations}\n"
    )
    with open(path, "w") as f:
        f.write(body)
    return path


def append_to_journal(now, observations, reflection_path):
    date_str = now.strftime("%Y-%m-%d")
    journal_path = os.path.join(
        VAULT_DIR, "journal", f"{now.year:04d}", f"{now.month:02d}", f"{date_str}.md"
    )
    if not os.path.exists(journal_path):
        return
    with open(journal_path) as f:
        content = f.read()
    rel = os.path.relpath(reflection_path, VAULT_DIR)[:-3]
    time_label = now.strftime("%-I:%M %p")
    entry = f"\n**{time_label}** [[{rel}|reflection]]\n\n{observations}\n"
    marker = "\n---\n*Generated by Alfred"
    if marker in content:
        content = content.replace(marker, entry + marker, 1)
    else:
        content = content.rstrip() + "\n" + entry
    with open(journal_path, "w") as f:
        f.write(content)


def push_if_warranted(observations: str) -> str:
    """Classify observations and push. Returns the delivery tag used: urgent, routine, or none.

    Sends via both ntfy and Web Push so ntfy stays as a fallback during the
    PWA push rollout. Web Push targets all stored PWA subscriptions.
    """
    stripped = observations.strip()
    if not stripped or stripped == NOTHING_FLAGGED:
        return "none"
    try:
        from notify import push_telegram
    except ImportError:
        return "none"
    if URGENT_PATTERN.search(stripped):
        push_telegram(stripped, priority="high")
        return "urgent"
    push_telegram(stripped, priority="low")
    return "routine"


def main():
    now = datetime.now(EASTERN)
    state = load_state()
    convs = recent_conversations(now)
    prompt = build_prompt(state, convs, now)
    observations = call_claude(prompt)
    path = save_reflection(now, observations)
    append_to_journal(now, observations, path)
    delivery = push_if_warranted(observations)
    print(
        f"[{now.isoformat(timespec='seconds')}] reflection saved: {path} "
        f"({len(convs)} convs, push={delivery})"
    )


if __name__ == "__main__":
    main()
