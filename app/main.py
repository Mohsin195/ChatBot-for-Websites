import asyncio
import json
import logging
import re
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is too noisy for big crawls

from . import config, crawler, rag, tts  # noqa: E402  (config validates the API key at import)

log = logging.getLogger("sitesense")
STATIC = Path(__file__).resolve().parent.parent / "static"
REPLACED = "This website was replaced. Please ask about the current one."
EXPIRED = "This chatbot expired after a period of inactivity. Click “New website” to build it again."
SESSION_COOKIE = "sitesense_session"
SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
BUILDING = ("queued", "discovering", "crawling", "indexing")


@dataclass
class Site:
    """One visitor's website and chatbot. Replaced wholesale on that visitor's next crawl or reset."""
    id: str = ""
    session: str = ""
    url: str = ""
    name: str = ""
    icon: str = ""
    stage: str = "idle"  # idle | queued | discovering | crawling | indexing | ready | error
    message: str = ""
    max_pages: int = 0
    pages_found: int = 0
    pages_crawled: int = 0
    pages_indexed: int = 0
    chunks_indexed: int = 0
    chunks_total: int = 0
    topics: list[str] = field(default_factory=list)
    vocab: list[str] = field(default_factory=list)  # names visitors may say, for voice input
    answer_cache: OrderedDict = field(default_factory=OrderedDict)
    last_active: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None


sites: dict[str, Site] = {}                                   # session id -> that visitor's site
session_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
crawl_slots = asyncio.Semaphore(config.MAX_CONCURRENT_CRAWLS)  # Chromium + crawling are heavy: cap them
waiting: list[str] = []                                       # site ids queued for a crawl slot, in order


# ---------------------------------------------------------------- sessions & rate limits
def session_of(request: Request) -> str:
    return request.state.session


def current_site(request: Request) -> Site | None:
    s = sites.get(session_of(request))
    if s:
        s.last_active = time.monotonic()
    return s


def client_ip(request: Request) -> str:
    if config.TRUST_PROXY_HEADERS:
        # The proxy appends the address it saw; earlier entries can be forged by the client.
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Sliding-window limits per client IP, kept in memory (single worker process)."""

    def __init__(self):
        self.hits: defaultdict[str, deque] = defaultdict(deque)

    def check(self, key: str, limit: int, window: float, message: str) -> None:
        if limit <= 0:
            return
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            wait = int(window - (now - q[0])) + 1
            raise HTTPException(429, message.format(wait=_human(wait)), headers={"Retry-After": str(wait)})
        q.append(now)

    def prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, q in self.hits.items() if not q or now - q[-1] > 3600]:
            del self.hits[key]


def _human(seconds: int) -> str:
    return f"{seconds} seconds" if seconds < 90 else f"{round(seconds / 60)} minutes"


limiter = RateLimiter()


# ---------------------------------------------------------------- site lifecycle
async def remove_site(s: Site) -> None:
    """Cancel the site's crawl if running and delete its vectors."""
    if sites.get(s.session) is s:
        del sites[s.session]
    if s.task and not s.task.done():
        s.task.cancel()
        await asyncio.wait([s.task], timeout=15)
    await asyncio.to_thread(rag.delete_site, s.id)


async def make_room() -> None:
    """Stay within MAX_ACTIVE_SITES by removing the least recently used finished chatbots."""
    while len(sites) >= config.MAX_ACTIVE_SITES:
        idle = [s for s in sites.values() if s.stage not in BUILDING]
        if not idle:
            raise HTTPException(503, "The server is busy building other chatbots. Please try again in a few minutes.")
        oldest = min(idle, key=lambda s: s.last_active)
        log.info("Removing least recently used site %s to make room", oldest.id)
        await remove_site(oldest)


async def cleanup_loop() -> None:
    """Delete chatbots nobody has used for SITE_TTL_MINUTES, and forget old rate-limit entries."""
    while True:
        await asyncio.sleep(300)
        try:
            cutoff = time.monotonic() - config.SITE_TTL_MINUTES * 60
            for s in [s for s in sites.values() if s.last_active < cutoff and s.stage not in BUILDING]:
                log.info("Site %s expired after %d idle minutes", s.id, config.SITE_TTL_MINUTES)
                await remove_site(s)
            limiter.prune()
            for sid in [k for k, lock in session_locks.items() if k not in sites and not lock.locked()]:
                del session_locks[sid]
        except Exception:
            log.exception("Cleanup failed")


async def build(s: Site, max_pages: int) -> None:
    """Wait for a free crawl slot, then crawl and index."""
    waiting.append(s.id)
    try:
        async with crawl_slots:
            waiting.remove(s.id)
            await _build(s, max_pages)
    finally:
        if s.id in waiting:
            waiting.remove(s.id)


