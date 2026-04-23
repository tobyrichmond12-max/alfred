import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import unquote_plus
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ALFRED_HOME = "/mnt/nvme/alfred"
VAULT_DIR = os.path.join(ALFRED_HOME, "vault")
STATIC_DIR = os.path.join(ALFRED_HOME, "bridge", "static")
PUSH_SUBSCRIPTIONS_PATH = os.path.join(ALFRED_HOME, "data", "push_subscriptions.json")
CLAUDE_BIN = "/home/thoth/.local/bin/claude"
REQUEST_TIMEOUT_S = 120
EASTERN = ZoneInfo("America/New_York")

CONV_FILENAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})\.md$")

WHISPER_MODEL_SIZE = os.environ.get("ALFRED_WHISPER_MODEL", "base")
_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper_model():
    """Lazy-load the faster-whisper model on first use."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel

            logger.info("Loading faster-whisper model: %s", WHISPER_MODEL_SIZE)
            _whisper_model = WhisperModel(
                WHISPER_MODEL_SIZE,
                device="cpu",
                compute_type="int8",
            )
            logger.info("Whisper model ready")
    return _whisper_model


def _transcribe_audio(path: str) -> str:
    model = _get_whisper_model()
    segments, _info = model.transcribe(path, beam_size=1, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()

sys.path.insert(0, os.path.join(ALFRED_HOME, "core"))
from session import get_session_info, touch_session  # noqa: E402
from state import load_state as load_state_dict, staleness_warning  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("alfred.bridge")

app = FastAPI(title="Alfred Voice Bridge")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    body = await request.body()
    headers = dict(request.headers)
    try:
        body_text = body.decode("utf-8")
    except UnicodeDecodeError:
        body_text = f"<{len(body)} bytes binary>"
    logger.info(
        "REQ %s %s headers=%s body=%s",
        request.method,
        request.url,
        headers,
        body_text,
    )

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive
    return await call_next(request)


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str
    duration_ms: int


@app.get("/health")
def health():
    """Liveness plus a quick staleness readout for external monitors."""
    from state import staleness_minutes as _stale_minutes  # noqa: WPS433

    state = load_state_dict()
    age_min = _stale_minutes(state)
    return {
        "ok": True,
        "state_as_of": state.get("as_of"),
        "state_age_minutes": round(age_min, 1) if age_min is not None else None,
        "state_stale": bool(age_min is not None and age_min >= 30),
    }


@app.get("/test")
def test():
    return {"response": "Alfred is alive"}


@app.get("/brief")
def brief():
    """Return the latest reflection's observations as plain text for voice readout."""
    refl_dir = os.path.join(VAULT_DIR, "reflections")
    try:
        files = sorted(
            f for f in os.listdir(refl_dir)
            if f.endswith(".md")
            and not f.startswith("weekly-review-")
            and f != "skill-candidates.md"
        )
    except FileNotFoundError:
        files = []
    if not files:
        return PlainTextResponse("Nothing flagged.")

    with open(os.path.join(refl_dir, files[-1])) as f:
        raw = f.read()

    if raw.startswith("---\n"):
        parts = raw.split("\n---\n", 1)
        if len(parts) == 2:
            raw = parts[1]

    lines = raw.lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return PlainTextResponse("\n".join(lines).strip() or "Nothing flagged.")


def _reverse_geocode(lat: float, lon: float) -> str:
    try:
        import urllib.request
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "Alfred/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        addr = data.get("address", {})
        parts = [
            addr.get("amenity") or addr.get("building") or addr.get("road"),
            addr.get("neighbourhood") or addr.get("suburb"),
            addr.get("city") or addr.get("town") or addr.get("village"),
        ]
        return ", ".join(p for p in parts if p) or data.get("display_name", "")[:60]
    except Exception:
        return f"{lat:.4f}, {lon:.4f}"


TELEGRAM_OWNER_PATHS = [
    os.path.join(ALFRED_HOME, "data", "telegram_owner.json"),
    os.path.join(ALFRED_HOME, "data", "alfred", "owner.json"),
    "/var/lib/alfred/owner.json",
]


def _load_telegram_token() -> str | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token
    env_path = os.path.join(ALFRED_HOME, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _load_telegram_chat_id() -> int | None:
    for path in TELEGRAM_OWNER_PATHS:
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        chat_id = data.get("chat_id")
        if chat_id is None:
            continue
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            continue
    return None


def _telegram_send(token: str, chat_id: int, text: str, reply_to: int | None = None) -> int | None:
    import urllib.parse
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    params: dict = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": "true",
    }
    if reply_to is not None:
        params["reply_to_message_id"] = reply_to
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode())
    if not payload.get("ok"):
        return None
    return (payload.get("result") or {}).get("message_id")


