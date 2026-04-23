"""Migrates all memories from SQLite to Obsidian markdown vault.

One-time migration. Safe to re-run, skips existing files.
Run: python3 /mnt/nvme/alfred/core/migrate_memories.py
"""
import json
import os
import re
import sqlite3
import sys

ALFRED_HOME = "/mnt/nvme/alfred"
VAULT_DIR = os.path.join(ALFRED_HOME, "vault")
MEMORY_DB = os.path.join(ALFRED_HOME, "data", "memory.db")

# Map memory_type → vault subfolder
FOLDER_MAP = {
    "preference": "memory/preferences",
    "goal":       "memory/facts",
    "commitment": "memory/facts",
    "reflection": "memory/facts",
    "observation": "memory/facts",
    "briefing":   "memory/facts",
    "relationship": "memory/people",
    "decision":   "memory/facts",
    "doubt":      "memory/facts",
    "emotion":    "memory/facts",
    "event":      "memory/facts",
    "instruction": "memory/facts",
    "problem":    "memory/facts",
    "question":   "memory/facts",
    "statement":  "memory/facts",
}
DEFAULT_FOLDER = "memory/facts"


def slugify(text, max_len=48):
    text = re.sub(r"\[.*?\]", "", text)  # strip [Reflection] prefixes
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = text.strip("-")
    slug = text[:max_len].rstrip("-")
    return slug or "untitled"


def tags_for_type(memory_type, db_tags):
    base = [memory_type]
    try:
        parsed = json.loads(db_tags) if db_tags else []
        base.extend(parsed)
    except Exception:
        pass
    return base


def migrate():
    conn = sqlite3.connect(MEMORY_DB)
    rows = conn.execute(
        "SELECT id, memory_type, tags, importance, content, ts_created FROM memories ORDER BY ts_created"
    ).fetchall()
    conn.close()

    created = 0
    skipped = 0

    for mem_id, memory_type, db_tags, importance, content, ts_created in rows:
        folder = FOLDER_MAP.get(memory_type, DEFAULT_FOLDER)
        dest_dir = os.path.join(VAULT_DIR, folder)
        os.makedirs(dest_dir, exist_ok=True)

        slug = slugify(content)
        filename = f"{slug}-{mem_id}.md"
        filepath = os.path.join(dest_dir, filename)

        if os.path.exists(filepath):
            skipped += 1
            continue

        # Clean content: strip leading [Type] prefix
        body = re.sub(r"^\[[\w\s]+\]\s*", "", content).strip()
        title = body[:80] if len(body) <= 80 else body[:77] + "..."
        tags = tags_for_type(memory_type, db_tags)
        date_str = ts_created[:10] if ts_created else "2026-04-18"

        note = f"""---
type: {memory_type}
tags: {json.dumps(tags)}
importance: {importance}
created: {date_str}
source: sqlite-migration
memory_id: {mem_id}
---

# {title}

{body}

## Related
- [[profile]]
"""
        with open(filepath, "w") as f:
            f.write(note)
        created += 1

    print(f"  Migrated: {created} memories created, {skipped} already existed")
    return created


if __name__ == "__main__":
    print("[migrate_memories] Starting...")
    migrate()
    print("[migrate_memories] Done.")
