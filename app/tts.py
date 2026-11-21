import re

import edge_tts

from . import config

MAX_CHARS = 5000


def clean_for_speech(text: str) -> str:
    text = re.sub(r"\s*\[\d{1,2}(?:\s*,\s*\d{1,2})*\]", "", text)  # citation markers like [1] or [2, 3]
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"^\s*(#+|[-*+]|\d+\.)\s+", "", text, flags=re.M)  # headings and list markers
    text = re.sub(r"[*_`#>|~]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def prepare(text: str) -> str:
    """Return the text to speak. Raises ValueError if nothing is left to speak."""
    text = clean_for_speech(text)[:MAX_CHARS]
    if not text:
        raise ValueError("Nothing to read aloud.")
    return text


async def stream(text: str):
    """Yield MP3 chunks as soon as the voice service produces them, so playback starts early."""
    async for chunk in edge_tts.Communicate(text, config.TTS_VOICE).stream():
        if chunk["type"] == "audio":
            yield chunk["data"]
