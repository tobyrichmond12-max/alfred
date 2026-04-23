"""Alfred unified server, web UI + API on one port."""
import asyncio
import os
import sys
import json
import time
from aiohttp import web

sys.path.insert(0, os.path.dirname(__file__))

_env_path = '/mnt/nvme/alfred/.env'
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

from conversation import chat, reset
from nudge import log_activity, get_pending_nudges, get_activity_summary
from voice import get_tts_audio
from database import init_databases

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web')
AUDIO_DIR = '/mnt/nvme/alfred/audio'
os.makedirs(AUDIO_DIR, exist_ok=True)

async def index(request):
    return web.FileResponse(os.path.join(WEB_DIR, 'index.html'))

async def handle_activity(request):
    """Log an activity event from iOS Shortcut automation."""
    try:
        body = await request.json()
    except Exception:
        return web.Response(text="Invalid JSON", status=400)
    
    activity_type = body.get("type", "unknown")
    detail = body.get("detail", "")
    
    entry = log_activity(activity_type, detail, source=body.get("source", "ios"))
    return web.json_response({"status": "logged", "entry": entry})


async def handle_nudges(request):
    """Get any pending nudges that should be delivered."""
    nudges = get_pending_nudges()
    return web.json_response({"nudges": nudges})


async def handle_ask(request):
    """Fast text-only endpoint for iOS Shortcuts, uses Haiku for speed."""
    try:
        body = await request.json()
    except Exception:
        return web.Response(text="Invalid JSON", status=400)
    text = body.get("text", "").strip()
    if not text:
        return web.Response(text="No text", status=400)
    loop = asyncio.get_event_loop()
    # Use fast_chat which uses Haiku instead of Sonnet
    from conversation import fast_chat
    response = await loop.run_in_executor(None, fast_chat, text)
    return web.json_response({"text": response})


async def handle_chat(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    text = body.get("text", "").strip()
    if not text:
        return web.json_response({"error": "No text"}, status=400)
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, chat, text)
    audio_url = None
    try:
        audio = await loop.run_in_executor(None, get_tts_audio, response)
        if audio:
            ts = str(int(time.time()))
            audio_path = os.path.join(AUDIO_DIR, f"resp_{ts}.mp3")
            with open(audio_path, "wb") as f:
                f.write(audio)
            audio_url = f"/audio/resp_{ts}.mp3"
    except Exception as e:
        print(f"TTS error: {e}")
    return web.json_response({"text": response, "audio_url": audio_url})

async def handle_audio(request):
    filename = request.match_info['filename']
    audio_path = os.path.join(AUDIO_DIR, filename)
    if os.path.exists(audio_path):
        return web.FileResponse(audio_path, headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-cache"
        })
    return web.json_response({"error": "No audio"}, status=404)

async def handle_status(request):
    return web.json_response({
        "status": "operational",
        "service": "Alfred",
        "time": time.strftime("%Y-%m-%d %H:%M:%S")
    })

async def handle_reset(request):
    reset()
    return web.json_response({"status": "Conversation reset, sir."})

async def start_web_server(host="0.0.0.0", port=8080):
    init_databases()
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/chat", handle_chat)
    app.router.add_post("/ask", handle_ask)
    app.router.add_get("/audio/{filename}", handle_audio)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/reset", handle_reset)
    app.router.add_post("/activity", handle_activity)
    app.router.add_get("/nudges", handle_nudges)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"Alfred running at http://{host}:{port}")
    return runner

if __name__ == "__main__":
    async def main():
        await start_web_server()
        await asyncio.Future()
    asyncio.run(main())
