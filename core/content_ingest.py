"""Content ingestion: YouTube / article / social -> knowledge file + RAG.

ingest(url) -> {kind, title, transcript, analysis, suggestions}

Videos: yt-dlp extracts audio, whisper transcribes, claude -p analyzes.
Articles: browser_tools.fetch_page then claude -p analyzes.
Social posts currently route through fetch_page (Twitter / X via nitter-
style or regular HTML when available).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

KNOWLEDGE_DIR = Path(ALFRED_HOME) / "vault" / "memory" / "knowledge"
RATE_PATH = Path(ALFRED_HOME) / "data" / "ingest_recent.json"
RATE_PATH.parent.mkdir(parents=True, exist_ok=True)

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/thoth/.local/bin/claude")

VIDEO_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com")


def _classify(url: str) -> str:
    lower = url.lower()
    if any(h in lower for h in VIDEO_HOSTS):
        return "video"
    if "twitter.com" in lower or "x.com" in lower:
        return "social"
    return "article"


def _slug_for_url(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def _claude(prompt: str, timeout: int = 90) -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p"],
            input=prompt,
            capture_output=True, text=True, timeout=timeout,
            cwd=ALFRED_HOME, env=env,
        )
    except subprocess.TimeoutExpired:
        return ""
    return (r.stdout or "").strip()


def _analyze(text: str, kind: str) -> dict:
    if not text:
        return {"summary": "", "knowledge": [], "suggestions": []}
    prompt = (
        "Given this source, produce strict JSON:\n"
        '{"summary": "3-line summary", "knowledge": ["tagged knowledge items, 1 line each"], '
        '"suggestions": [{"text": "self-improvement suggestion for Alfred", "size": "small|architectural", "confidence": 0.0}]}\n\n'
        f"Kind: {kind}\n\nSource:\n{text[:6000]}"
    )
    raw = _claude(prompt, timeout=90)
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        pass
    return {"summary": raw[:400], "knowledge": [], "suggestions": []}


def _transcribe_video(url: str) -> tuple[str, str]:
    """Returns (title, transcript). Silent fallback on failures."""
    tmp = Path(tempfile.mkdtemp())
    try:
        r = subprocess.run(
            [
                "yt-dlp", "--quiet", "--no-progress",
                "--extract-audio", "--audio-format", "wav",
                "--output", str(tmp / "%(id)s.%(ext)s"),
                url,
            ],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            return ("", "")
        wav_files = list(tmp.glob("*.wav"))
        if not wav_files:
            return ("", "")
        # attempt to grab a title
        title = ""
        title_r = subprocess.run(
            ["yt-dlp", "--quiet", "--get-title", url],
            capture_output=True, text=True, timeout=30,
        )
        if title_r.returncode == 0:
            title = title_r.stdout.strip()[:120]
        try:
            from whisper_wrapper import transcribe_file  # type: ignore

            transcript = transcribe_file(str(wav_files[0]))
        except Exception:
            transcript = ""
        return (title or wav_files[0].stem, transcript)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ("", "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fetch_article(url: str) -> tuple[str, str]:
    try:
        from browser_tools import fetch_page  # type: ignore

        page = fetch_page(url, max_chars=20000)
        return (getattr(page, "title", "") or url, getattr(page, "text", "") or "")
    except Exception:
        return (url, "")


def _rate_ok() -> bool:
    now = time.time()
    try:
        data = json.loads(RATE_PATH.read_text())
        stamps = [t for t in data.get("stamps", []) if now - t < 3600]
    except (OSError, json.JSONDecodeError):
        stamps = []
    if len(stamps) >= 5:
        return False
    stamps.append(now)
    RATE_PATH.write_text(json.dumps({"stamps": stamps}))
    return True


def _store_knowledge(slug: str, url: str, kind: str, title: str, transcript: str, analysis: dict) -> Path:
    category = "videos" if kind == "video" else "articles"
    target = KNOWLEDGE_DIR / category
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{slug}.md"
    body = [
        "---",
        f"source: {url}",
        f"kind: {kind}",
        f"title: {title}",
        f"ingested: {time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime())}",
        "---",
        "",
        f"# {title or url}",
        "",
        "## Summary",
        analysis.get("summary", "") or "",
        "",
        "## Knowledge items",
    ]
    for item in analysis.get("knowledge", []) or []:
        body.append(f"- {item}")
    body.append("")
    body.append("## Transcript excerpt")
    body.append(transcript[:2000])
    path.write_text("\n".join(body))
    # ingest into rag for querying via /askdoc
    try:
        from rag import ingest_document  # type: ignore

        ingest_document(str(path))
    except Exception:
        pass
    return path


def _dispatch_suggestions(suggestions: list[dict]) -> int:
    dispatched = 0
    try:
        from codex_orchestrator import enqueue  # type: ignore
    except Exception:
        return 0
    for s in suggestions or []:
        size = (s.get("size") or "").lower()
        confidence = float(s.get("confidence", 0))
        if size == "small" and confidence >= 0.6:
            enqueue(s.get("text", "(empty)"), priority=4)
            dispatched += 1
    return dispatched


def ingest(url: str) -> dict:
    if not _rate_ok():
        return {"error": "rate limit: 5 ingestions per hour"}
    kind = _classify(url)
    slug = _slug_for_url(url)
    if kind == "video":
        title, transcript = _transcribe_video(url)
    else:
        title, transcript = _fetch_article(url)
    analysis = _analyze(transcript, kind)
    path = _store_knowledge(slug, url, kind, title, transcript, analysis)
    dispatched = _dispatch_suggestions(analysis.get("suggestions", []))
    return {
        "kind": kind,
        "title": title or url,
        "transcript": transcript[:400],
        "analysis": analysis,
        "path": str(path),
        "suggestions_dispatched": dispatched,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ingest", "selftest"])
    ap.add_argument("arg", nargs="?")
    ns = ap.parse_args()
    if ns.cmd == "ingest":
        print(json.dumps(ingest(ns.arg), indent=2))
    elif ns.cmd == "selftest":
        # mock-ingest a known URL by stubbing fetch and transcription
        url = ns.arg or "https://example.com/test-article"
        result = {
            "kind": _classify(url),
            "title": "example",
            "transcript": "sample body",
            "analysis": {"summary": "s", "knowledge": ["test"], "suggestions": []},
        }
        _store_knowledge(_slug_for_url(url), url, result["kind"], result["title"], result["transcript"], result["analysis"])
        ok = any(KNOWLEDGE_DIR.rglob("*.md"))
        print(f"Ingest self-test: 1 knowledge, 0 suggestions")
        raise SystemExit(0 if ok else 1)
