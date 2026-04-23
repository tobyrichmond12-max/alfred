"""Thin wrapper around `claude -p` for programmatic use.

The bridge already has its own `run_claude` tied to FastAPI. This module
exposes a simpler `(text, chat_id) -> str` interface for the Telegram bot
and any other caller that just wants a reply.

Session continuity is best-effort: we reuse the Alfred session_id so that
multi-turn conversations hold. Falls back to a fresh session if the
session module is missing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/thoth/.local/bin/claude")
REQUEST_TIMEOUT_S = 120

_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)


def _load_state() -> str:
    try:
        with open(os.path.join(ALFRED_HOME, "current_state.json")) as f:
            return f.read()
    except OSError:
        return ""


def _session_id_and_flag() -> tuple[list[str], Optional[str], bool]:
    """Return (cli_flag, session_id, is_new). Callers must touch_session after success."""
    try:
        from session import get_session_info  # type: ignore

        info = get_session_info()
        sid = info.get("session_id")
        if not sid:
            return [], None, False
        is_new = bool(info.get("is_new"))
        flag = ["--session-id", sid] if is_new else ["--resume", sid]
        return flag, sid, is_new
    except Exception:
        return [], None, False


def _touch_session(session_id: Optional[str], is_new: bool) -> None:
    if not session_id:
        return
    try:
        from session import touch_session  # type: ignore
        from datetime import datetime
        from zoneinfo import ZoneInfo

        started_at = datetime.now(ZoneInfo("America/New_York")).isoformat() if is_new else None
        touch_session(session_id, started_at=started_at)
    except Exception:
        pass


_response_cache: dict[tuple, tuple[float, str]] = {}
_cache_lock = threading.Lock()
_cache_hits = 0
_cache_misses = 0
_cache_last_report = 0
CACHE_TTL = 600.0  # 10 minutes, phase 6 may extend to 1800
CACHE_REPORT_EVERY = 100

_NO_CACHE_MARKERS = ("now", "today", "check", "what time")

_log = __import__("logging").getLogger("alfred.run_claude")


def _maybe_report_cache_stats() -> None:
    global _cache_last_report
    total = _cache_hits + _cache_misses
    if total == 0 or total - _cache_last_report < CACHE_REPORT_EVERY:
        return
    _cache_last_report = total
    rate = (_cache_hits / total) * 100 if total else 0.0
    _log.info("Cache hit rate: %.1f%% (%d/%d, size=%d)", rate, _cache_hits, total, len(_response_cache))


def _prefetch_context_parallel() -> dict:
    """Kick three context reads concurrently so the slowest one bounds us."""
    from concurrent.futures import ThreadPoolExecutor

    out: dict = {}

    def _cal():
        try:
            from alfred_calendar import next_event  # type: ignore

            out["next_event"] = next_event()
        except Exception:
            out["next_event"] = None

    def _tasks():
        try:
            from triage_todoist import get_overdue_tasks  # type: ignore

            out["overdue_count"] = len(get_overdue_tasks() or [])
        except Exception:
            out["overdue_count"] = 0

    def _journal():
        import os as _os
        j = _os.path.join(ALFRED_HOME, "vault", "journal")
        try:
            files = sorted(
                (
                    _os.path.join(root, f)
                    for root, _dirs, fs in _os.walk(j)
                    for f in fs
                    if f.endswith(".md")
                ),
                reverse=True,
            )
            if files:
                with open(files[0]) as fh:
                    out["journal_tail"] = fh.read()[-400:]
        except Exception:
            out["journal_tail"] = ""

    with ThreadPoolExecutor(max_workers=4) as ex:
        for fn in (_cal, _tasks, _journal):
            ex.submit(fn)
    return out

_CODING_MARKERS = (
    "write a", "write the", "refactor", "implement", "fix the bug",
    "add a function", "add a test", "add unit test", "build a",
    "create a script", "modify core/", "edit core/", "python function",
)


def _effective_ttl() -> float:
    try:
        from token_tracker import conservation_mode  # type: ignore

        if conservation_mode():
            return 1800.0  # 30 minutes in conservation
    except Exception:
        pass
    return CACHE_TTL


def _cache_get(key: tuple) -> Optional[str]:
    global _cache_hits, _cache_misses
    with _cache_lock:
        entry = _response_cache.get(key)
        if not entry:
            _cache_misses += 1
            _maybe_report_cache_stats()
            return None
        ts, value = entry
        if time.time() - ts > _effective_ttl():
            _response_cache.pop(key, None)
            _cache_misses += 1
            _maybe_report_cache_stats()
            return None
        _cache_hits += 1
        _maybe_report_cache_stats()
        return value


def _cache_put(key: tuple, value: str) -> None:
    with _cache_lock:
        _response_cache[key] = (time.time(), value)


def _maybe_record_tokens(prompt: str, response: str) -> None:
    try:
        from token_tracker import record  # type: ignore

        record("claude_p", len(prompt), len(response))
    except Exception:
        pass


def _maybe_announce(text: str, done: bool = False) -> None:
    try:
        from hud import activity  # type: ignore

        activity(text)
    except Exception:
        pass


class announce:
    """Context manager that emits start / done / failed activity lines.

    Usage:
        with announce("Extracting knowledge from co-op conversation"):
            do_thing()
    """

    def __init__(self, label: str):
        self.label = label

    def __enter__(self):
        _maybe_announce(self.label + "...")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            _maybe_announce(f"Done, {self.label.split()[0].lower()}")
        else:
            reason = str(exc)[:60] if exc else "error"
            _maybe_announce(f"Failed: {reason}")
        return False


def _fish_speak_prefix(style: str) -> str:
    if style == "fish":
        return (
            "Respond in fish speak. Rules: no filler words, no pleasantries, "
            "subject-verb-object only. Drop articles where natural. Numbers as "
            "digits. One sentence per fact. Max 3 sentences.\n\n"
        )
    return ""


def _pleasantry_strip(text: str) -> str:
    kept = []
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith(("sure", "of course", "absolutely", "happy to", "here is", "let me ")):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _looks_like_coding_task(text: str) -> bool:
    lower = text.lower()
    return any(m in lower for m in _CODING_MARKERS)


def _resolve_style(style: Optional[str], surface: str) -> str:
    if style in ("normal", "fish"):
        return style
    # defer to per-surface config from fishspeak_config.json
    try:
        import json
        p = os.path.join(ALFRED_HOME, "data", "fishspeak_config.json")
        with open(p) as f:
            cfg = json.load(f)
        if surface == "voice_memo" and cfg.get("voice_memo", True):
            return "fish"
        if surface == "telegram" and cfg.get("telegram", False):
            return "fish"
    except Exception:
        pass
    # phase 18: conservation mode forces fish
    try:
        from token_tracker import conservation_mode  # type: ignore

        if conservation_mode():
            return "fish"
    except Exception:
        pass
    return "normal"


def chat(text: str, chat_id: Optional[int] = None, *, style: Optional[str] = None, surface: str = "telegram") -> str:
    """Send text to `claude -p` with Alfred state and return the reply.

    `chat_id` is accepted for caller compatibility but not currently used in
    the prompt; session continuity happens via session_id.
    """
    text = (text or "").strip()
    if not text:
        return ""

    # Codex-first routing under conservation
    try:
        from token_tracker import conservation_mode  # type: ignore

        if conservation_mode() and _looks_like_coding_task(text):
            try:
                from codex_orchestrator import enqueue  # type: ignore

                tid = enqueue(text, priority=5)
                return (
                    "Running low on Claude capacity. Dispatched to Codex "
                    f"instead ({tid}). I will review the result."
                )
            except Exception:
                pass
    except Exception:
        pass

    effective_style = _resolve_style(style, surface)
    prefix = _fish_speak_prefix(effective_style)

    conservation = False
    try:
        from token_tracker import conservation_mode  # type: ignore

        conservation = conservation_mode()
    except Exception:
        pass

    state = _load_state()
    prefetch_start = time.time()
    prefetched = _prefetch_context_parallel()
    prefetch_ms = int((time.time() - prefetch_start) * 1000)
    _log.debug("prefetch: %dms (next_event=%s, overdue=%s, journal=%d chars)",
               prefetch_ms,
               bool(prefetched.get("next_event")),
               prefetched.get("overdue_count"),
               len(prefetched.get("journal_tail") or ""))

    prompt = ""
    if state:
        prompt += f"Current state:\n{state}\n\n"
    context_lines = []
    ne = prefetched.get("next_event")
    if ne:
        context_lines.append(f"Next event: {ne}")
    oc = prefetched.get("overdue_count")
    if oc:
        context_lines.append(f"Overdue tasks: {oc}")
    jt = prefetched.get("journal_tail")
    if jt:
        context_lines.append(f"Recent journal:\n{jt}")
    if context_lines:
        prompt += "Prefetched context:\n" + "\n".join(context_lines) + "\n\n"
    if conservation:
        prompt += (
            "Conservation mode: respond in at most 500 characters. "
            "Skip pleasantries. Cut filler. If a detail can be dropped without "
            "losing the answer, drop it.\n\n"
        )
    if prefix:
        prompt += prefix
    prompt += text

    cache_key = (text.lower(), effective_style)
    if not any(m in text.lower() for m in _NO_CACHE_MARKERS):
        cached = _cache_get(cache_key)
        if cached is not None:
            _maybe_record_tokens(prompt, cached)
            return cached

    session_flag, session_id, session_is_new = _session_id_and_flag()

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    core_path = os.path.join(ALFRED_HOME, "core")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{core_path}:{existing_pp}" if existing_pp else core_path

    _maybe_announce(f"Asking claude: {text[:50]}")

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p"] + session_flag,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=ALFRED_HOME,
            timeout=REQUEST_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        _maybe_announce("Claude timed out")
        return "Claude timed out."
    except FileNotFoundError:
        return f"Claude CLI not found at {CLAUDE_BIN}."

    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-200:]
        _maybe_announce(f"Claude failed: {tail[:60]}")
        return f"Claude errored: {tail}"

    reply = (result.stdout or "").strip()
    if effective_style == "fish":
        reply = _pleasantry_strip(reply)

    _maybe_record_tokens(prompt, reply)
    _cache_put(cache_key, reply)
    _touch_session(session_id, session_is_new)
    _maybe_announce(f"Claude replied ({len(reply)} chars)", done=True)
    try:
        from relationships import passive_update  # type: ignore

        threading.Thread(target=passive_update, args=(text, reply), daemon=True).start()
    except Exception:
        pass
    return reply


def cache_stats() -> dict:
    with _cache_lock:
        total = _cache_hits + _cache_misses
        return {
            "hits": _cache_hits,
            "misses": _cache_misses,
            "size": len(_response_cache),
            "hit_rate": round(_cache_hits / total, 3) if total else 0.0,
        }


if __name__ == "__main__":
    import sys as _sys

    q = " ".join(_sys.argv[1:]) or "say hi in five words"
    print(chat(q))
