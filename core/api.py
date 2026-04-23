"""Alfred HTTP API, for iOS Shortcuts and simple integrations.

POST /chat
  Body: {"text": "your message"}
  Response: {"text": "Alfred's response", "audio_url": "/audio/latest.mp3"}

GET /audio/latest.mp3
  Returns the most recent TTS audio file

GET /status
  Returns system status
"""
import asyncio
import json
import os
import sys
import time
from aiohttp import web

sys.path.insert(0, os.path.dirname(__file__))

# Load env
_env_path = '/mnt/nvme/alfred/.env'
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from conversation import chat, reset
from voice import get_tts_audio
from database import init_databases

AUDIO_DIR = '/mnt/nvme/alfred/audio'
os.makedirs(AUDIO_DIR, exist_ok=True)

async def handle_chat(request):
    """Handle a chat message. Returns text + generates audio."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    
    text = body.get("text", "").strip()
    if not text:
        return web.json_response({"error": "No text provided"}, status=400)
    
    # Get Alfred's response
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, chat, text)
    
    # Generate TTS audio
    audio_url = None
    try:
        audio = await loop.run_in_executor(None, get_tts_audio, response)
        if audio:
            audio_path = os.path.join(AUDIO_DIR, "latest.mp3")
            with open(audio_path, "wb") as f:
                f.write(audio)
            audio_url = "/audio/latest.mp3"
    except Exception as e:
        print(f"TTS error: {e}")
    
    return web.json_response({
        "text": response,
        "audio_url": audio_url
    })


async def handle_audio(request):
    """Serve the latest audio file."""
    audio_path = os.path.join(AUDIO_DIR, "latest.mp3")
    if os.path.exists(audio_path):
        return web.FileResponse(audio_path, headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-cache"
        })
    return web.json_response({"error": "No audio available"}, status=404)


async def handle_status(request):
    """Return system status."""
    return web.json_response({
        "status": "operational",
        "service": "Alfred",
        "time": time.strftime("%Y-%m-%d %H:%M:%S")
    })


async def handle_reset(request):
    """Reset conversation history."""
    reset()
    return web.json_response({"status": "Conversation reset, sir."})


def create_api_app():
    """Create the API web application."""
    app = web.Application()
    app.router.add_post('/chat', handle_chat)
    app.router.add_get('/audio/latest.mp3', handle_audio)
    app.router.add_get('/status', handle_status)
    app.router.add_post('/reset', handle_reset)
    return app


async def start_api_server(host='0.0.0.0', port=8081):
    """Start the API server."""
    app = create_api_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"Alfred API available at http://{host}:{port}")
    return runner


if __name__ == "__main__":
    init_databases()
    async def main():
        await start_api_server()
        await asyncio.Future()
    asyncio.run(main())
