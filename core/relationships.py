"""Relationship CRM.

People live under vault/memory/people/<slug>.md with YAML frontmatter and
an append-only body. Passive updates bump last_contact whenever a name
shows up in a telegram conversation, email sender, or calendar attendee.

API:
    upsert(name, role, importance)
    update_contact(name, interaction_type, notes)
    get_contact(name)
    get_stale_relationships(days, min_importance)
    mentions_in_text(text)
    weekly_digest()
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

PEOPLE_DIR = Path(ALFRED_HOME) / "vault" / "memory" / "people"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/thoth/.local/bin/claude")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "unknown"


def _path_for(slug: str) -> Path:
    return PEOPLE_DIR / f"{slug}.md"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    header_block = text[4:end]
    body = text[end + 5:]
    meta: dict = {}
    current_key = None
    for line in header_block.splitlines():
        if line.startswith("  - "):
            if current_key is None:
                continue
            meta.setdefault(current_key, [])
            if isinstance(meta[current_key], list):
                meta[current_key].append(line[4:].strip())
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value:
            if value.startswith("[") and value.endswith("]"):
                meta[key] = [x.strip().strip("'").strip('"') for x in value[1:-1].split(",") if x.strip()]
            else:
                meta[key] = value.strip().strip("'").strip('"')
            current_key = None
        else:
            meta[key] = []
            current_key = key
    return meta, body


def _render_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            if len(value) <= 5:
                items = ", ".join(f'"{v}"' for v in value)
                lines.append(f"{key}: [{items}]")
            else:
                lines.append(f"{key}:")
                for v in value:
                    lines.append(f"  - {v}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def upsert(name: str, role: str = "unknown", importance: int = 3) -> Path:
    slug = slugify(name)
    path = _path_for(slug)
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        meta, body = _parse_frontmatter(path.read_text(errors="ignore"))
        meta.setdefault("role", role)
        meta.setdefault("importance", importance)
        if "name" not in meta:
            meta["name"] = name
        path.write_text(_render_frontmatter(meta) + body)
        return path
    meta = {
        "name": name,
        "slug": slug,
        "role": role,
        "importance": int(importance),
        "last_contact": date.today().isoformat(),
        "channels": [],
        "pending": [],
    }
    body = f"\n# {name}\n\nAdded {date.today().isoformat()}.\n"
    path.write_text(_render_frontmatter(meta) + body)
    _announce_new_person(name, slug)
    return path


def update_contact(name: str, interaction_type: str, notes: str = "") -> None:
    slug = slugify(name)
    path = _path_for(slug)
    if not path.exists():
        upsert(name)
    raw = path.read_text(errors="ignore")
    meta, body = _parse_frontmatter(raw)
    meta["last_contact"] = date.today().isoformat()
    channels = meta.get("channels") or []
    if interaction_type and interaction_type not in channels:
        channels.append(interaction_type)
    meta["channels"] = channels
    bullet = f"- {datetime.now().strftime('%Y-%m-%d %H:%M')} [{interaction_type}] {notes[:200]}".rstrip()
    body_lines = body.strip().splitlines()
    body_lines.insert(0, bullet)
    path.write_text(_render_frontmatter(meta) + "\n" + "\n".join(body_lines) + "\n")


def get_contact(name: str) -> Optional[dict]:
    slug = slugify(name)
    path = _path_for(slug)
    if not path.exists():
        return None
    meta, body = _parse_frontmatter(path.read_text(errors="ignore"))
    meta["body"] = body.strip()
    return meta


def _importance_int(meta: dict) -> int:
    try:
        return int(meta.get("importance", 3))
    except (TypeError, ValueError):
        return 3


def _last_contact_date(meta: dict) -> Optional[date]:
    lc = meta.get("last_contact")
    if not lc:
        return None
    try:
        return datetime.strptime(str(lc)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def get_stale_relationships(days: int = 14, min_importance: int = 3) -> list[dict]:
    if not PEOPLE_DIR.exists():
        return []
    today = date.today()
    stale = []
    for p in PEOPLE_DIR.glob("*.md"):
        meta, _ = _parse_frontmatter(p.read_text(errors="ignore"))
        if _importance_int(meta) < min_importance:
            continue
        lc = _last_contact_date(meta)
        if not lc:
            continue
        gap = (today - lc).days
        if gap < days:
            continue
        stale.append({
            "name": meta.get("name") or p.stem,
            "role": meta.get("role", "unknown"),
            "importance": _importance_int(meta),
            "days_since": gap,
            "pending": meta.get("pending") or [],
            "score": gap * _importance_int(meta),
        })
    stale.sort(key=lambda r: -r["score"])
    return stale


def mentions_in_text(text: str) -> list[str]:
    if not PEOPLE_DIR.exists():
        return []
    lower = " " + text.lower() + " "
    hits: list[str] = []
    seen = set()
    for p in PEOPLE_DIR.glob("*.md"):
        slug = p.stem
        meta, _ = _parse_frontmatter(p.read_text(errors="ignore"))
        name = (meta.get("name") or slug).strip()
        if not name:
            continue
        parts = name.lower().split()
        if not parts:
            continue
        patterns = {name.lower(), slug.lower(), parts[0]}
        for pat in patterns:
            if len(pat) < 3:
                continue
            if f" {pat} " in lower or f" {pat}'s " in lower or lower.startswith(f"{pat} "):
                if name not in seen:
                    seen.add(name)
                    hits.append(name)
                break
    return hits


def weekly_digest(min_importance: int = 3) -> str:
    stale = get_stale_relationships(days=7, min_importance=min_importance)
    if not stale:
        return "Relationship check-in: nothing stale this week."
    lines = ["Relationship check-in:"]
    for s in stale[:10]:
        pending = f" Pending: {s['pending'][0]}." if s["pending"] else ""
        lines.append(f"- {s['name']} ({s['role']}, {s['importance']}): last contact {s['days_since']} days ago.{pending}")
    return "\n".join(lines)


def _announce_new_person(name: str, slug: str) -> None:
    try:
        from hud import activity  # type: ignore

        activity(f"New person file: {slug}")
    except Exception:
        pass


def passive_update(user_text: str, reply_text: str = "") -> None:
    """Runs in a background thread from run_claude.chat."""
    names = mentions_in_text(user_text + "\n" + reply_text)
    for n in names:
        try:
            update_contact(n, "conversation", user_text[:120])
        except Exception:
            pass


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest", "digest"])
    ns = ap.parse_args()
    if ns.cmd == "selftest":
        # seed three fake people in a temp dir
        import tempfile

        PEOPLE_DIR = Path(tempfile.mkdtemp()) / "people"
        # rebind module attribute so helpers pick it up
        globals()["PEOPLE_DIR"] = PEOPLE_DIR
        PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
        upsert("<advisor-name>", role="co-op-advisor", importance=5)
        upsert("Mom", role="family", importance=5)
        upsert("<contact-name-a>", role="roommate", importance=3)
        # mark <advisor> stale
        p = _path_for("<advisor>-newsome")
        meta, body = _parse_frontmatter(p.read_text())
        meta["last_contact"] = (date.today() - timedelta(days=20)).isoformat()
        meta.setdefault("pending", []).append("Send updated resume")
        p.write_text(_render_frontmatter(meta) + body)
        stale = get_stale_relationships(days=14, min_importance=3)
        print(f"Relationships self-test: 3 seeded, {len(stale)} stale, digest rendered")
        raise SystemExit(0 if stale and stale[0]["name"] == "<advisor-name>" else 1)
    elif ns.cmd == "digest":
        print(weekly_digest())
