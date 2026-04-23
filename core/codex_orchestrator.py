"""Codex task queue + runner + reviewer.

Public API:
    enqueue(task, priority=5) -> task_id
    run_next()                 -> dict or None
    review(task_id)            -> dict
    loop(interval_seconds=60)  -> never returns

The queue is a single jsonl at /var/lib/alfred/codex_queue.jsonl with
one entry per line. Runs materialize under /var/lib/alfred/codex_runs/<id>/.

Dispatch prefers `codex exec`; if the binary is unavailable, it falls
back to `claude -p` with a coding-agent system prompt. Review always
goes through `claude -p` so Opus judges Codex output.

State fields per entry:
    task_id, task, priority, status, created_at, started_at, ended_at,
    output_path, review_path, review_status
status values: pending, running, complete, failed, blocked
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("alfred.codex")

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

QUEUE_PATH = Path(os.environ.get(
    "CODEX_QUEUE_PATH", "/var/lib/alfred/codex_queue.jsonl",
))
RUNS_DIR = Path(os.environ.get(
    "CODEX_RUNS_DIR", "/var/lib/alfred/codex_runs",
))

# Fall back to repo-relative paths if /var/lib/alfred is not writable
if not os.access(QUEUE_PATH.parent, os.W_OK):
    QUEUE_PATH = Path(ALFRED_HOME) / "data" / "codex_queue.jsonl"
    RUNS_DIR = Path(ALFRED_HOME) / "data" / "codex_runs"

QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
RUNS_DIR.mkdir(parents=True, exist_ok=True)

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/thoth/.local/bin/claude")
RUN_TIMEOUT_S = int(os.environ.get("CODEX_RUN_TIMEOUT", "300"))

_file_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _read_queue() -> list[dict]:
    if not QUEUE_PATH.exists():
        return []
    entries = []
    for line in QUEUE_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _write_queue(entries: list[dict]) -> None:
    tmp = QUEUE_PATH.with_suffix(QUEUE_PATH.suffix + ".tmp")
    with tmp.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    os.replace(tmp, QUEUE_PATH)


def _maybe_announce(text: str) -> None:
    try:
        from hud import activity  # type: ignore

        activity(text)
    except Exception:
        pass


def _maybe_record_tokens(request_chars: int, response_chars: int) -> None:
    try:
        from token_tracker import record  # type: ignore

        record("codex", request_chars, response_chars)
    except Exception:
        pass


def enqueue(task: str, priority: int = 5) -> str:
    task = task.strip()
    if not task:
        raise ValueError("empty task")
    task_id = "cx_" + secrets.token_hex(6)
    entry = {
        "task_id": task_id,
        "task": task,
        "priority": int(priority),
        "status": "pending",
        "created_at": _now(),
    }
    with _file_lock:
        entries = _read_queue()
        entries.append(entry)
        _write_queue(entries)
    _maybe_announce(f"Enqueued Codex task {task_id}: {task[:60]}")
    return task_id


def _codex_available() -> bool:
    try:
        r = subprocess.run([CODEX_BIN, "--version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_with_codex(task: str, run_dir: Path) -> tuple[int, str, str]:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = subprocess.run(
            [CODEX_BIN, "exec", task],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
            cwd=ALFRED_HOME,
            env=env,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"codex timed out after {RUN_TIMEOUT_S}s"


def _run_with_claude(task: str, run_dir: Path) -> tuple[int, str, str]:
    prompt = (
        "You are a coding agent. Execute the following task and report what you "
        "did and any remaining questions. Be concise.\n\n"
        f"Task: {task}"
    )
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
            cwd=ALFRED_HOME,
            env=env,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"claude timed out after {RUN_TIMEOUT_S}s"


def _pop_top(entries: list[dict]) -> Optional[dict]:
    pending = [e for e in entries if e.get("status") == "pending"]
    if not pending:
        return None
    pending.sort(key=lambda e: (-int(e.get("priority", 5)), e.get("created_at", 0)))
    return pending[0]


def run_next() -> Optional[dict]:
    with _file_lock:
        entries = _read_queue()
        top = _pop_top(entries)
        if top is None:
            return None
        top["status"] = "running"
        top["started_at"] = _now()
        _write_queue(entries)

    task_id = top["task_id"]
    run_dir = RUNS_DIR / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task.txt").write_text(top["task"])

    _maybe_announce(f"Running Codex {task_id}")

    if _codex_available():
        rc, stdout, stderr = _run_with_codex(top["task"], run_dir)
        provider = "codex"
    else:
        rc, stdout, stderr = _run_with_claude(top["task"], run_dir)
        provider = "claude_p"

    (run_dir / "stdout.txt").write_text(stdout)
    (run_dir / "stderr.txt").write_text(stderr)
    (run_dir / "meta.json").write_text(json.dumps({
        "return_code": rc,
        "provider": provider,
        "ended_at": _now(),
    }))

    _maybe_record_tokens(len(top["task"]), len(stdout))

    with _file_lock:
        entries = _read_queue()
        for e in entries:
            if e.get("task_id") == task_id:
                e["status"] = "complete" if rc == 0 else "failed"
                e["ended_at"] = _now()
                e["output_path"] = str(run_dir)
                e["provider"] = provider
                break
        _write_queue(entries)

    _maybe_announce(f"Codex {task_id} {'done' if rc == 0 else 'failed'}")
    return {"task_id": task_id, "rc": rc, "run_dir": str(run_dir), "provider": provider}


def review(task_id: str) -> dict:
    run_dir = RUNS_DIR / task_id
    task_file = run_dir / "task.txt"
    stdout_file = run_dir / "stdout.txt"
    if not task_file.exists():
        return {"status": "blocked", "notes": f"run_dir missing {task_id}", "next_action": "enqueue again"}
    task = task_file.read_text()
    stdout = stdout_file.read_text() if stdout_file.exists() else ""
    stderr_file = run_dir / "stderr.txt"
    stderr = stderr_file.read_text() if stderr_file.exists() else ""

    prompt = (
        "Review this Codex run. Classify as: success / partial / failed / blocked. "
        'Return strict JSON {"status": "...", "notes": "...", "next_action": "..."}. '
        "No prose outside the JSON.\n\n"
        f"TASK:\n{task}\n\nSTDOUT:\n{stdout[:4000]}\n\nSTDERR:\n{stderr[:2000]}"
    )
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=ALFRED_HOME,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"status": "blocked", "notes": "review timed out", "next_action": "rerun review"}

    raw = (r.stdout or "").strip()
    (run_dir / "review.txt").write_text(raw)

    result: dict = {"status": "blocked", "notes": raw[:200], "next_action": "manual inspect"}
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(raw[start:end + 1])
            if isinstance(parsed, dict) and "status" in parsed:
                result = parsed
    except json.JSONDecodeError:
        pass

    with _file_lock:
        entries = _read_queue()
        for e in entries:
            if e.get("task_id") == task_id:
                e["review_status"] = result.get("status")
                e["review_path"] = str(run_dir / "review.txt")
                break
        _write_queue(entries)

    return result


def loop(interval_seconds: int = 60) -> None:
    log.info("codex_orchestrator: loop started, interval=%ds", interval_seconds)
    while True:
        result = run_next()
        if result is None:
            time.sleep(interval_seconds)
            continue
        review(result["task_id"])
        time.sleep(max(1, interval_seconds // 2))


def list_queue() -> list[dict]:
    return _read_queue()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["enqueue", "run_next", "review", "list", "loop"])
    ap.add_argument("arg", nargs="?")
    ns = ap.parse_args()

    if ns.cmd == "enqueue":
        print(enqueue(ns.arg or "print hello"))
    elif ns.cmd == "run_next":
        print(json.dumps(run_next() or {}, indent=2))
    elif ns.cmd == "review":
        print(json.dumps(review(ns.arg), indent=2))
    elif ns.cmd == "list":
        print(json.dumps(list_queue(), indent=2))
    elif ns.cmd == "loop":
        loop(int(ns.arg or 60))