def _mirror_to_telegram(user_msg: str, alfred_response: str) -> None:
    try:
        token = _load_telegram_token()
        chat_id = _load_telegram_chat_id()
        if not token or chat_id is None:
            return
        first_id = _telegram_send(token, chat_id, f"Voice: {user_msg}")
        _telegram_send(token, chat_id, alfred_response, reply_to=first_id)
    except Exception as exc:
        logger.debug("telegram mirror skipped: %s", exc)


def mirror_to_telegram_async(user_msg: str, alfred_response: str) -> None:
    t = threading.Thread(
        target=_mirror_to_telegram,
        args=(user_msg, alfred_response),
        daemon=True,
    )
    t.start()


def log_conversation(
    user_msg: str,
    alfred_response: str,
    duration_ms: int,
    source: str = "voice",
):
    try:
        now = datetime.now(EASTERN)
        date_str = now.strftime("%Y-%m-%d")
        year, month = now.strftime("%Y"), now.strftime("%m")
        time_slug = now.strftime("%H%M%S")

        conv_dir = os.path.join(VAULT_DIR, "conversations", year, month)
        os.makedirs(conv_dir, exist_ok=True)

        source_note = "\n*Voice memo (PWA).*\n\n" if source == "voice-memo" else ""
        filepath = os.path.join(conv_dir, f"{date_str}-{time_slug}.md")
        content = (
            f"---\n"
            f"date: {date_str}\n"
            f"time: \"{now.strftime('%H:%M')}\"\n"
            f"duration_ms: {duration_ms}\n"
            f"source: {source}\n"
            f"tags: [conversation, {source}]\n"
            f"---\n\n"
            f"# {date_str} {now.strftime('%H:%M')}\n"
            f"{source_note}"
            f"\n**User:** {user_msg}\n\n"
            f"**Alfred:** {alfred_response}\n\n"
            f"---\n"
            f"← [[journal/{year}/{month}/{date_str}]]\n"
        )
        with open(filepath, "w") as f:
            f.write(content)
    except Exception as e:
        logger.warning("Failed to log conversation: %s", e)


def _summarize_session(session_data: dict):
    """Background: resume expired session, generate summary, save to vault."""
    try:
        session_id = session_data["session_id"]
        started_str = session_data.get("started_at") or session_data.get("last_activity")
        started = datetime.fromisoformat(started_str).astimezone(EASTERN)

        date_str = started.strftime("%Y-%m-%d")
        year, month = started.strftime("%Y"), started.strftime("%m")
        time_slug = started.strftime("%H%M%S")

        prompt = (
            "Please summarize this conversation in 2-3 concise sentences for my personal journal. "
            "Focus on what was discussed and any decisions or action items."
        )

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        core_path = os.path.join(ALFRED_HOME, "core")
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{core_path}:{existing_pp}" if existing_pp else core_path
        result = subprocess.run(
            [CLAUDE_BIN, "-p", "--resume", session_id],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=ALFRED_HOME,
            timeout=60,
            env=env,
        )

        if result.returncode != 0 or not result.stdout.strip():
            logger.warning("Session summary failed for %s: %s", session_id, result.stderr[:200])
            return

        summary = result.stdout.strip()
        turn_count = session_data.get("turn_count", 0)

        conv_dir = os.path.join(VAULT_DIR, "conversations", year, month)
        os.makedirs(conv_dir, exist_ok=True)
        filepath = os.path.join(conv_dir, f"{date_str}-{time_slug}-summary.md")

        content = (
            f"---\n"
            f"date: {date_str}\n"
            f"time: \"{started.strftime('%H:%M')}\"\n"
            f"session_id: {session_id}\n"
            f"turn_count: {turn_count}\n"
            f"tags: [conversation, summary]\n"
            f"---\n\n"
            f"# Session Summary, {date_str} {started.strftime('%-I:%M %p')}\n\n"
            f"{summary}\n\n"
            f"---\n"
            f"← [[journal/{year}/{month}/{date_str}]]\n"
        )
        with open(filepath, "w") as f:
            f.write(content)

        logger.info("Session summary saved: %s (%d turns)", filepath, turn_count)
    except Exception as e:
        logger.warning("Session summarization error: %s", e)


def _summarize_expired_session_async(session_data: dict):
    t = threading.Thread(target=_summarize_session, args=(session_data,), daemon=True)
    t.start()