async def _build(s: Site, max_pages: int) -> None:
    try:
        c = crawler.Crawler(s.url, max_pages, s)
        pages = await c.run()
        if not pages:
            raise crawler.CrawlError(
                "No readable content was found. The site may block crawlers, require a login, or be empty."
            )
        s.name = c.site_name or urlsplit(s.url).hostname.removeprefix("www.")
        s.icon = c.icon
        s.pages_indexed = len(pages)
        s.stage = "indexing"
        await rag.index_site(s.id, pages, s)
        s.message = "Summarising topics…"
        s.topics = await rag.make_topics(s.name, [p.title for p in pages])
        s.vocab = rag.site_vocabulary(s.name, s.url, s.topics, pages)
        s.stage, s.message = "ready", f"Indexed {len(pages)} pages ({s.chunks_indexed} chunks)."
        log.info("Site %s ready: %s", s.id, s.message)
    except asyncio.CancelledError:
        log.info("Crawl %s cancelled", s.id)
        raise
    except crawler.CrawlError as e:
        s.stage, s.message = "error", str(e)
        log.warning("Crawl %s failed: %s", s.id, e)
    except genai_errors.APIError as e:
        if rag.is_daily_quota(e):
            msg = "The AI service reached its daily quota. Please try again later."
        elif e.code == 429:
            msg = "The AI service is busy. Please wait a few minutes and try again."
        elif e.code in (400, 401, 403):
            msg = f"The AI service rejected the request ({e.code}). The server's API key may be invalid."
        else:
            msg = f"AI service error ({e.code}). Please try again."
        s.stage, s.message = "error", msg
        log.error("Gemini error during build: %s", e)
    except Exception:
        s.stage, s.message = "error", "Something went wrong while building the chatbot. Please try again."
        log.exception("Build %s failed", s.id)
    finally:
        s.last_active = time.monotonic()  # the expiry clock starts when the build ends


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(rag.delete_all)  # in-memory state is gone, so drop leftovers from earlier runs
    cleaner = asyncio.create_task(cleanup_loop())
    yield
    cleaner.cancel()
    for s in list(sites.values()):
        if s.task:
            s.task.cancel()


