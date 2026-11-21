"""Groq as a fallback LLM when Gemini is rate-limited, overloaded or out of quota.

Uses Groq's OpenAI-compatible REST API through httpx, so no extra SDK is needed.
Disabled unless GROQ_API_KEY is set.
"""
import json
import logging

import httpx

from . import config

log = logging.getLogger("sitesense.fallback")

BASE_URL = "https://api.groq.com/openai/v1"
enabled = bool(config.GROQ_API_KEY)
_client = httpx.AsyncClient(
    base_url=BASE_URL,
    headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
    timeout=httpx.Timeout(60, connect=10),
) if enabled else None
_no_reasoning_effort: set[str] = set()  # models that rejected reasoning_effort


class FallbackError(Exception):
    pass


def _payload(messages: list[dict], temperature: float, **extra) -> dict:
    body = {"model": config.GROQ_MODEL, "messages": messages, "temperature": temperature, **extra}
    # gpt-oss models reason before answering; keep it short so the first words arrive quickly.
    if "gpt-oss" in config.GROQ_MODEL and config.GROQ_MODEL not in _no_reasoning_effort:
        body["reasoning_effort"] = "low"
    return body


async def _error(resp: httpx.Response) -> FallbackError:
    try:
        detail = (await resp.aread()).decode(errors="replace")[:300]
    except Exception:
        detail = ""
    return FallbackError(f"Groq HTTP {resp.status_code}: {detail}")


async def stream_chat(system: str, messages: list[dict], temperature: float = 0.1):
    """Yield answer text chunks. messages: [{"role": "user"|"assistant", "content": str}, ...]."""
    if not enabled:
        raise FallbackError("Groq fallback is not configured.")
    body = _payload([{"role": "system", "content": system}, *messages], temperature, stream=True)
    for attempt in range(2):
        async with _client.stream("POST", "/chat/completions", json=body) as resp:
            if resp.status_code == 400 and "reasoning_effort" in body and attempt == 0:
                _no_reasoning_effort.add(config.GROQ_MODEL)
                body.pop("reasoning_effort")
                continue
            if resp.status_code != 200:
                raise await _error(resp)
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    return
                try:
                    delta = json.loads(data)["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError):
                    continue
                if delta.get("content"):  # reasoning arrives in a separate field and is never shown
                    yield delta["content"]
            return


async def complete_json(prompt: str) -> dict:
    """One JSON-object completion (used for topic generation)."""
    if not enabled:
        raise FallbackError("Groq fallback is not configured.")
    body = _payload([{"role": "user", "content": prompt}], 0.2, response_format={"type": "json_object"})
    resp = await _client.post("/chat/completions", json=body)
    if resp.status_code == 400 and "reasoning_effort" in body:
        _no_reasoning_effort.add(config.GROQ_MODEL)
        body.pop("reasoning_effort")
        resp = await _client.post("/chat/completions", json=body)
    if resp.status_code != 200:
        raise await _error(resp)
    return json.loads(resp.json()["choices"][0]["message"]["content"])


_EXT = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm", "audio/ogg": "ogg", "audio/mpeg": "mp3",
        "audio/mp4": "mp4", "audio/aac": "m4a", "audio/flac": "flac"}


async def transcribe(audio: bytes, mime: str, prompt: str) -> str:
    """Speech to text with Whisper; the prompt (max ~224 tokens) carries the site's names for correct spelling."""
    if not enabled:
        raise FallbackError("Groq fallback is not configured.")
    files = {"file": (f"question.{_EXT.get(mime, 'wav')}", audio, mime)}
    data = {"model": config.GROQ_STT_MODEL, "response_format": "json", "temperature": "0", "prompt": prompt[:800]}
    resp = await _client.post("/audio/transcriptions", files=files, data=data)
    if resp.status_code != 200:
        raise await _error(resp)
    return (resp.json().get("text") or "").strip()
