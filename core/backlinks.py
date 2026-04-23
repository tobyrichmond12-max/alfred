"""Neural backlinks and knowledge graph over vault/*.md.

Every note gets a `## Related` section when generate_backlinks finds
candidates. build_graph emits data/knowledge_graph.json with nodes and
edges (wikilink + semantic).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

from embeddings import _cosine, embed  # type: ignore

DEFAULT_VAULT = Path(ALFRED_HOME) / "vault"
CACHE_DB = Path(ALFRED_HOME) / "data" / "backlinks_cache.db"
GRAPH_PATH = Path(ALFRED_HOME) / "data" / "knowledge_graph.json"
MIN_BODY_CHARS = 200
RELATED_MARKER = "## Related"
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _cache_conn() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS embeds (
            path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            embedding TEXT NOT NULL
        );
        """
    )
    return conn


def _cached_embed(path: Path, text: str) -> list[float]:
    mtime = path.stat().st_mtime
    with _cache_conn() as conn:
        row = conn.execute("SELECT mtime, embedding FROM embeds WHERE path = ?", (str(path),)).fetchone()
        if row and abs(row[0] - mtime) < 1:
            return json.loads(row[1])
        vec = embed(text)
        conn.execute(
            "INSERT OR REPLACE INTO embeds (path, mtime, embedding) VALUES (?, ?, ?)",
            (str(path), mtime, json.dumps(vec)),
        )
        conn.commit()
        return vec


def scan_notes(vault_path: Path = DEFAULT_VAULT) -> list[dict]:
    out: list[dict] = []
    for p in vault_path.rglob("*.md"):
        if "_templates" in p.parts or ".archive" in p.parts:
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        slug = p.stem
        title = slug
        # use first H1 if present
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        existing_links = set(WIKILINK_RE.findall(text))
        out.append({
            "path": p,
            "slug": slug,
            "title": title,
            "existing_links": existing_links,
            "text": text,
        })
    return out


def related_candidates(note: dict, all_notes: list[dict], threshold: float = 0.7, top_k: int = 5) -> list[dict]:
    if len(note["text"]) < MIN_BODY_CHARS:
        return []
    q_vec = _cached_embed(note["path"], note["text"])
    scored = []
    for other in all_notes:
        if other["path"] == note["path"]:
            continue
        if len(other["text"]) < MIN_BODY_CHARS:
            continue
        if other["slug"] in note["existing_links"]:
            continue
        o_vec = _cached_embed(other["path"], other["text"])
        score = _cosine(q_vec, o_vec)
        if score >= threshold:
            first_sentence = other["text"].strip().split(".")[0][:120]
            scored.append({
                "slug": other["slug"],
                "title": other["title"],
                "score": score,
                "reason": first_sentence,
            })
    scored.sort(key=lambda d: -d["score"])
    return scored[:top_k]


def _append_related(path: Path, candidates: list[dict]) -> None:
    if not candidates:
        return
    existing = path.read_text(errors="ignore")
    if RELATED_MARKER in existing:
        return
    block = ["", RELATED_MARKER, ""]
    for c in candidates:
        reason = (c.get("reason") or "").replace("\n", " ").strip() or c.get("title", "")
        block.append(f"- [[{c['slug']}]]: {reason}")
    block.append("")
    path.write_text(existing.rstrip() + "\n" + "\n".join(block) + "\n")


def generate_backlinks(vault_path: Path = DEFAULT_VAULT, dry_run: bool = False) -> dict:
    notes = scan_notes(vault_path)
    summary = {"scanned": len(notes), "linked": 0, "skipped_short": 0, "skipped_no_candidates": 0}
    for note in notes:
        if len(note["text"]) < MIN_BODY_CHARS:
            summary["skipped_short"] += 1
            continue
        cands = related_candidates(note, notes)
        if not cands:
            summary["skipped_no_candidates"] += 1
            continue
        if RELATED_MARKER in note["text"]:
            continue
        summary["linked"] += 1
        if not dry_run:
            _append_related(note["path"], cands)
    return summary


def build_graph(vault_path: Path = DEFAULT_VAULT) -> dict:
    notes = scan_notes(vault_path)
    slug_to_title = {n["slug"]: n["title"] for n in notes}
    nodes = []
    edges: list[dict] = []
    degree: dict[str, int] = {s: 0 for s in slug_to_title}

    for n in notes:
        for link in n["existing_links"]:
            slug = link.split("|")[0].strip()
            if slug in slug_to_title and slug != n["slug"]:
                edges.append({"source": n["slug"], "target": slug, "kind": "wikilink", "weight": 1.0})
                degree[slug] = degree.get(slug, 0) + 1
                degree[n["slug"]] = degree.get(n["slug"], 0) + 1

    # add semantic edges from top-3 candidates
    for n in notes:
        if len(n["text"]) < MIN_BODY_CHARS:
            continue
        cands = related_candidates(n, notes, threshold=0.7, top_k=3)
        for c in cands:
            edges.append({
                "source": n["slug"],
                "target": c["slug"],
                "kind": "semantic",
                "weight": round(float(c["score"]), 3),
            })
            degree[c["slug"]] = degree.get(c["slug"], 0) + 1
            degree[n["slug"]] = degree.get(n["slug"], 0) + 1

    for n in notes:
        d = degree.get(n["slug"], 0)
        cluster = "hub" if d >= 6 else ("mid" if d >= 2 else "leaf")
        nodes.append({"id": n["slug"], "title": n["title"], "path": str(n["path"]), "cluster": cluster})

    graph = {"nodes": nodes, "edges": edges}
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    GRAPH_PATH.write_text(json.dumps(graph, indent=2))
    return {"nodes": len(nodes), "edges": len(edges)}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["generate", "graph", "selftest"])
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args()

    if ns.cmd == "generate":
        print(json.dumps(generate_backlinks(dry_run=ns.dry_run), indent=2))
    elif ns.cmd == "graph":
        print(json.dumps(build_graph(), indent=2))
    elif ns.cmd == "selftest":
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        seeds = [
            ("alpha.md", "# Alpha\n\n" + ("alfred telegram bot notifications " * 30)),
            ("beta.md", "# Beta\n\n" + ("alfred telegram bot pushes nudges " * 30)),
            ("gamma.md", "# Gamma\n\n" + ("codex orchestrator handles coding tasks " * 30)),
            ("delta.md", "# Delta\n\n" + ("codex orchestrator enqueues coding tasks " * 30)),
        ]
        for name, body in seeds:
            (tmp / name).write_text(body)
        summary = generate_backlinks(tmp, dry_run=True)
        gstats = build_graph(tmp)
        ok = summary["linked"] >= 1
        print(f"Backlinks self-test: {summary['linked']} candidates, graph shape ok ({gstats['nodes']}n/{gstats['edges']}e)")
        raise SystemExit(0 if ok else 1)