app = FastAPI(title="SiteSense AI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def session_cookie(request: Request, call_next):
    """Give every browser its own anonymous session, so each visitor has a separate chatbot."""
    sid = request.cookies.get(SESSION_COOKIE, "")
    is_new = not SESSION_ID.match(sid)
    if is_new:
        sid = uuid.uuid4().hex
    request.state.session = sid
    response = await call_next(request)
    if is_new:
        https = request.url.scheme == "https" or (
            config.TRUST_PROXY_HEADERS and request.headers.get("x-forwarded-proto", "") == "https")
        response.set_cookie(SESSION_COOKIE, sid, max_age=30 * 24 * 3600, httponly=True, samesite="lax", secure=https)
    return response


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("Unhandled error: %s", exc)
    return JSONResponse({"detail": "Unexpected server error. Please try again."}, status_code=500)


class CrawlIn(BaseModel):
    url: str = Field(max_length=2048)
    max_pages: int = config.DEFAULT_MAX_PAGES


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatIn(BaseModel):
    site_id: str
    question: str = Field(min_length=1, max_length=2000)
    history: list[Message] = []


class TtsIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


MAX_AUDIO_BYTES = 4 * 1024 * 1024  # ~2 minutes of 16 kHz mono WAV
AUDIO_TYPES = {"audio/wav", "audio/x-wav", "audio/webm", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/flac"}
TTS_TTL = 600
tts_jobs: dict[str, tuple[float, str]] = {}  # id -> (created, text); the <audio> element fetches it by GET


def site_for(request: Request, site_id: str) -> Site:
    """The visitor's site, if it is the one the page is showing."""
    s = current_site(request)
    if not s:
        raise HTTPException(409, EXPIRED)
    if site_id != s.id:
        raise HTTPException(409, REPLACED)
    return s


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/crawl")
async def start_crawl(body: CrawlIn, request: Request):
    limiter.check(f"crawl:{client_ip(request)}", config.CRAWLS_PER_HOUR, 3600,
                  "You've built several chatbots in a short time. Please try again in {wait}.")
    try:
        url = await crawler.validate_url(body.url)
    except crawler.CrawlError as e:
        raise HTTPException(400, str(e))
    max_pages = max(1, min(body.max_pages, config.MAX_PAGES_LIMIT))
    sid = session_of(request)
    async with session_locks[sid]:
        if sid in sites:
            await remove_site(sites[sid])
        await make_room()
        s = Site(id=uuid.uuid4().hex[:12], session=sid, url=url, stage="queued",
                 message="Waiting for a free slot…", max_pages=max_pages)
        sites[sid] = s
        s.task = asyncio.create_task(build(s, max_pages))
    log.info("New crawl %s for %s (max %d pages); %d active sites", s.id, url, max_pages, len(sites))
    return {"site_id": s.id}


@app.get("/api/status")
async def status(request: Request):
    s = current_site(request) or Site()
    message = s.message
    if s.stage == "queued" and s.id in waiting:
        ahead = waiting.index(s.id)
        message = ("Waiting for a free slot. You're next…" if ahead == 0
                   else f"Waiting for a free slot. {ahead} {'chatbot' if ahead == 1 else 'chatbots'} ahead of you…")
    return {
        "site_id": s.id, "stage": s.stage, "message": message, "max_pages": s.max_pages,
        "pages_found": s.pages_found, "pages_crawled": s.pages_crawled, "pages_indexed": s.pages_indexed,
        "chunks_indexed": s.chunks_indexed, "chunks_total": s.chunks_total,
        "site_url": s.url, "site_name": s.name, "icon": s.icon, "topics": s.topics,
        "max_pages_limit": config.MAX_PAGES_LIMIT, "default_max_pages": config.DEFAULT_MAX_PAGES,
    }


@app.post("/api/chat")
async def chat(body: ChatIn, request: Request):
    s = site_for(request, body.site_id)
    if s.stage != "ready":
        raise HTTPException(409, "The chatbot is still being built. Please wait until it's ready.")
    limiter.check(f"chat:{client_ip(request)}", config.QUESTIONS_PER_MINUTE, 60,
                  "You're asking questions very quickly. Please wait {wait} and try again.")

    async def events():
        try:
            async for ev in rag.stream_answer(s, body.question.strip(), body.history):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as e:
            log.exception("Chat failed")
            msg = REPLACED if sites.get(s.session) is not s else "Sorry, I couldn't generate an answer right now. Please try again."
            if rag.is_daily_quota(e):
                msg = "The AI service reached its daily quota. Please try again later."
            elif isinstance(e, genai_errors.APIError) and e.code == 429:
                msg = "The AI service is busy (rate limit reached). Please wait a moment and try again."
            yield f"data: {json.dumps({'type': 'error', 'message': msg})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/transcribe")
async def transcribe(request: Request, site_id: str = ""):
    """Voice question -> text, using the visitor's website vocabulary so its names are spelled right."""
    s = site_for(request, site_id)
    limiter.check(f"voice:{client_ip(request)}", config.VOICE_PER_MINUTE, 60,
                  "Too many voice questions in a short time. Please wait {wait} or type your question.")
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime not in AUDIO_TYPES:
        raise HTTPException(415, "Unsupported audio format.")
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "No audio received.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "The recording is too long. Please ask a shorter question.")
    t0 = time.perf_counter()
    try:
        text = await rag.transcribe(audio, mime, s)
    except Exception:
        log.exception("Transcription failed")
        raise HTTPException(502, "Voice transcription is unavailable right now.")
    log.info("Transcribed %d KB in %.0f ms: %r", len(audio) // 1024, (time.perf_counter() - t0) * 1000, text[:120])
    return {"text": text}


@app.post("/api/tts")
async def text_to_speech(body: TtsIn, request: Request):
    """Register text to read aloud; the browser then streams it from GET /api/tts/{id}."""
    limiter.check(f"tts:{client_ip(request)}", config.TTS_PER_MINUTE, 60,
                  "Too many read-aloud requests. Please wait {wait}.")
    try:
        text = tts.prepare(body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    now = time.monotonic()
    for k in [k for k, (t, _) in tts_jobs.items() if now - t > TTS_TTL]:
        del tts_jobs[k]
    job = uuid.uuid4().hex
    tts_jobs[job] = (now, text)
    return {"url": f"/api/tts/{job}"}


@app.get("/api/tts/{job}")
async def tts_audio(job: str):
    if job not in tts_jobs:
        raise HTTPException(404, "This audio has expired. Please press play again.")
    gen = tts.stream(tts_jobs[job][1])
    try:
        first = await anext(gen)  # surface voice-service failures as an HTTP error, not a broken stream
    except StopAsyncIteration:
        raise HTTPException(502, "Text-to-speech returned no audio.")
    except Exception:
        log.exception("TTS failed")
        raise HTTPException(502, "Text-to-speech is unavailable right now. Please try again.")

    async def body():
        yield first
        try:
            async for chunk in gen:
                yield chunk
        except Exception:
            log.exception("TTS stream interrupted")

    return StreamingResponse(body(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/reset")
async def reset(request: Request):
    """Delete only this visitor's chatbot."""
    sid = session_of(request)
    async with session_locks[sid]:
        if sid in sites:
            await remove_site(sites[sid])
    return {"ok": True}
