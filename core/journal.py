"""Generates and updates daily journal notes in the Obsidian vault.

Runs at midnight via cron to create the next day's note and finalize today's.
Manual regeneration always overwrites: python3 journal.py [YYYY-MM-DD]

Creates: vault/journal/YYYY/MM/YYYY-MM-DD.md
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ALFRED_HOME = "/mnt/nvme/alfred"
VAULT_DIR = os.path.join(ALFRED_HOME, "vault")
STATE_FILE = os.path.join(ALFRED_HOME, "current_state.json")
EASTERN = ZoneInfo("America/New_York")

# Known people: display name -> vault path (for wikilinks in generated text)
KNOWN_PEOPLE = {
    "<advisor-name-lower>": ("people/<advisor-slug>", "<advisor-name>"),
    "<advisor-key>":         ("people/<advisor-slug>", "<advisor-name>"),
    "<contact-key-a>":         ("people/<contact-slug-a>", "<contact-name-a>"),
    "<contact-key-b>":          ("people/<contact-slug-b>", "<contact-name-b>"),
    "<contact-slug-c>":         ("people/<contact-slug-c>", "<contact-name-c>"),
}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _wikilink_people(text):
    """Replace known people names with wikilinks in a text string."""
    for name, (path, display) in KNOWN_PEOPLE.items():
        pattern = re.compile(re.escape(display), re.IGNORECASE)
        if pattern.search(text):
            text = pattern.sub(f"[[{path}|{display}]]", text, count=1)
    return text


def _read_toby_message(filepath):
    """Extract the user's first message from a conversation file."""
    try:
        with open(filepath) as f:
            for line in f:
                if line.startswith("**User:**"):
                    msg = line.replace("**User:**", "").strip()
                    return msg[:50] + "..." if len(msg) > 50 else msg
    except Exception:
        pass
    return ""


def get_conversations_for_date(date_str):
    """Return list of (display_time, vault_path, alias, summary_path) for a given date.

    Summary files (`<stem>-summary.md`) are paired with their parent conversation
    rather than listed as separate entries. `summary_path` is None when no
    summary exists yet.
    """
    year, month, _ = date_str.split("-")
    conv_dir = os.path.join(VAULT_DIR, "conversations", year, month)
    if not os.path.isdir(conv_dir):
        return []
    all_md = {f for f in os.listdir(conv_dir) if f.startswith(date_str) and f.endswith(".md")}
    summaries = {f for f in all_md if f.endswith("-summary.md")}
    raw = sorted(f for f in all_md if f not in summaries)
    results = []
    for f in raw:
        time_part = f[len(date_str) + 1: len(date_str) + 7]
        display_time = f"{time_part[:2]}:{time_part[2:4]}" if len(time_part) >= 4 else "??"
        vault_path = f"conversations/{year}/{month}/{f[:-3]}"
        msg = _read_toby_message(os.path.join(conv_dir, f))
        alias = f"{display_time}, {msg}" if msg else display_time
        summary_file = f[:-3] + "-summary.md"
        summary_path = (
            f"conversations/{year}/{month}/{summary_file[:-3]}"
            if summary_file in summaries
            else None
        )
        results.append((display_time, vault_path, alias, summary_path))
    return results


def format_location(state):
    loc = state.get("user", {}).get("location", {})
    if "latitude" in loc:
        # GPS location: use reverse-geocoded place name if available, else unknown
        return loc.get("place") or "location unknown"
    return loc.get("description") or loc.get("place") or loc.get("city") or "unknown"


def format_snapshot_section(state):
    bio = state.get("biometrics", {})
    sleep = bio.get("sleep_hours_last_night")
    hrv = bio.get("hrv_ms")
    loc_str = format_location(state)
    is_placeholder = "_note" in bio

    lines = []
    if sleep:
        lines.append(f"**Sleep** {sleep}h")
    if hrv and not is_placeholder:
        lines.append(f"**HRV** {hrv} ms")
    elif hrv:
        lines.append(f"**HRV** {hrv} ms *(placeholder)*")
    lines.append(f"**Location** {loc_str}")

    return "  \n".join(lines) if lines else "_No data_"


