"""Skill discovery and auto-install.

Scans local skills, probes community repos via browser_tools.search_web,
classifies risk, and queues the interesting ones for review. Low-risk
skills can be auto-installed when data/skill_scanner_config.json opts in.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

CONFIG_PATH = Path(ALFRED_HOME) / "data" / "skill_scanner_config.json"
CANDIDATES_PATH = Path(ALFRED_HOME) / "vault" / "reflections" / "skill-candidates.md"

DEFAULT_CONFIG = {
    "auto_install_low_risk": False,
    "max_reviews_per_week": 3,
}


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


def _read_skill_md(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(errors="ignore")
    meta: dict = {}
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end > 0:
            header = raw[3:end]
            body = raw[end + 4:]
            for line in header.splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip().strip("\"'")
    name = meta.get("name", path.parent.name)
    description = meta.get("description") or body.strip().splitlines()[0][:160] if body.strip() else ""
    return {"name": name, "description": description, "body": body, "path": str(path)}


def scan_claude_skills(path: Path = Path.home() / ".claude" / "skills") -> list[dict]:
    out = []
    if not path.exists():
        return out
    for sub in path.iterdir():
        if not sub.is_dir():
            continue
        skill_md = sub / "SKILL.md"
        if not skill_md.exists():
            skill_md = next(sub.glob("*.md"), None)
            if not skill_md:
                continue
        meta = _read_skill_md(skill_md)
        desc = meta.get("description", "")
        permissions = _sniff_permissions(desc + "\n" + meta.get("body", ""))
        try:
            size = sum(p.stat().st_size for p in sub.rglob("*") if p.is_file())
        except OSError:
            size = 0
        out.append({
            "slug": sub.name,
            "name": meta.get("name", sub.name),
            "description": desc,
            "trigger_keywords": [],
            "permissions_required": permissions,
            "installed_at": None,
            "size_kb": size // 1024,
        })
    return out


def scan_codex_plugins(path: Optional[Path] = None) -> list[dict]:
    path = path or Path.home() / ".codex" / "plugins"
    if not path.exists():
        return []
    out = []
    for sub in path.iterdir():
        if not sub.is_dir():
            continue
        readme = next(sub.glob("README*"), None)
        meta = _read_skill_md(readme) if readme else {"name": sub.name, "description": ""}
        perms = _sniff_permissions(meta.get("description", "") + meta.get("body", ""))
        out.append({
            "slug": sub.name,
            "name": meta.get("name", sub.name),
            "description": meta.get("description", ""),
            "trigger_keywords": [],
            "permissions_required": perms,
            "installed_at": None,
            "size_kb": 0,
        })
    return out


def _sniff_permissions(text: str) -> list[str]:
    lower = text.lower()
    perms = []
    for needle, tag in (
        ("network", "network"),
        ("fetch", "network"),
        ("http", "network"),
        ("shell", "shell"),
        ("subprocess", "shell"),
        ("file system", "filesystem"),
        ("reads file", "filesystem-read"),
        ("writes to", "filesystem-write"),
        ("deletes", "filesystem-delete"),
        ("install", "install"),
        ("sudo", "sudo"),
    ):
        if needle in lower and tag not in perms:
            perms.append(tag)
    return perms


def classify_risk(skill: dict) -> str:
    perms = set(skill.get("permissions_required") or [])
    desc = skill.get("description", "").lower() + skill.get("body", "").lower()
    high_signals = {"sudo", "install", "filesystem-delete"} & perms
    if high_signals or "rm -rf" in desc or "installs" in desc and "package" in desc:
        return "high"
    if {"network", "filesystem-write", "shell"} & perms:
        return "medium"
    return "low"


def discover_community_skills(topic: Optional[str] = None) -> list[dict]:
    queries = ['"claude code skill" github 2026', '"codex plugin" github']
    if topic:
        queries.append(f'"claude skill" {topic} github')
    results: list[dict] = []
    try:
        from browser_tools import search_web, fetch_page  # type: ignore
    except Exception:
        return results
    seen: set[str] = set()
    for q in queries:
        try:
            hits = search_web(q, top_n=5) or []
        except Exception:
            hits = []
        for h in hits:
            url = getattr(h, "url", "")
            if not url or "github.com" not in url or url in seen:
                continue
            seen.add(url)
            try:
                page = fetch_page(url, max_chars=6000)
                body = getattr(page, "text", "") or ""
                title = getattr(page, "title", url)
            except Exception:
                continue
            perms = _sniff_permissions(body)
            results.append({
                "url": url,
                "name": title,
                "description": body[:200].strip(),
                "install_snippet": _extract_install(body),
                "permissions_sniffed": perms,
            })
            if len(results) >= 10:
                return results
    return results


_INSTALL_RE = re.compile(r"(?im)(?:bash\s*$.+\n)?(pip install|npm install|cargo install|git clone) [^\n]+")


def _extract_install(text: str) -> str:
    m = _INSTALL_RE.search(text or "")
    return m.group(0) if m else ""


def queue_for_review(skill: dict) -> None:
    CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    risk = classify_risk(skill)
    line = (
        f"- {skill.get('name', skill.get('url','?'))} "
        f"({risk}) :: {skill.get('description','')[:120]}"
    )
    with CANDIDATES_PATH.open("a") as f:
        if CANDIDATES_PATH.stat().st_size == 0:
            f.write(f"# Skill candidates\n\n")
        f.write(f"{line}\n")


def auto_install(skill: dict) -> bool:
    cfg = _load_config()
    if not cfg.get("auto_install_low_risk", False):
        return False
    if classify_risk(skill) != "low":
        return False
    import subprocess

    url = skill.get("url")
    if not url:
        return False
    target = Path.home() / ".claude" / "skills" / re.sub(r"[^a-z0-9]+", "-", skill.get("name", "skill").lower())[:40]
    target.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["git", "clone", "--depth", "1", url, str(target)], timeout=60, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CANDIDATES_PATH.open("a") as f:
        f.write(f"INSTALLED {date.today().isoformat()}: {skill.get('name')} from {url}\n")
    return True


def weekly_scan() -> dict:
    installed = scan_claude_skills() + scan_codex_plugins()
    community = discover_community_skills()
    for item in community:
        queue_for_review(item)
        if classify_risk(item) == "low":
            auto_install(item)
    return {"installed": len(installed), "community_candidates": len(community)}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest", "scan"])
    ns = ap.parse_args()
    if ns.cmd == "selftest":
        safe = {"name": "read-only file listing", "description": "read-only file listing of a directory", "permissions_sniffed": []}
        risky = {"name": "pkg installer", "description": "installs npm packages and runs shell", "permissions_sniffed": ["install", "shell"]}
        c1 = classify_risk(safe)
        c2 = classify_risk(risky)
        print(f"Skills self-test: 2 candidates, {int(c1=='low')} low risk, {int(c2=='high')} high risk")
        raise SystemExit(0 if c1 == "low" and c2 == "high" else 1)
    elif ns.cmd == "scan":
        print(json.dumps(weekly_scan(), indent=2))
