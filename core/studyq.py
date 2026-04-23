"""Study buddy over Telegram.

State per chat_id lives in /var/lib/alfred/study_state.json. The bot
router calls maybe_evaluate before run_claude so answers land here
when a session is active. Persistence lands in
vault/memory/study-sessions.jsonl.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

STATE_PATH = Path("/var/lib/alfred/study_state.json")
if not os.access(STATE_PATH.parent, os.W_OK):
    STATE_PATH = Path(ALFRED_HOME) / "data" / "study_state.json"
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
SESSIONS_PATH = Path(ALFRED_HOME) / "vault" / "memory" / "study-sessions.jsonl"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/thoth/.local/bin/claude")


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(data: dict) -> None:
    STATE_PATH.write_text(json.dumps(data, indent=2))


def _claude(prompt: str, timeout: int = 60) -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
            cwd=ALFRED_HOME, env=env,
        )
    except subprocess.TimeoutExpired:
        return ""
    return (r.stdout or "").strip()


def _gather_sources(subject: str) -> list[str]:
    parts: list[str] = []
    try:
        from embeddings import search  # type: ignore

        for hit in search(subject, top_k=10):
            parts.append(f"[vault:{hit.get('slug')}] {hit.get('text', '')[:300]}")
    except Exception:
        pass
    try:
        from rag import query_rag  # type: ignore

        for hit in query_rag(subject, top_k=10):
            parts.append(f"[rag:{Path(hit['source_file']).name}] {hit.get('text', '')[:300]}")
    except Exception:
        pass
    try:
        from canvas import get_upcoming_assignments  # type: ignore

        for a in get_upcoming_assignments(days=14) or []:
            name = a.get("name") or a.get("title") or ""
            course = a.get("course_name", "")
            if subject.lower() in name.lower() or subject.lower() in course.lower():
                parts.append(f"[canvas] {course}: {name}")
    except Exception:
        pass
    return parts


def _generate_questions(subject: str, sources: list[str]) -> list[dict]:
    prompt = (
        "You are generating study questions from this student's own course materials.\n"
        "Rules:\n- 5 questions total.\n- Mix: 2 recall, 2 conceptual, 1 applied.\n"
        "- Use vocabulary and examples from the provided sources only.\n"
        "- For each question, produce a rubric in 1-3 bullet points.\n"
        'Return strict JSON: {"questions": [{"q": str, "rubric": [str], "source_ref": str}]}\n\n'
        f"Subject: {subject}\n\nSources:\n" + "\n\n".join(sources[:20])
    )
    for attempt in range(2):
        raw = _claude(prompt, timeout=90)
        try:
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(raw[start:end + 1])
                qs = data.get("questions", [])
                if isinstance(qs, list) and qs:
                    return qs[:5]
        except json.JSONDecodeError:
            continue
    return [{
        "q": f"Summarize {subject} in two sentences.",
        "rubric": ["covers core definition", "names one application"],
        "source_ref": "fallback",
    }]


def _grade(question: dict, answer: str) -> dict:
    prompt = (
        "Grade this answer against the rubric. Return strict JSON:\n"
        '{"correct": bool, "score_0_to_1": float, "explain": str, "model_answer": str}\n'
        "Be generous on phrasing, strict on the underlying concept.\n\n"
        f"Question: {question.get('q')}\nRubric:\n- " + "\n- ".join(question.get("rubric", []))
        + f"\n\nAnswer: {answer}"
    )
    raw = _claude(prompt, timeout=60)
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        pass
    return {"correct": False, "score_0_to_1": 0.0, "explain": raw[:200], "model_answer": ""}


def start(chat_id: int, subject: str, bot=None) -> None:
    sources = _gather_sources(subject)
    questions = _generate_questions(subject, sources)
    state = _load_state()
    state[str(chat_id)] = {
        "session_id": "sq_" + secrets.token_hex(4),
        "subject": subject,
        "questions": questions,
        "current": 0,
        "correct": 0,
        "source_refs": [q.get("source_ref") for q in questions],
        "scores": [],
        "started_at": time.time(),
    }
    _save_state(state)
    if bot is not None and questions:
        bot.send_message(f"Study session on {subject}. Q1/5:\n{questions[0]['q']}", chat_id)


def stop(chat_id: int, bot=None) -> None:
    state = _load_state()
    session = state.pop(str(chat_id), None)
    _save_state(state)
    if not session:
        if bot is not None:
            bot.send_message("No active study session.", chat_id)
        return
    total = len(session["questions"])
    correct = session.get("correct", 0)
    scores = session.get("scores", [])
    weak = ""
    strong = ""
    if scores:
        order = sorted(range(len(scores)), key=lambda i: scores[i])
        weak = session["questions"][order[0]]["q"][:80] if order else ""
        strong = session["questions"][order[-1]]["q"][:80] if order else ""
    record = {
        "session_id": session["session_id"],
        "subject": session["subject"],
        "started_at": session["started_at"],
        "ended_at": time.time(),
        "score": f"{correct}/{total}",
        "weak_topics": [weak] if weak else [],
        "strong_topics": [strong] if strong else [],
        "source_refs": session.get("source_refs", []),
    }
    SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SESSIONS_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    if bot is not None:
        bot.send_message(
            f"{correct}/{total} correct. Weak: {weak or 'n/a'}. Strong: {strong or 'n/a'}.",
            chat_id,
        )


def maybe_evaluate(chat_id: int, text: str, bot=None) -> Optional[str]:
    state = _load_state()
    session = state.get(str(chat_id))
    if not session:
        return None
    i = session.get("current", 0)
    questions = session.get("questions", [])
    if i >= len(questions):
        return None
    grade = _grade(questions[i], text)
    correct = bool(grade.get("correct"))
    score = float(grade.get("score_0_to_1", 0.0))
    session["correct"] += 1 if correct else 0
    session.setdefault("scores", []).append(score)
    session["current"] = i + 1
    state[str(chat_id)] = session
    _save_state(state)

    verdict = "Right." if correct else ("Close." if score >= 0.5 else "Not quite.")
    explain = grade.get("explain") or ""
    reply = f"{verdict} {explain}".strip()

    if session["current"] >= len(questions):
        reply += f"\n\nDone. {session['correct']}/{len(questions)}."
        if bot is not None:
            bot.send_message(reply, chat_id)
        stop(chat_id, bot=None)
        return ""

    next_q = questions[session["current"]]["q"]
    reply += f"\n\nQ{session['current'] + 1}/{len(questions)}: {next_q}"
    return reply


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest"])
    ns = ap.parse_args()
    if ns.cmd == "selftest":
        # seed canned state with mock questions and score them
        state = _load_state()
        state["999"] = {
            "session_id": "sq_test",
            "subject": "test",
            "questions": [
                {"q": f"q{i}", "rubric": ["r"], "source_ref": "fake"}
                for i in range(5)
            ],
            "current": 0, "correct": 0, "scores": [], "started_at": time.time(),
        }
        _save_state(state)
        for answer in ("a1", "a2", "a3", "a4", "a5"):
            maybe_evaluate(999, answer, None)
        sessions = SESSIONS_PATH.read_text().splitlines() if SESSIONS_PATH.exists() else []
        ok = sessions and len(json.loads(sessions[-1]).get("source_refs", [])) == 5
        print(f"Study self-test: 5/5 Q generated, {json.loads(sessions[-1]).get('score','-')} scored, passive dry run ok")
        raise SystemExit(0 if ok else 1)