def load_state() -> str:
    state_path = os.path.join(ALFRED_HOME, "current_state.json")
    try:
        with open(state_path) as f:
            return f.read()
    except OSError:
        return ""


def _current_staleness_warning() -> str:
    """Return the staleness warning for the current state, or ""."""
    return staleness_warning(load_state_dict())


def run_claude(message: str) -> ChatResponse:
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="empty message")

    session_info = get_session_info()
    session_id = session_info["session_id"]
    is_new = session_info["is_new"]
    expired_session = session_info["expired_session"]

    if expired_session:
        _summarize_expired_session_async(expired_session)

    state = load_state()
    if state:
        warning = _current_staleness_warning()
        prefix = f"Current state:\n{state}\n\n"
        if warning:
            prefix += f"{warning}\n\n"
        message = prefix + message

    session_flag = ["--session-id", session_id] if is_new else ["--resume", session_id]

    # Claude CLI prefers ANTHROPIC_API_KEY over its stored login session.
    # Strip it so we always use the interactive login under ~/.claude/.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    # Let Alfred import core modules without sys.path gymnastics.
    core_path = os.path.join(ALFRED_HOME, "core")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{core_path}:{existing_pp}" if existing_pp else core_path

    start = time.monotonic()
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p"] + session_flag,
            input=message,
            capture_output=True,
            text=True,
            cwd=ALFRED_HOME,
            timeout=REQUEST_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="claude timed out")

    duration_ms = int((time.monotonic() - start) * 1000)

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()[-500:]
        raise HTTPException(
            status_code=500,
            detail=f"claude exited {result.returncode}: {stderr_tail}",
        )

    started_at = datetime.now(EASTERN).isoformat() if is_new else None
    touch_session(session_id, started_at=started_at)

    return ChatResponse(response=result.stdout.strip(), duration_ms=duration_ms)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    return run_claude(req.message)


@app.get("/chat", response_model=ChatResponse)
def chat_get(message: str):
    return run_claude(unquote_plus(message))


@app.get("/ask/{path_message:path}")
def ask_get_path(path_message: str, message: str = None):
    text = _normalize_user_text(path_message) or (
        _normalize_user_text(message) if message else ""
    )
    if not text:
        text = "Greet me briefly."
    result = run_claude(text)
    log_conversation(text, result.response, result.duration_ms)
    mirror_to_telegram_async(text, result.response)
    return PlainTextResponse(result.response)


@app.get("/ask")
def ask_get(message: str = None):
    text = _normalize_user_text(message) if message else ""
    if not text:
        text = "Greet me briefly."
    result = run_claude(text)
    log_conversation(text, result.response, result.duration_ms)
    mirror_to_telegram_async(text, result.response)
    return PlainTextResponse(result.response)


@app.post("/location")
async def update_location(request: Request):
    body = await request.body()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="body must be JSON")

    lat = data.get("latitude")
    lon = data.get("longitude")
    if lat is None or lon is None:
        raise HTTPException(status_code=400, detail="latitude and longitude required")

    state_path = os.path.join(ALFRED_HOME, "current_state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        state = {}

    # Reverse geocode for human-readable place name
    place_name = _reverse_geocode(lat, lon)

    state.setdefault("user", {})["location"] = {
        "latitude": lat,
        "longitude": lon,
        "accuracy_m": data.get("accuracy"),
        "place": place_name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_path)

    logger.info("Location updated: %.4f, %.4f", lat, lon)
    return {"ok": True}


def _normalize_user_text(raw: str) -> str:
    """Strip the form-encoded ``message=`` prefix the iOS Shortcut sends, then url-decode."""
    if raw is None:
        return ""
    text = raw.strip()
    if text.startswith("message="):
        text = unquote_plus(text[len("message=") :])
    text = text.strip()
    # A dictated empty string still arrives as the literal placeholder
    # "message=" or "message=%0A" after url-decoding. Treat those as empty.
    if text.lower() in {"message=", "message"}:
        return ""
    return text


@app.post("/ask")
async def ask(request: Request):
    body = await request.body()
    try:
        raw = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="body must be UTF-8 text")
    content_type = request.headers.get("content-type", "").lower()
    if "application/x-www-form-urlencoded" in content_type or raw.strip().startswith("message="):
        message = _normalize_user_text(raw)
    else:
        message = raw
    if not message.strip():
        raise HTTPException(status_code=400, detail="empty message")
    result = run_claude(message)
    log_conversation(message, result.response, result.duration_ms)
    mirror_to_telegram_async(message, result.response)
    return PlainTextResponse(result.response)