def format_calendar_section(state):
    cal = state.get("calendar", {})
    today_events = cal.get("today_events", [])
    if not today_events:
        return "_No events today_"

    rows = ["| Time | Event | Location |", "|------|-------|----------|"]
    for e in today_events:
        title = _wikilink_people(e["title"])
        loc = e.get("location", "") or ""
        rows.append(f"| {e['time']} | {title} | {loc} |")
    return "\n".join(rows)


def format_tomorrow_section(state):
    cal = state.get("calendar", {})
    tomorrow_events = cal.get("tomorrow_events", [])
    if not tomorrow_events:
        return None

    rows = ["| Time | Event | Location |", "|------|-------|----------|"]
    for e in tomorrow_events:
        title = _wikilink_people(e["title"])
        loc = e.get("location", "") or ""
        rows.append(f"| {e['time']} | {title} | {loc} |")
    return "\n".join(rows)


def format_tasks_section(state):
    tasks = state.get("tasks", {})
    overdue = tasks.get("overdue_count", 0)
    due_today = tasks.get("due_today_count", 0)
    items = tasks.get("open_items", [])

    if not overdue and not due_today and not items:
        return "_No tasks_"

    parts = []

    if overdue:
        overdue_items = [i for i in items if i.startswith("OVERDUE:")]
        other_items = [i for i in items if not i.startswith("OVERDUE:")]
        clean_overdue = [i.replace("OVERDUE: ", "") for i in overdue_items]

        callout_lines = [f"> [!warning]+ {overdue} overdue"]
        for item in clean_overdue[:4]:
            callout_lines.append(f"> - {item}")
        if overdue > len(clean_overdue):
            callout_lines.append(f"> - *(and {overdue - len(clean_overdue)} more)*")
        parts.append("\n".join(callout_lines))

        if due_today:
            parts.append(f"**Due today ({due_today})**")
        for item in other_items[:5]:
            parts.append(f"- {item}")
    else:
        if due_today:
            parts.append(f"**Due today: {due_today}**")
        for item in items[:5]:
            parts.append(f"- {item}")

    return "\n".join(parts)


def _render_conversation_line(vault_path, alias, summary_path):
    line = f'- [[{vault_path}|{alias}]]'
    if summary_path:
        line += f' · [[{summary_path}|summary]]'
    return line


def format_conversations_section(conversations, existing_links=None):
    if not conversations:
        return "_None yet_"
    lines = [
        _render_conversation_line(vault_path, alias, summary_path)
        for _, vault_path, alias, summary_path in conversations
    ]
    return "\n".join(lines)


def _is_tomorrow(target_date, now):
    return (target_date - now.date()).days == 1


