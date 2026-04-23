"""End-of-day synthesis pass that writes to the journal's Alfred's Notes.

Runs once a day via cron (22:30 local). Reads today's reflection bullets and
conversation surface from the journal, asks Claude for a 2-3 sentence
synthesis written in Alfred's voice, and splices it as a "## Day summary"
block at the top of the "Alfred's Notes" section. The raw reflection
bullets stay intact below, so nothing is lost.

Manual run:
    python3 core/day_summary.py            # today
    python3 core/day_summary.py 2026-04-21 # explicit date
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

ALFRED_HOME = "/mnt/nvme/alfred"
VAULT_DIR = os.path.join(ALFRED_HOME, "vault")
CLAUDE_BIN = "/home/thoth/.local/bin/claude"
EASTERN = ZoneInfo("America/New_York")
CLAUDE_RETRY_DELAY_SECONDS = 30

SYNTHESIS_PROMPT_HEADER = """You are Alfred, the user's cognitive operating system, writing the end-of-day wrap for his journal.

The material below is today's accumulated reflection bullets and voice conversation surface. Your job is to produce 2 or 3 sentences that capture the durable shape of the day: what actually happened, what moved, what slipped, and anything a future Alfred would want to remember. Skip the play-by-play.

Rules:
- Plain prose, no bullet points, no markdown headers, no em dashes.
- Voice-first register: contractions, casual but sharp, no preamble, no wrap-up.
- 2 or 3 sentences. Never more than 4.
- If the day was genuinely uneventful, say so in one sentence.
- Speak about the user in the third person (he, the user), not second person.
- Never mention "today" or "the day" explicitly more than once, keep it tight.
- No AI disclaimers.

Material:
"""

SECTION_HEADING = "## Day summary"


def _journal_path(target: date) -> str:
    return os.path.join(
        VAULT_DIR, "journal",
        target.strftime("%Y"),
        target.strftime("%m"),
        f"{target.strftime('%Y-%m-%d')}.md",
    )


def _extract_alfreds_notes(journal_text: str) -> str:
    m = re.search(
        r"## Alfred's Notes\n(.*?)(?=\n---\n|\n## |\Z)",
        journal_text,
        flags=re.DOTALL,
    )
    return m.group(1).strip() if m else ""


def _extract_conversations(journal_text: str) -> str:
    m = re.search(
        r"## Conversations\n(.*?)(?=\n## |\Z)",
        journal_text,
        flags=re.DOTALL,
    )
    if not m:
        return ""
    body = m.group(1).strip()
    if body.startswith("_None yet_") or not body:
        return ""
    return body


def _call_claude(prompt_text: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", prompt_text, "--output-format", "text"],
                capture_output=True,
                text=True,
                timeout=240,
                cwd=ALFRED_HOME,
                env=env,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"claude rc={result.returncode} stderr={result.stderr[-400:]!r}"
                )
            out = result.stdout.strip()
            if not out:
                raise RuntimeError("claude returned empty synthesis")
            return out
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            last_error = e
            if attempt == 1:
                print(
                    f"day_summary: claude failed (attempt 1/2), "
                    f"retrying in {CLAUDE_RETRY_DELAY_SECONDS}s: {e}",
                    file=sys.stderr,
                )
                time.sleep(CLAUDE_RETRY_DELAY_SECONDS)
    raise RuntimeError(f"day_summary: claude failed after retry: {last_error}")


def _splice_summary(journal_text: str, summary: str, now: datetime) -> str:
    """Insert or replace a '## Day summary' block right above the bullets."""
    generated_line = f"*synthesized at {now.strftime('%-I:%M %p')}*"
    block = (
        f"{SECTION_HEADING}\n\n"
        f"{summary}\n\n"
        f"{generated_line}\n\n"
    )

    existing_summary = re.compile(
        r"## Day summary\n.*?(?=\n## |\Z)",
        flags=re.DOTALL,
    )

    def _inject(notes_match: re.Match) -> str:
        body = notes_match.group(1)
        body_stripped = body.lstrip("\n")
        placeholder = "_Observations and things worth remembering from today_"
        if body_stripped.startswith(placeholder):
            body_stripped = body_stripped[len(placeholder):].lstrip("\n")
        # Drop any existing Day summary block; we replace it.
        body_stripped = existing_summary.sub("", body_stripped).strip()
        combined = block + body_stripped
        return f"## Alfred's Notes\n{combined}\n"

    return re.sub(
        r"## Alfred's Notes\n(.*?)(?=\n---\n|\n## |\Z)",
        _inject,
        journal_text,
        count=1,
        flags=re.DOTALL,
    )


def synthesize(target: date | None = None) -> str | None:
    """Generate (or regenerate) today's Day summary in the journal file.

    Returns the journal path on success, None if the journal is missing.
    """
    now = datetime.now(EASTERN)
    if target is None:
        target = now.date()

    path = _journal_path(target)
    if not os.path.exists(path):
        print(f"day_summary: journal not found at {path}", file=sys.stderr)
        return None

    with open(path) as f:
        journal_text = f.read()

    notes = _extract_alfreds_notes(journal_text)
    convs = _extract_conversations(journal_text)
    body_parts: list[str] = []
    if notes:
        body_parts.append("### Reflection bullets accumulated today\n" + notes)
    if convs:
        body_parts.append("### Conversations logged today\n" + convs)
    if not body_parts:
        print("day_summary: nothing to synthesize (empty notes, empty conversations)")
        return path

    prompt = SYNTHESIS_PROMPT_HEADER + "\n\n".join(body_parts)
    summary = _call_claude(prompt)

    updated = _splice_summary(journal_text, summary, now)
    with open(path, "w") as f:
        f.write(updated)
    print(f"day_summary: wrote synthesis to {path}")
    return path


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    target = None
    if args:
        target = date.fromisoformat(args[0])
    try:
        synthesize(target)
    except Exception as e:
        print(f"day_summary error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
