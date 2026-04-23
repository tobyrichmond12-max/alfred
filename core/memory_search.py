"""Keyword memory search over data/memory.db.

A lightweight companion to memory.search_memories: no embeddings, no
local model dependency, just substring + tag + slug matching over the
rows populated by core/import_claude.build_index. Fast enough to call
from voice conversations and tolerant of the current (embedding-null)
vault-imported rows.

CLI:
    python3 -m core.memory_search "what does user think about claude max?"
    python3 -m core.memory_search --type people <advisor>
    python3 -m core.memory_search --limit 5 thoth

From a Python script:
    from memory_search import search
    hits = search("alfred voice pipeline", top_k=5)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Any

ALFRED_HOME = "/mnt/nvme/alfred"
MEMORY_DB = os.path.join(ALFRED_HOME, "data", "memory.db")

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_STOPWORDS = {
    "the", "and", "for", "that", "this", "you", "are", "with", "not",
    "but", "from", "have", "had", "has", "was", "were", "will", "would",
    "about", "any", "all", "can", "could", "should", "what", "where",
    "when", "who", "how", "why", "there", "their", "they", "them",
}


def _tokens(query: str) -> list[str]:
    return [w.lower() for w in _TOKEN_RE.findall(query) if w.lower() not in _STOPWORDS]


def _score_row(row: sqlite3.Row, tokens: list[str]) -> float:
    """Cheap relevance score: slug match counts 3x, tag match 2x, body 1x."""
    if not tokens:
        return 0.0
    slug = (row["slug"] or "").lower()
    content = (row["content"] or "").lower()
    tags_raw = row["tags"] or "[]"
    try:
        tags = {t.lower() for t in json.loads(tags_raw)}
    except (json.JSONDecodeError, TypeError):
        tags = set()

    score = 0.0
    for tok in tokens:
        if tok in slug:
            score += 3.0
        if tok in tags:
            score += 2.0
        if tok in content:
            score += 1.0
    return score


def search(
    query: str,
    top_k: int = 8,
    memory_type: str | None = None,
    db_path: str = MEMORY_DB,
) -> list[dict[str, Any]]:
    """Return up to top_k matching memory rows, highest-scoring first.

    Each hit has: slug, memory_type, content, tags (list), valid_at, score.
    """
    tokens = _tokens(query)
    if not tokens:
        return []
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where = "slug IS NOT NULL"
    params: list[Any] = []
    if memory_type:
        where += " AND memory_type = ?"
        params.append(memory_type)
    rows = conn.execute(
        f"SELECT id, slug, memory_type, content, tags, valid_at "
        f"FROM memories WHERE {where}",
        params,
    ).fetchall()
    conn.close()

    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        s = _score_row(row, tokens)
        if s > 0:
            scored.append((s, row))
    scored.sort(key=lambda x: x[0], reverse=True)

    hits: list[dict[str, Any]] = []
    for score, row in scored[:top_k]:
        try:
            tags = json.loads(row["tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            tags = []
        hits.append(
            {
                "slug": row["slug"],
                "memory_type": row["memory_type"],
                "content": row["content"],
                "tags": tags,
                "valid_at": row["valid_at"],
                "score": round(score, 1),
            }
        )
    return hits


def format_for_voice(hits: list[dict[str, Any]]) -> str:
    """Render hits as a compact plain-text block for voice or prompt context."""
    if not hits:
        return ""
    lines: list[str] = []
    for h in hits:
        snippet = h["content"].strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:240].rsplit(" ", 1)[0] + "..."
        lines.append(f"[{h['memory_type']}/{h['slug']}] {snippet}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("query", nargs="+")
    parser.add_argument("--type", dest="memory_type", default=None)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true", help="emit raw JSON")
    args = parser.parse_args()
    q = " ".join(args.query)
    hits = search(q, top_k=args.limit, memory_type=args.memory_type)
    if args.json:
        print(json.dumps(hits, indent=2))
    else:
        print(format_for_voice(hits) or "(no hits)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
