"""
Photo ingestion, OCR, categorization, and vault storage.

Used by:
  - Telegram photo handler: `process_telegram_photo(path)` on receipt of
    a photo message
  - Action Button shortcut posting an image to the Jetson

Design:
  - OCR is pluggable. Callers can inject an `ocr_fn(image_bytes, mime) -> str`.
    The default uses Claude vision via urllib so we stay inside standard
    library + what is already on the Jetson. pytesseract is optional; if
    importable, an `ocr_tesseract` helper is exposed for callers who prefer
    it, but it is never imported eagerly.
  - Storage is a content-addressed tree under vault/memory/images/ keyed by
    SHA-256 so duplicates collapse and metadata is idempotent.
  - Categorization is a simple rule set on the extracted text, good enough
    to separate receipts, screenshots, whiteboards, documents, and photos.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("alfred.photos")

VAULT_IMAGES = Path("vault/memory/images")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_VISION_MODEL = "claude-sonnet-4-6"

CATEGORIES = ("screenshot", "whiteboard", "receipt", "document", "photo")

OcrFn = Callable[[bytes, str], str]


# ---- data model -------------------------------------------------------------

@dataclass
class PhotoRecord:
    sha: str
    stored_path: Path
    metadata_path: Path
    category: str
    extracted_text: str
    original_filename: str
    captured_at: datetime
    size_bytes: int
    mime: str
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "sha": self.sha,
            "stored_path": str(self.stored_path),
            "metadata_path": str(self.metadata_path),
            "category": self.category,
            "extracted_text": self.extracted_text,
            "original_filename": self.original_filename,
            "captured_at": self.captured_at.isoformat(),
            "size_bytes": self.size_bytes,
            "mime": self.mime,
            "extra": self.extra,
        }


# ---- OCR --------------------------------------------------------------------

def _vision_ocr(image_bytes: bytes, mime: str) -> str:
    """Default OCR: call Claude vision to transcribe visible text.

    Requires ANTHROPIC_API_KEY. Returns empty string on failure.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.warning("photos: ANTHROPIC_API_KEY not set, OCR returning empty")
        return ""
    model = os.environ.get("ALFRED_VISION_MODEL", DEFAULT_VISION_MODEL)
    prompt = (
        "Transcribe all visible text in this image verbatim. "
        "Preserve line breaks. Do not add commentary or description. "
        "If there is no readable text, reply with the single word NONE."
    )
    body = {
        "model": model,
        "max_tokens": 2048,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime or "image/jpeg",
                        "data": base64.b64encode(image_bytes).decode(),
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        log.warning("photos: vision call failed: %s", exc)
        return ""
    parts = payload.get("content", [])
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    return "" if text.upper() == "NONE" else text


def ocr_tesseract(image_path: str | os.PathLike) -> str:
    """Optional pytesseract path for callers who have it installed.

    Raises ImportError if pytesseract is unavailable. Never imported at
    module load so this module stays pip-dep-free.
    """
    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore
    return pytesseract.image_to_string(Image.open(str(image_path)))


# ---- categorization ---------------------------------------------------------

RECEIPT_HINTS = (r"\btotal\b", r"\bsubtotal\b", r"\btax\b", r"\$[\s]*\d", r"\bvisa\b", r"\bmastercard\b")
SCREENSHOT_HINTS = (r"\bsafari\b", r"\bchrome\b", r"\bfirefox\b", r"\bslack\b",
                    r"\bnotifications?\b", r"\bsettings?\b", r"\b(file|edit|view|window|help)\b",
                    r"\b(battery|wifi|bluetooth)\b")
WHITEBOARD_HINTS = (r"->", r"=>", r"==", r"^[A-Z]{2,}$")  # arrows, caps labels

_RX_RECEIPT = [re.compile(h, re.I) for h in RECEIPT_HINTS]
_RX_SCREEN = [re.compile(h, re.I) for h in SCREENSHOT_HINTS]
_RX_WHITEBOARD = [re.compile(h, re.M) for h in WHITEBOARD_HINTS]


def classify(extracted_text: str, filename: str = "") -> str:
    """Pick a category based on the extracted text plus the source filename.

    Rule order (first match wins):
      1. screenshot filename prefix (common on iOS, macOS, Android)
      2. receipt signals ($ totals, subtotal, tax, card brands)
      3. screenshot UI chrome words
      4. whiteboard shorthand (arrows, short capped labels, sparse text)
      5. document (long text)
      6. photo (fallback, low-text)
    """
    fname = filename.lower()
    if any(tok in fname for tok in ("screenshot", "screen shot", "scrnshot")):
        return "screenshot"

    text = extracted_text or ""
    lower = text.lower()
    tokens = text.split()
    line_count = len([ln for ln in text.splitlines() if ln.strip()])

    if any(rx.search(text) for rx in _RX_RECEIPT):
        return "receipt"
    if any(rx.search(lower) for rx in _RX_SCREEN):
        return "screenshot"
    if line_count > 0 and line_count <= 8 and any(rx.search(text) for rx in _RX_WHITEBOARD):
        return "whiteboard"
    if len(tokens) > 80:
        return "document"
    return "photo"


# ---- storage ----------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _day_dir(root: Path, when: datetime) -> Path:
    d = root / when.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def categorize_and_store(
    image_path: str | os.PathLike,
    extracted_text: str,
    category: str,
    *,
    vault_root: Path = VAULT_IMAGES,
    captured_at: Optional[datetime] = None,
    extra: Optional[dict] = None,
) -> PhotoRecord:
    """Move/copy the image into vault/memory/images/YYYY-MM-DD/<sha>.<ext>
    and write a sidecar <sha>.json with metadata. Idempotent on duplicate
    inputs (same sha lands in the same slot).
    """
    src = Path(image_path)
    if not src.exists():
        raise FileNotFoundError(str(src))
    when = captured_at or datetime.now(timezone.utc)
    sha = _sha256_file(src)
    mime, _ = mimetypes.guess_type(str(src))
    suffix = src.suffix or ".bin"

    dest_dir = _day_dir(vault_root, when)
    stored = dest_dir / f"{sha}{suffix}"
    meta_path = dest_dir / f"{sha}.json"

    if not stored.exists():
        shutil.copy2(src, stored)

    record = PhotoRecord(
        sha=sha,
        stored_path=stored,
        metadata_path=meta_path,
        category=category if category in CATEGORIES else "photo",
        extracted_text=extracted_text or "",
        original_filename=src.name,
        captured_at=when,
        size_bytes=stored.stat().st_size,
        mime=mime or "application/octet-stream",
        extra=dict(extra or {}),
    )
    meta_path.write_text(json.dumps(record.as_dict(), indent=2))
    return record


# ---- pipeline ---------------------------------------------------------------

def process_photo(
    image_path: str | os.PathLike,
    *,
    ocr_fn: Optional[OcrFn] = None,
) -> PhotoRecord:
    """Read image bytes, run OCR, classify, and store. Returns the record."""
    src = Path(image_path)
    mime, _ = mimetypes.guess_type(str(src))
    mime = mime or "image/jpeg"
    with src.open("rb") as f:
        data = f.read()
    fn = ocr_fn or _vision_ocr
    try:
        text = fn(data, mime)
    except Exception:
        log.exception("photos: OCR callback crashed")
        text = ""
    category = classify(text, src.name)
    return categorize_and_store(src, text, category)


def process_telegram_photo(
    file_path: str | os.PathLike,
    *,
    ocr_fn: Optional[OcrFn] = None,
) -> str:
    """Entry point for the Telegram bot. Returns a short human summary."""
    try:
        record = process_photo(file_path, ocr_fn=ocr_fn)
    except Exception as exc:
        log.exception("photos: process_telegram_photo failed")
        return f"Could not process that image: {exc}"
    text = record.extracted_text.strip()
    preview = (text[:140] + "...") if len(text) > 140 else text
    lines = [f"Saved as {record.category} ({record.sha[:8]})."]
    if preview:
        lines.append(f"Text: {preview}")
    lines.append(f"Stored at {record.stored_path}")
    return "\n".join(lines)


# ---- search -----------------------------------------------------------------

def search_photos(
    query: str,
    *,
    vault_root: Path = VAULT_IMAGES,
    limit: int = 20,
) -> list[PhotoRecord]:
    """Substring search over stored metadata. Matches extracted_text,
    category, and original_filename, case-insensitive.
    """
    q = query.lower().strip()
    if not q:
        return []
    results: list[PhotoRecord] = []
    if not vault_root.exists():
        return results
    for meta in sorted(vault_root.glob("*/*.json"), reverse=True):
        try:
            data = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        blob = " ".join([
            data.get("extracted_text", ""),
            data.get("category", ""),
            data.get("original_filename", ""),
        ]).lower()
        if q in blob:
            results.append(_record_from_dict(data))
        if len(results) >= limit:
            break
    return results


def _record_from_dict(d: dict) -> PhotoRecord:
    return PhotoRecord(
        sha=d["sha"],
        stored_path=Path(d["stored_path"]),
        metadata_path=Path(d["metadata_path"]),
        category=d["category"],
        extracted_text=d.get("extracted_text", ""),
        original_filename=d.get("original_filename", ""),
        captured_at=datetime.fromisoformat(d["captured_at"]),
        size_bytes=d.get("size_bytes", 0),
        mime=d.get("mime", ""),
        extra=d.get("extra", {}),
    )


# ---- test block -------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    logging.basicConfig(level=logging.INFO)

    if "--offline" in sys.argv:
        # Exercise the pure helpers without any external calls.
        assert classify("Total $12.99\nSubtotal 11.99\nTax 1.00", "photo.jpg") == "receipt"
        assert classify("Chrome File Edit View Help", "screenshot_2026.png") == "screenshot"
        assert classify("sketch here", "IMG_2020.jpg") == "photo"
        assert classify("A -> B -> C\n== final ==", "whiteboard.jpg") == "whiteboard"
        assert classify("Lorem ipsum " * 50, "scan.png") == "document"

        tmp = Path(tempfile.mkdtemp())
        (tmp / "src.jpg").write_bytes(b"fake-jpeg-bytes")
        vault = tmp / "vault"
        rec = categorize_and_store(tmp / "src.jpg", "hello world", "photo",
                                   vault_root=vault)
        assert rec.stored_path.exists()
        assert rec.metadata_path.exists()
        hits = search_photos("hello", vault_root=vault)
        assert hits and hits[0].sha == rec.sha
        print("photos offline tests: ok")
        sys.exit(0)

    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m core.photos <image_path> [--offline]")
    path = sys.argv[1]
    rec = process_photo(path)
    print(json.dumps(rec.as_dict(), indent=2))