def generate_journal_note(target_date=None, force=False):
    now = datetime.now(EASTERN)
    if target_date is None:
        target_date = now.date()

    date_str = target_date.strftime("%Y-%m-%d")
    year = target_date.strftime("%Y")
    month = target_date.strftime("%m")
    day_label = target_date.strftime("%A, %B %-d %Y")

    journal_dir = os.path.join(VAULT_DIR, "journal", year, month)
    os.makedirs(journal_dir, exist_ok=True)
    filepath = os.path.join(journal_dir, f"{date_str}.md")

    state = load_state()
    conversations = get_conversations_for_date(date_str)

    # Cron mode (no force): append new conversation links AND add summary
    # backlinks to conversations that gained a summary since the last run.
    if os.path.exists(filepath) and not force:
        with open(filepath) as f:
            existing = f.read()
        changed = False

        # Append any conversation whose raw link isn't in the file yet.
        new_convs = [
            row for row in conversations
            if f"[[{row[1]}|" not in existing and f"[[{row[1]}]]" not in existing
        ]
        if new_convs:
            new_lines = "\n".join(
                _render_conversation_line(vp, a, sp) for _, vp, a, sp in new_convs
            )
            existing = re.sub(
                r"(## Conversations\n)(_None yet_\n?)?",
                lambda m: m.group(1) + new_lines + "\n",
                existing,
                count=1,
            )
            changed = True

        # Attach summary backlinks to conversations already listed but whose
        # summary landed after the initial append.
        for _, vault_path, alias, summary_path in conversations:
            if not summary_path:
                continue
            if f"[[{summary_path}|" in existing or f"[[{summary_path}]]" in existing:
                continue
            line_pattern = re.compile(
                r"^(- \[\[" + re.escape(vault_path) + r"\|[^\]]*\]\])\s*$",
                re.MULTILINE,
            )
            replacement = r"\1" + f" · [[{summary_path}|summary]]"
            updated, n = line_pattern.subn(replacement, existing, count=1)
            if n:
                existing = updated
                changed = True

        if changed:
            with open(filepath, "w") as f:
                f.write(existing)
        print(f"  Updated: {filepath}")
        return filepath

    # Full generation (new file or forced regeneration). Preserve any
    # existing "## Alfred's Notes" content so a force regen does not
    # destroy accumulated reflection bullets.
    preserved_notes_body = ""
    if os.path.exists(filepath):
        try:
            with open(filepath) as f:
                prior = f.read()
            notes_match = re.search(
                r"## Alfred's Notes\n(.*?)(?=\n---\n|\n## |\Z)",
                prior,
                flags=re.DOTALL,
            )
            if notes_match:
                body = notes_match.group(1).strip()
                if body and body != "_Observations and things worth remembering from today_":
                    preserved_notes_body = body
        except OSError:
            pass

    # When pre-generating tomorrow's note, swap today/tomorrow event sources.
    generating_tomorrow = _is_tomorrow(target_date, now)
    if generating_tomorrow:
        # Swap calendar: tomorrow's events become "Today", upcoming shifts to "Tomorrow"
        cal = state.get("calendar", {})
        swapped = dict(state)
        swapped["calendar"] = dict(cal)
        swapped["calendar"]["today_events"] = cal.get("tomorrow_events", [])
        upcoming = cal.get("upcoming", [])
        next_day = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
        swapped["calendar"]["tomorrow_events"] = [e for e in upcoming if e.get("date", "") == next_day]
        state = swapped

    snapshot = format_snapshot_section(state)
    calendar_section = format_calendar_section(state)
    tasks_section = format_tasks_section(state)
    conv_section = format_conversations_section(conversations)

    tomorrow_section = format_tomorrow_section(state)
    tomorrow_block = f"\n## Tomorrow\n{tomorrow_section}\n" if tomorrow_section else ""

    alfred_notes_body = (
        preserved_notes_body
        if preserved_notes_body
        else "_Observations and things worth remembering from today_"
    )

    content = f"""---
date: {date_str}
day_of_week: {target_date.strftime("%A")}
tags: [journal, daily]
---

# {day_label}

## Snapshot
{snapshot}

## Today
{calendar_section}
{tomorrow_block}
## Tasks
{tasks_section}

## Conversations
{conv_section}

## Alfred's Notes
{alfred_notes_body}

---
*Generated by Alfred, {now.strftime("%-I:%M %p")}*
"""

    with open(filepath, "w") as f:
        f.write(content)

    print(f"  Created: {filepath}")
    return filepath


def main():
    target = None
    force = "--force" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if args:
        from datetime import date
        target = date.fromisoformat(args[0])
        force = True  # explicit date always regenerates

    print("[journal] Generating note...")
    generate_journal_note(target, force=force)

    # Pre-create tomorrow's note if it doesn't exist (cron only)
    if target is None:
        tomorrow = datetime.now(EASTERN).date() + timedelta(days=1)
        tomorrow_path = os.path.join(
            VAULT_DIR, "journal", tomorrow.strftime("%Y"), tomorrow.strftime("%m"),
            f"{tomorrow.strftime('%Y-%m-%d')}.md",
        )
        if not os.path.exists(tomorrow_path):
            print("[journal] Pre-creating tomorrow's note...")
            generate_journal_note(tomorrow, force=False)


if __name__ == "__main__":
    main()