class PwaMessageResponse(BaseModel):
    response: str
    duration_ms: int
    timestamp: str


class PwaHistoryMessage(BaseModel):
    role: str
    text: str
    timestamp: str


class PwaHistoryResponse(BaseModel):
    messages: list[PwaHistoryMessage]


def _extract_turns(md: str) -> tuple[str | None, str | None]:
    """Pull the user message and Alfred response out of a conversation note."""
    user = None
    alfred = None
    if "**User:**" in md:
        after = md.split("**User:**", 1)[1]
        user_block = after.split("**Alfred:**", 1)[0]
        user = user_block.strip().rstrip("-").strip() or None
    if "**Alfred:**" in md:
        after = md.split("**Alfred:**", 1)[1]
        alfred_block = after.split("\n---\n", 1)[0]
        alfred = alfred_block.strip() or None
    return user, alfred


def _collect_conversation_files(limit_files: int) -> list[str]:
    conv_root = os.path.join(VAULT_DIR, "conversations")
    if not os.path.isdir(conv_root):
        return []
    paths: list[str] = []
    for year in sorted(os.listdir(conv_root), reverse=True):
        year_dir = os.path.join(conv_root, year)
        if not os.path.isdir(year_dir):
            continue
        for month in sorted(os.listdir(year_dir), reverse=True):
            month_dir = os.path.join(year_dir, month)
            if not os.path.isdir(month_dir):
                continue
            for name in sorted(os.listdir(month_dir), reverse=True):
                if not CONV_FILENAME_RE.match(name):
                    continue
                paths.append(os.path.join(month_dir, name))
                if len(paths) >= limit_files:
                    return paths
    return paths


@app.post("/api/message", response_model=PwaMessageResponse)
def pwa_message(req: ChatRequest):
    result = run_claude(req.message)
    log_conversation(req.message, result.response, result.duration_ms, source="text")
    return PwaMessageResponse(
        response=result.response,
        duration_ms=result.duration_ms,
        timestamp=datetime.now(EASTERN).isoformat(),
    )


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscription(BaseModel):
    endpoint: str
    keys: PushKeys
    expirationTime: int | None = None


@app.get("/api/push/public-key")
def push_public_key():
    key = os.environ.get("VAPID_PUBLIC_KEY", "")
    if not key:
        # Fall back to reading .env directly in case the service was started
        # before VAPID_PUBLIC_KEY was added.
        env_path = os.path.join(ALFRED_HOME, ".env")
        try:
            with open(env_path) as f:
                for line in f:
                    if line.startswith("VAPID_PUBLIC_KEY="):
                        key = line.split("=", 1)[1].strip()
                        break
        except OSError:
            pass
    if not key:
        raise HTTPException(status_code=503, detail="VAPID public key not configured")
    return {"public_key": key}


@app.post("/api/push/subscribe")
def push_subscribe(sub: PushSubscription):
    payload = sub.model_dump(exclude_none=True)
    os.makedirs(os.path.dirname(PUSH_SUBSCRIPTIONS_PATH), exist_ok=True)
    try:
        with open(PUSH_SUBSCRIPTIONS_PATH) as f:
            existing = json.load(f)
        if not isinstance(existing, list):
            existing = []
    except (OSError, json.JSONDecodeError):
        existing = []

    # Dedupe by endpoint. Replace the matching entry so refreshed keys stick.
    existing = [s for s in existing if s.get("endpoint") != payload["endpoint"]]
    existing.append(payload)

    tmp = PUSH_SUBSCRIPTIONS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f, indent=2)
    os.replace(tmp, PUSH_SUBSCRIPTIONS_PATH)
    logger.info("push subscription stored (%d total)", len(existing))
    return {"ok": True, "count": len(existing)}


class PwaVoiceMemoResponse(BaseModel):
    transcription: str
    response: str
    duration_ms: int
    transcribe_ms: int
    timestamp: str


