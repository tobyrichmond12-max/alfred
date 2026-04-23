"""Singleton faster-whisper wrapper shared across callers.

The bridge has its own lazy loader at bridge/server.py. This one keeps an
independent singleton for the Telegram bot and any non-FastAPI caller.
Both are fine to coexist: they are separate processes. Within one
process, every call to `transcribe_file` reuses the same warmed model.
"""
from __future__ import annotations

import logging
import os
import threading
import wave

log = logging.getLogger("alfred.whisper")

_model = None
_model_lock = threading.Lock()

WHISPER_MODEL_SIZE = os.environ.get("ALFRED_WHISPER_MODEL", "base")


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel  # type: ignore

            log.info("whisper_wrapper: loading %s", WHISPER_MODEL_SIZE)
            _model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
            log.info("whisper_wrapper: model ready")
    return _model


def transcribe_file(path: str) -> str:
    """Return the transcript of the audio at `path`, or '' on silence."""
    if not path or not os.path.exists(path):
        return ""
    model = _get_model()
    segments, _info = model.transcribe(path, beam_size=1, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def warm_with_silence(seconds: float = 1.0) -> float:
    """Feed silent PCM to warm GPU/CPU kernels. Returns elapsed ms."""
    import time
    import tempfile

    t0 = time.monotonic()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        sr = 16000
        nframes = int(sr * seconds)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(b"\x00\x00" * nframes)
        transcribe_file(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return (time.monotonic() - t0) * 1000.0


if __name__ == "__main__":
    print(f"Warming whisper... {warm_with_silence():.0f}ms first inference")
