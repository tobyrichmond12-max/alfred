"""Semantic memory: embeddings + sqlite store + cosine search.

Primary provider is Ollama's nomic-embed-text at /api/embeddings. When
that model is not installed or Ollama is unreachable, a deterministic
token-hashing fallback (128-dim signed counts) is used so the whole
stack still works. Once `ollama pull nomic-embed-text` completes, call
`reindex_all()` to regenerate the table with real vectors.

Schema:
    CREATE TABLE chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        text TEXT NOT NULL,
        embedding BLOB NOT NULL,
        dim INTEGER NOT NULL,
        provider TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        UNIQUE(path, chunk_index)
    );
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("alfred.embeddings")

ALFRED_HOME = "/mnt/nvme/alfred"
DB_PATH = Path(ALFRED_HOME) / "vault" / "memory" / "semantic.db"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

FALLBACK_DIM = 128
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+|\d+")
CHUNK_TOKENS = 500
OVERLAP_TOKENS = 50


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB NOT NULL,
            dim INTEGER NOT NULL,
            provider TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            UNIQUE(path, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
        """
    )
    return conn


def _ollama_available() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return False
    for m in data.get("models", []):
        name = m.get("name", "")
        if name.startswith(OLLAMA_MODEL):
            return True
    return False


def _ollama_embed(text: str) -> Optional[list[float]]:
    body = json.dumps({"model": OLLAMA_MODEL, "prompt": text[:8000]}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    vec = data.get("embedding")
    if not isinstance(vec, list):
        return None
    return [float(x) for x in vec]


def _hash_embed(text: str, dim: int = FALLBACK_DIM) -> list[float]:
    vec = [0.0] * dim
    toks = TOKEN_RE.findall((text or "").lower())
    if not toks:
        return vec
    for tok in toks:
        for variant in (tok, tok[:3], tok[:5]):
            h = hashlib.blake2b(variant.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % dim
            sign = 1.0 if (h[4] & 1) else -1.0
            vec[idx] += sign
    # length-normalize so cosine similarity behaves
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _record_tokens(prompt: str, response: str) -> None:
    try:
        from token_tracker import record  # type: ignore

        record("embeddings", len(prompt), len(response))
    except Exception:
        pass


def embed(text: str) -> list[float]:
    text = (text or "").strip()
    if not text:
        return [0.0] * FALLBACK_DIM
    if _ollama_available():
        vec = _ollama_embed(text)
        if vec is not None:
            _record_tokens(text, "[vec]")
            return vec
    return _hash_embed(text)


def _provider() -> str:
    return OLLAMA_MODEL if _ollama_available() else "hash-fallback"


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(buf: bytes) -> list[float]:
    n = len(buf) // 4
    return list(struct.unpack(f"<{n}f", buf))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def chunk_text(text: str, chunk_tokens: int = CHUNK_TOKENS, overlap: int = OVERLAP_TOKENS) -> list[str]:
    tokens = TOKEN_RE.findall(text or "")
    if not tokens:
        return []
    chunks = []
    step = max(1, chunk_tokens - overlap)
    for start in range(0, len(tokens), step):
        slice_toks = tokens[start:start + chunk_tokens]
        if not slice_toks:
            break
        chunks.append(" ".join(slice_toks))
        if start + chunk_tokens >= len(tokens):
            break
    return chunks


def index(path: Path) -> int:
    """Embed a single file and upsert its chunks. Returns chunks written."""
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return 0
    chunks = chunk_text(text)
    if not chunks:
        return 0
    provider = _provider()
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    with _connect() as conn:
        conn.execute("DELETE FROM chunks WHERE path = ?", (str(path),))
        for i, c in enumerate(chunks):
            vec = embed(c)
            conn.execute(
                "INSERT OR REPLACE INTO chunks (path, chunk_index, text, embedding, dim, provider, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(path), i, c, _pack(vec), len(vec), provider, now),
            )
        conn.commit()
    return len(chunks)


def reindex_all(root: Path = Path(ALFRED_HOME) / "vault" / "memory") -> int:
    total = 0
    files = 0
    for pattern in ("**/*.md", "**/*.txt", "**/*.jsonl"):
        for p in root.glob(pattern):
            if ".archive" in p.parts or "semantic.db" in p.name:
                continue
            c = index(p)
            total += c
            if c:
                files += 1
    log.info("reindex_all: %d chunks across %d files", total, files)
    return total


def search(query: str, top_k: int = 5, traverse: int = 0) -> list[dict]:
    q_vec = embed(query)
    results: list[tuple[float, dict]] = []
    with _connect() as conn:
        for row in conn.execute("SELECT path, chunk_index, text, embedding FROM chunks"):
            vec = _unpack(row["embedding"])
            score = _cosine(q_vec, vec)
            results.append((score, {
                "path": row["path"],
                "chunk_index": row["chunk_index"],
                "text": row["text"],
                "score": score,
                "slug": Path(row["path"]).stem,
            }))
    results.sort(key=lambda t: -t[0])
    top = [r[1] for r in results[:top_k]]
    if traverse > 0:
        neighbors = _graph_neighbors([r["slug"] for r in top])
        for n in neighbors:
            n["via_graph"] = True
        top.extend(neighbors[:top_k])
    return top


def _graph_neighbors(slugs: list[str]) -> list[dict]:
    graph_path = Path(ALFRED_HOME) / "data" / "knowledge_graph.json"
    if not graph_path.exists():
        return []
    try:
        graph = json.loads(graph_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    edges = graph.get("edges", [])
    target_slugs = set(slugs)
    out = []
    for e in edges:
        if e.get("source") in target_slugs:
            out.append({"slug": e.get("target"), "path": None, "text": f"via_graph from {e.get('source')}"})
        elif e.get("target") in target_slugs:
            out.append({"slug": e.get("source"), "path": None, "text": f"via_graph to {e.get('target')}"})
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["reindex", "search", "probe"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--top-k", type=int, default=5)
    ns = ap.parse_args()

    if ns.cmd == "probe":
        print("ollama_available:", _ollama_available())
        print("provider:", _provider())
    elif ns.cmd == "reindex":
        n = reindex_all()
        print(f"Semantic index: {n} chunks")
    elif ns.cmd == "search":
        for hit in search(ns.arg or "alfred", top_k=ns.top_k):
            print(f"{hit['score']:.3f} {hit['slug']} :: {hit['text'][:80]}")