@app.post("/api/voice-memo", response_model=PwaVoiceMemoResponse)
async def pwa_voice_memo(audio: UploadFile = File(...)):
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio upload")

    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(prefix="alfred-vm-", suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        transcribe_start = time.monotonic()
        try:
            transcription = _transcribe_audio(tmp_path)
        except Exception as e:
            logger.exception("Whisper transcription failed")
            raise HTTPException(status_code=500, detail=f"transcription failed: {e}")
        transcribe_ms = int((time.monotonic() - transcribe_start) * 1000)

        if not transcription:
            raise HTTPException(status_code=422, detail="no speech detected")

        result = run_claude(transcription)
        log_conversation(transcription, result.response, result.duration_ms, source="voice-memo")
        return PwaVoiceMemoResponse(
            transcription=transcription,
            response=result.response,
            duration_ms=result.duration_ms,
            transcribe_ms=transcribe_ms,
            timestamp=datetime.now(EASTERN).isoformat(),
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.get("/api/history", response_model=PwaHistoryResponse)
def pwa_history(limit: int = 50):
    limit = max(1, min(limit, 200))
    # One conversation file holds one user message + one Alfred response.
    files = list(reversed(_collect_conversation_files(limit)))
    messages: list[PwaHistoryMessage] = []
    for path in files:
        try:
            with open(path) as f:
                md = f.read()
        except OSError:
            continue
        match = CONV_FILENAME_RE.match(os.path.basename(path))
        if not match:
            continue
        y, mo, d, h, mi, s = match.groups()
        ts = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=EASTERN).isoformat()
        user, alfred = _extract_turns(md)
        if user:
            messages.append(
                PwaHistoryMessage(role="user", text=_normalize_user_text(user), timestamp=ts)
            )
        if alfred:
            messages.append(PwaHistoryMessage(role="alfred", text=alfred, timestamp=ts))
    return PwaHistoryResponse(messages=messages[-limit * 2 :])


# Serve PWA static assets (built by Vite into bridge/static).
if os.path.isdir(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/manifest.webmanifest")
    def pwa_manifest():
        return FileResponse(os.path.join(STATIC_DIR, "manifest.webmanifest"))

    @app.get("/sw.js")
    def pwa_sw():
        return FileResponse(
            os.path.join(STATIC_DIR, "sw.js"),
            media_type="application/javascript",
        )

    @app.get("/icon-192.png")
    def pwa_icon_192():
        return FileResponse(os.path.join(STATIC_DIR, "icon-192.png"))

    @app.get("/icon-512.png")
    def pwa_icon_512():
        return FileResponse(os.path.join(STATIC_DIR, "icon-512.png"))

    @app.get("/icon-512-maskable.png")
    def pwa_icon_512_maskable():
        return FileResponse(os.path.join(STATIC_DIR, "icon-512-maskable.png"))

    @app.get("/")
    def pwa_root():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ---- G2 HUD (phase 4) -------------------------------------------------------

try:
    from hud import (  # type: ignore
        dashboard_snapshot,
        read_feed,
        reading_queue,
        register_sse_queue,
        unregister_sse_queue,
    )
except Exception as _hud_exc:
    logger.warning("hud module unavailable: %s", _hud_exc)
    dashboard_snapshot = None  # type: ignore
    read_feed = None  # type: ignore
    reading_queue = None  # type: ignore


@app.get("/hud")
def hud_shell():
    path = os.path.join(STATIC_DIR, "hud.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="hud not deployed")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=600"})


@app.get("/api/hud/dashboard")
def hud_dashboard():
    if dashboard_snapshot is None:
        raise HTTPException(status_code=503, detail="hud unavailable")
    return dashboard_snapshot()


@app.get("/api/hud/feed")
def hud_feed_endpoint(since: str | None = None, limit: int = 100):
    if read_feed is None:
        raise HTTPException(status_code=503, detail="hud unavailable")
    items = read_feed(since_iso=since, limit=limit)
    last_seen = items[-1]["ts"] if items else None
    return {"items": items, "last_seen": last_seen}


@app.get("/api/hud/reading")
def hud_reading():
    if reading_queue is None:
        raise HTTPException(status_code=503, detail="hud unavailable")
    return {"items": reading_queue()}


@app.get("/api/hud/stream")
def hud_stream():
    if register_sse_queue is None:
        raise HTTPException(status_code=503, detail="hud unavailable")
    from fastapi.responses import StreamingResponse

    q = register_sse_queue()

    def gen():
        try:
            # kick with a hello so clients render something fast
            yield "event: hello\ndata: {}\n\n"
            last_keepalive = time.time()
            while True:
                try:
                    payload = q.get(timeout=10)
                except Exception:
                    payload = None
                if payload is not None:
                    event = payload.get("event", "message")
                    data = json.dumps(payload.get("data", {}))
                    yield f"event: {event}\ndata: {data}\n\n"
                else:
                    # periodic comment to keep the connection alive
                    now = time.time()
                    if now - last_keepalive >= 30:
                        yield ": keepalive\n\n"
                        last_keepalive = now
        finally:
            unregister_sse_queue(q)

    return StreamingResponse(gen(), media_type="text/event-stream")
