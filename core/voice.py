"""Alfred voice system, TTS and audio streaming."""
import os
import json
import asyncio
from datetime import datetime
from config import ALFRED_HOME, LOGS_DIR

# Load .env file
_env_path = '/mnt/nvme/alfred/.env'
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ElevenLabs config. Set via .env, never hardcode the key here.
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ALFRED_VOICE_ID = os.environ.get("ALFRED_VOICE_ID", "")

def clean_for_speech(text):
    """Remove markdown, priority tags, and other non-speech elements."""
    import re
    text = re.sub(r'\*+\[?Priority:.*?\]?\*+', '', text)
    text = re.sub(r'\*+.*?\*+', '', text)  # Remove *bold* and **bold**
    text = re.sub(r'<[^>]+>', '', text)  # Remove XML-like tags
    text = re.sub(r'\[.*?\]', '', text)  # Remove [bracketed notes]
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def get_tts_audio(text, voice_id=None):
    """Convert text to speech using ElevenLabs. Returns audio bytes (mp3)."""
    if not ELEVENLABS_API_KEY:
        return None
    
    import urllib.request
    
    vid = voice_id or ALFRED_VOICE_ID
    if not vid:
        return None
    
    text = clean_for_speech(text)
    if not text:
        return None

    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {
            "stability": 0.6,
            "similarity_boost": 0.8,
            "style": 0.15,
            "use_speaker_boost": True
        },
        "speed": 2.0
    }
    
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "xi-api-key": ELEVENLABS_API_KEY
        }
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        print(f"TTS error: {e}")
        return None


def get_tts_audio_streaming(text, voice_id=None):
    """Stream TTS audio from ElevenLabs. Yields audio chunks for low-latency playback."""
    if not ELEVENLABS_API_KEY:
        return
    
    import urllib.request
    
    vid = voice_id or ALFRED_VOICE_ID
    if not vid:
        return
    
    text = clean_for_speech(text)
    if not text:
        return None

    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {
            "stability": 0.6,
            "similarity_boost": 0.8,
            "style": 0.15,
            "use_speaker_boost": True
        },
        "speed": 2.0
    }
    
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{vid}/stream",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "xi-api-key": ELEVENLABS_API_KEY
        }
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                yield chunk
    except Exception as e:
        print(f"TTS streaming error: {e}")


def split_into_sentences(text):
    """Split text into sentences for streaming TTS.
    Send each sentence to TTS as soon as it's ready rather than waiting for the full response."""
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return [s.strip() for s in sentences if s.strip()]


if __name__ == "__main__":
    if ELEVENLABS_API_KEY and ALFRED_VOICE_ID:
        print("Testing TTS...")
        audio = get_tts_audio("Good evening, sir. Alfred is at your service.")
        if audio:
            path = os.path.join(ALFRED_HOME, "test_audio.mp3")
            with open(path, "wb") as f:
                f.write(audio)
            print(f"Audio saved to {path} ({len(audio)} bytes)")
        else:
            print("TTS returned no audio")
    else:
        print("ElevenLabs not configured. Set ELEVENLABS_API_KEY and ALFRED_VOICE_ID.")
        print(f"  API key set: {bool(ELEVENLABS_API_KEY)}")
        print(f"  Voice ID set: {bool(ALFRED_VOICE_ID)}")
