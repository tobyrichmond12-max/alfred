"""RAG over any ingested document.

data/rag.db holds one row per chunk. ingest_document accepts PDF, DOCX,
TXT, MD, HTML, JSON. query_rag returns top-k by cosine similarity with
adjacent chunks as context. ask_rag formats hits as Sources then asks
claude -p to answer using only those sources.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("alfred.rag")

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

from embeddings import _connect as _embed_connect, _cosine, _pack, _unpack, chunk_text, embed, _provider  # type: ignore

DB_PATH = Path(ALFRED_HOME) / "data" / "rag.db"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/thoth/.local/bin/claude")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB NOT NULL,
            ingested_at TEXT NOT NULL,
            UNIQUE(source_file, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_source ON chunks(source_file);
        """
    )
    return conn


def _extract_text(file_path: str) -> str:
    p = Path(file_path)
    suffix = p.suffix.lower()
    if suffix in (".txt", ".md", ".json", ".jsonl"):
        return p.read_text(errors="ignore")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(str(p))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            try:
                r = subprocess.run(
                    ["pdftotext", str(p), "-"],
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode == 0:
                    return r.stdout
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            return ""
    if suffix == ".docx":
        try:
            from docx import Document  # type: ignore

            doc = Document(str(p))
            return "\n\n".join(par.text for par in doc.paragraphs)
        except ImportError:
            return ""
    if suffix in (".html", ".htm"):
        try:
            from browser_tools import _Reader  # type: ignore

            rdr = _Reader()
            rdr.feed(p.read_text(errors="ignore"))
            return rdr.text
        except Exception:
            # naive tag strip
            text = p.read_text(errors="ignore")
            return re.sub(r"<[^>]+>", " ", text)
    return ""


def ingest_document(file_path: str, source_hint: Optional[str] = None) -> int:
    text = _extract_text(file_path)
    if not text.strip():
        return 0
    chunks = chunk_text(text)
    if not chunks:
        return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    source = source_hint or file_path
    with _connect() as conn:
        conn.execute("DELETE FROM chunks WHERE source_file = ?", (source,))
        for i, c in enumerate(chunks):
            vec = embed(c)
            conn.execute(
                "INSERT OR REPLACE INTO chunks (source_file, chunk_index, text, embedding, ingested_at) VALUES (?, ?, ?, ?, ?)",
                (source, i, c, _pack(vec), now),
            )
        conn.commit()
    return len(chunks)


def query_rag(question: str, top_k: int = 5, source_filter: Optional[str] = None) -> list[dict]:
    q_vec = embed(question)
    results: list[tuple[float, dict]] = []
    with _connect() as conn:
        sql = "SELECT source_file, chunk_index, text, embedding FROM chunks"
        args: tuple = ()
        if source_filter:
            sql += " WHERE source_file = ?"
            args = (source_filter,)
        for row in conn.execute(sql, args):
            vec = _unpack(row["embedding"])
            score = _cosine(q_vec, vec)
            results.append((score, {
                "source_file": row["source_file"],
                "chunk_index": row["chunk_index"],
                "text": row["text"],
                "score": score,
            }))
    results.sort(key=lambda t: -t[0])
    top = [r[1] for r in results[:top_k]]

    # attach context_before and context_after
    if top:
        with _connect() as conn:
            for hit in top:
                hit["context_before"] = _neighbor_text(conn, hit["source_file"], hit["chunk_index"] - 1)
                hit["context_after"] = _neighbor_text(conn, hit["source_file"], hit["chunk_index"] + 1)
    return top


def _neighbor_text(conn, source_file: str, chunk_index: int) -> str:
    if chunk_index < 0:
        return ""
    row = conn.execute(
        "SELECT text FROM chunks WHERE source_file = ? AND chunk_index = ?",
        (source_file, chunk_index),
    ).fetchone()
    return row["text"] if row else ""


def ask_rag(question: str, top_k: int = 5) -> str:
    hits = query_rag(question, top_k=top_k)
    if not hits:
        return "I do not have any indexed sources that match that."
    sources_block = []
    for i, h in enumerate(hits, start=1):
        sources_block.append(
            f"[{i}] {Path(h['source_file']).name}:{h['chunk_index']}\n{h['text']}"
        )
    prompt = (
        "Answer using only the provided sources. Cite with [n] markers. If the "
        "sources do not contain the answer, say so.\n\n"
        "Sources:\n" + "\n\n".join(sources_block) + f"\n\nQuestion: {question}"
    )
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p"],
            input=prompt,
            capture_output=True, text=True, timeout=60,
            cwd=ALFRED_HOME, env=env,
        )
    except subprocess.TimeoutExpired:
        return "Timed out asking claude."
    return (r.stdout or "").strip()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ingest", "query", "ask", "selftest"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--top-k", type=int, default=5)
    ns = ap.parse_args()

    if ns.cmd == "ingest":
        n = ingest_document(ns.arg)
        print(f"Ingested {n} chunks")
    elif ns.cmd == "query":
        for h in query_rag(ns.arg or "", top_k=ns.top_k):
            print(f"{h['score']:.3f} {Path(h['source_file']).name}:{h['chunk_index']} {h['text'][:80]}")
    elif ns.cmd == "ask":
        print(ask_rag(ns.arg or "", top_k=ns.top_k))
    elif ns.cmd == "selftest":
        import tempfile

        tmp = Path(tempfile.mkdtemp()) / "seed.txt"
        tmp.write_text(
            "The Jetson Orin Nano runs Alfred nightly.\n\n"
            "<advisor-name> is the user's advisor.\n\n"
            "The morning briefing pushes via Telegram at 8 AM.\n"
        )
        n = ingest_document(str(tmp))
        hits = query_rag("Who is the co-op advisor?", top_k=3)
        top = hits[0] if hits else {}
        ok = "<advisor-key>" in top.get("text", "").lower()
        rank = [i for i, h in enumerate(hits) if "<advisor-key>" in h.get("text", "").lower()]
        print(f"RAG self-test: ingested {n} chunks, query hit rank {rank[0] + 1 if rank else '-'}")
        raise SystemExit(0 if ok else 1)
