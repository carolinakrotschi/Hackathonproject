"""Pager speech through Fish Audio (POST /v1/tts). The key stays on the server; pagers fetch MP3 from
/api/hospital/tts. Guidance repeats the same phrases, so finished clips are cached in memory.

The key comes from FISH_AUDIO_API_KEY, else from the team key in voice.env at the repo root (private repo, shared
for the hackathon; rotate it afterwards)."""

import json
import os
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path

KEY_FILE = Path(__file__).resolve().parent.parent / "voice.env"
FISH_URL = "https://api.fish.audio/v1/tts"
MODEL = os.environ.get("FISH_AUDIO_MODEL", "s2.1-pro-free")
MAX_CHARS = 500
CACHE_SIZE = 256
TIMEOUT_S = 20.0

_cache: OrderedDict[str, bytes] = OrderedDict()


def api_key() -> str | None:
    if os.environ.get("FISH_AUDIO_API_KEY"):
        return os.environ["FISH_AUDIO_API_KEY"]
    if KEY_FILE.is_file():
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "FISH_AUDIO_API_KEY" and value.strip():
                return value.strip()
    return None


def available() -> bool:
    return api_key() is not None


def speech(text: str) -> bytes:
    """MP3 for `text`. Raises RuntimeError when Fish Audio is not configured or fails."""
    key = api_key()
    if not key:
        raise RuntimeError("FISH_AUDIO_API_KEY is not set.")
    text = text.strip()[:MAX_CHARS]
    if text in _cache:
        _cache.move_to_end(text)
        return _cache[text]
    # latency "low" cuts the first byte from ~2.2 s to ~0.6 s on s2.1-pro-free
    body = {"text": text, "format": "mp3", "latency": "low", "language": "en"}
    request = urllib.request.Request(FISH_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "model": MODEL})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            audio = response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"Fish Audio failed: {error}") from error
    _cache[text] = audio
    if len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return audio
