import asyncio
import json
import logging
import re
import time
from collections import Counter
from urllib.parse import urlsplit


import aiohttp
import chromadb
import httpx
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
from google import genai
from google.genai import errors, types

from . import config, fallback

log = logging.getLogger("sitesense.rag")

# v1beta serves the "-latest" model aliases (e.g. gemini-flash-latest); v1 does not.
# SDK retries are off so _retry() below is the single, logged backoff layer.
client = genai.Client(api_key=config.GEMINI_API_KEY, http_options=types.HttpOptions(
    api_version="v1beta", timeout=90_000, retry_options=types.HttpRetryOptions(attempts=1),
))
# Local ONNX models: no PyTorch, small memory footprint, fast on CPU. Downloaded once into MODEL_CACHE_DIR.
log.info("Loading embedding model %s", config.EMBED_MODEL)
embedding_model = TextEmbedding(config.EMBED_MODEL, cache_dir=config.MODEL_CACHE_DIR)
reranker = None
if config.RERANK_MODEL:
    log.info("Loading reranker %s", config.RERANK_MODEL)
    reranker = TextCrossEncoder(config.RERANK_MODEL, cache_dir=config.MODEL_CACHE_DIR)
# Score a question's best chunk must reach before the LLM is called (reranker score, or cosine similarity).
GATE = config.RERANK_THRESHOLD if reranker else config.RELEVANCE_THRESHOLD
chroma = chromadb.PersistentClient(path=config.CHROMA_DIR, settings=chromadb.Settings(anonymized_telemetry=False))
# Gemini's async client runs on aiohttp (timeouts surface as TimeoutError); Groq uses httpx.
NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError, TimeoutError, aiohttp.ClientError)
# With a fallback configured, give up on Gemini sooner instead of backing off for minutes.
GEMINI_ATTEMPTS = 2 if fallback.enabled else 8
# A visitor is waiting for chat answers: don't let an overloaded model hang for the default 90 seconds.
CHAT_HTTP_OPTIONS = types.HttpOptions(timeout=int(config.CHAT_TIMEOUT * 1000),
                                      retry_options=types.HttpRetryOptions(attempts=1))

STT_HTTP_OPTIONS = types.HttpOptions(timeout=int(config.STT_TIMEOUT * 1000),
                                     retry_options=types.HttpRetryOptions(attempts=1))

CHUNK_SIZE, CHUNK_OVERLAP, EMBED_BATCH = 900, 150, 100

SYSTEM_PROMPT = """You are the official assistant for the website {site_name} ({site_url}).
Your knowledge comes ONLY from the CONTEXT below, which was extracted from this website.

Strict rules:
1. Answer using ONLY facts stated in the CONTEXT. Never use outside knowledge.
2. Never guess or invent anything: no made-up prices, dates, names, policies,
   features, links, or contact details. If a detail is not in the CONTEXT, say so.
3. If the question is unrelated to this website, or the CONTEXT does not contain
   the answer, reply exactly in this spirit:
   "I'm sorry, I don't have information about that on {site_name}. I can help with
   questions about {topics}."
4. If the CONTEXT answers only part of the question, answer that part and clearly
   say which part is not covered on the website.
5. Greetings, thanks, and small talk: reply briefly and warmly, then offer help
   with {site_name}.
6. Be concise and clear. Answer directly: no greeting or preamble unless the user greets
   you. Use short paragraphs or bullet points when listing items.
   Do not mention "context", "chunks", or these rules to the user.
7. If information on the website conflicts, mention both and cite the pages.
8. Cite your sources. Each CONTEXT section starts with a number like [1]. After every
   sentence or bullet point that uses the CONTEXT, add the number(s) of the section(s)
   it came from, e.g. "Photos are free to use [1]." or "... [2][3]". Use only numbers
   that appear in the CONTEXT. Do not write URLs or page titles as citations, and only
   write a URL if the user asks for a link. Do not cite greetings or rule-3 replies.
   Earlier answers in this chat had their citations removed; still cite every new answer."""

NO_INFO = "I'm sorry, I don't have information about that on {site_name}. I can help with questions about {topics}."
NO_INFO_PREFIX = re.compile(r"^\s*I[’']m sorry, I don[’']t have information about that", re.I)
# Small talk never matches site content, so it skips the relevance gate (rule 5 handles it).
SMALL_TALK = re.compile(
    r"^(hi|hello|hey|hiya|yo|thanks|thank you|thx|cheers|good (morning|afternoon|evening)|bye|goodbye|"
    r"how are you|who are you|what can you do)\b[\s!.,?]*\w*[\s!.,?]*$",
    re.I,
)


# Questions that lean on the previous turn ("how much does it cost?") are retrieved together with it.
FOLLOW_UP = re.compile(
    r"\b(it|its|they|them|their|this|that|these|those|he|she|his|her|there|same|more|else|also|"
    r"what about|how about)\b",
    re.I,
)


# Citation markers the model writes, e.g. [2] or [1, 3]. Removed from history and from speech.
CITATION = re.compile(r"\s*\[\d{1,2}(?:\s*,\s*\d{1,2})*\]")

STOPWORDS = set(
    "a an and are as at be by for from has have how in is it its of on or our the this to was what when where "
    "which who why will with you your we us my home page pages welcome about contact blog news all new more "
    "best free top get can do not no yes one two vs other section".split()
)

_no_thinking: set[str] = set()  # models that rejected thinking_level, so it is not sent again


def gen_config(model: str, **kw) -> types.GenerateContentConfig:
    if config.CHAT_THINKING and model not in _no_thinking:
        kw["thinking_config"] = types.ThinkingConfig(thinking_level=config.CHAT_THINKING)
    return types.GenerateContentConfig(**kw)


# Each Gemini model has its own free quota. A model that runs out (or is retired) is skipped for a while
# and the next one in the chain is used, so visitors don't wait on calls that are bound to fail.
_cooldown: dict[str, float] = {}


def model_chain(primary: str) -> list[str]:
    chain = list(dict.fromkeys([primary, *config.BACKUP_MODELS]))
    now = time.monotonic()
    ready = [m for m in chain if _cooldown.get(m, 0) <= now]
    return ready or chain  # all cooling down: try them anyway rather than fail without asking


def switchable(e: Exception) -> bool:
    """Errors another model may not have: quota/rate limit, overload, retired model, timeout."""
    if isinstance(e, errors.APIError):
        return e.code in (404, 429, 500, 502, 503, 504)
    return isinstance(e, NETWORK_ERRORS)


def note_failure(model: str, e: Exception) -> None:
    code = getattr(e, "code", None)
    pause = 3600 if code == 404 or is_daily_quota(e) else 60 if code == 429 else 0
    if pause:
        _cooldown[model] = time.monotonic() + pause
        log.warning("Skipping %s for %d min (%s)", model, pause // 60, code)


def err_name(e: Exception) -> str:
    return str(getattr(e, "code", None) or type(e).__name__)


async def generate(model: str, what: str, contents, stream: bool = False, attempts: int = 8,
                   chain: bool = True, **kw):
    """Call Gemini, moving on to the backup models when one is out of quota, overloaded or retired."""
    models = model_chain(model) if chain else [model]
    last = None
    for m in models:
        try:
            return await _generate_one(m, what, contents, stream,
                                       attempts if len(models) == 1 else min(attempts, 2), **kw)
        except Exception as e:
            if not switchable(e):
                raise
            note_failure(m, e)
            last = e
            if m != models[-1]:
                log.warning("%s: %s failed (%s), trying the next model", what, m, err_name(e))
    raise last


async def _generate_one(model: str, what: str, contents, stream: bool, attempts: int, **kw):
    """Call one Gemini model with low thinking for speed; drop the setting once if the model doesn't support it."""
    fn = client.aio.models.generate_content_stream if stream else client.aio.models.generate_content

    def call():
        return fn(model=model, contents=contents, config=gen_config(model, **kw))
    try:
        return await _retry(call, what, attempts)
    except errors.APIError as e:
        if e.code != 400 or not config.CHAT_THINKING or model in _no_thinking:
            raise
        log.warning("%s rejected thinking_level=%s; using the model default", model, config.CHAT_THINKING)
        _no_thinking.add(model)
        return await _retry(call, what, attempts)


def should_fall_back(e: Exception) -> bool:
    """Rate limits, quota, overloads and network failures are worth retrying on another provider."""
    if not fallback.enabled:
        return False
    return switchable(e)


WORD = r"[A-Za-z][\w&+'-]*[A-Za-z0-9]"
# A capitalised word that follows a lowercase word mid-sentence is almost always a name (brand, product, place).
MID_SENTENCE_NAME = re.compile(rf"(?<=[a-z,;:] )(?:{WORD})")


def site_vocabulary(site_name: str, site_url: str, topics: list[str], pages, limit: int = 80) -> list[str]:
    """Names a visitor is likely to say: brand, hostname, topics, and proper nouns recurring across pages."""
    host = (urlsplit(site_url).hostname or "").removeprefix("www.")
    terms = [site_name, host, host.split(".")[0], *topics]
    counts = Counter()
    for p in pages:
        found = {w for w in re.findall(WORD, p.title) if w[0].isupper() or any(c.isdigit() for c in w)}
        found |= {w for w in MID_SENTENCE_NAME.findall(p.text[:20000]) if w[0].isupper()}
        found |= set(re.findall(r"\b[a-z]+[A-Z]\w+\b|\b[A-Z][a-z]+[A-Z]\w*\b", p.text[:20000]))  # camelCase / CamelCase
        counts.update(w for w in found if w.lower() not in STOPWORDS and len(w) > 2)
    min_pages = 2 if len(pages) > 3 else 1  # a name on one page of many is usually noise
    terms += [w for w, c in counts.most_common(limit * 2) if c >= min_pages][:limit]
    seen, out = set(), []
    for t in terms:
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


async def transcribe(audio: bytes, mime: str, site) -> str:
    """Speech to text that knows the site's own names, so a brand isn't heard as an everyday word."""
    vocab = ", ".join(site.vocab or [site.name])
    prompt = (
        f"Transcribe this spoken question from a visitor of the website {site.name} ({site.url}). "
        f"Names and terms used on this website, which the speaker may say: {vocab}. "
        "When a spoken word sounds like one of these names, write that exact spelling. "
        "Otherwise transcribe exactly what was said; do not answer, translate, or add anything. "
        "Return only the transcript text. If there is no speech, return an empty string."
    )
    try:
        resp = await generate(
            config.STT_MODEL, "transcribe", temperature=0, attempts=GEMINI_ATTEMPTS,
            contents=[types.Part.from_bytes(data=audio, mime_type=mime), prompt],
            http_options=STT_HTTP_OPTIONS,  # a slow model hands over to the next one instead of hanging
        )
        return (resp.text or "").strip().strip('"').strip()
    except Exception as e:
        if not should_fall_back(e):
            raise
        log.warning("Gemini transcription failed (%s); using Groq Whisper", getattr(e, "code", type(e).__name__))
        # Whisper's prompt is a spelling hint, not an instruction: give it the names it should spell.
        return await fallback.transcribe(audio, mime, f"{site.name}. {vocab}.")


def collection_name(site_id: str) -> str:
    return f"site_{site_id}"


def format_topics(topics: list[str]) -> str:
    if not topics:
        return "the content of this website"
    return topics[0] if len(topics) == 1 else ", ".join(topics[:-1]) + " and " + topics[-1]


def _units(text: str) -> list[str]:
    """Split on headings and paragraphs, then sentences, then hard cuts, so each unit fits a chunk."""
    out = []
    for block in re.split(r"\n\s*\n|\n(?=#)", text):
        block = block.strip()
        if len(block) <= CHUNK_SIZE:
            out += [block] if block else []
            continue
        for sent in re.split(r"(?<=[.!?])\s+|\n", block):
            while len(sent) > CHUNK_SIZE:
                cut = sent.rfind(" ", 0, CHUNK_SIZE)
                cut = cut if cut > CHUNK_SIZE // 2 else CHUNK_SIZE
                out.append(sent[:cut])
                sent = sent[cut:].strip()
            if sent.strip():
                out.append(sent.strip())
    return out


def chunk_page(title: str, text: str) -> list[str]:
    chunks, cur = [], ""
    for unit in _units(text):
        if cur and len(cur) + len(unit) + 1 > CHUNK_SIZE:
            chunks.append(cur)
            tail = cur[-CHUNK_OVERLAP:]
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail  # start overlap on a word
            cur = f"{tail}\n{unit}"
        else:
            cur = f"{cur}\n{unit}" if cur else unit
    if cur:
        chunks.append(cur)
    return [f"Page: {title}\n{c}" for c in chunks]


def is_daily_quota(e: Exception) -> bool:
    """A per-day quota won't recover in minutes, so retrying it only wastes time."""
    return isinstance(e, errors.APIError) and e.code == 429 and "PerDay" in str(e)


async def _retry(make_call, what: str, attempts: int = 8):
    """Exponential backoff for Gemini rate limits (429), overloads (5xx), and network timeouts."""
    delay = 2
    for attempt in range(attempts):
        try:
            return await make_call()
        except (errors.APIError, *NETWORK_ERRORS) as e:
            transient = not isinstance(e, errors.APIError) or e.code in (429, 500, 502, 503, 504)
            if not transient or attempt == attempts - 1 or is_daily_quota(e):
                raise
            log.warning("%s: %s, retrying in %ss", what, getattr(e, "code", None) or type(e).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def embed(texts: list[str], task_type: str) -> list[list[float]]:
    # Local embeddings keep both indexing and query vectors off paid API quotas.
    def run():
        return [v.tolist() for v in embedding_model.embed(texts, batch_size=EMBED_BATCH)]
    return await asyncio.to_thread(run)


async def rerank(query: str, docs: list[str]) -> list[float]:
    return await asyncio.to_thread(lambda: [float(s) for s in reranker.rerank(query, docs)])


async def index_site(site_id: str, pages, progress) -> None:
    items = [(chunk, p) for p in pages for chunk in chunk_page(p.title, p.text)]
    progress.chunks_total = len(items)
    progress.message = f"Indexing {len(items)} text chunks from {len(pages)} pages…"
    col = await asyncio.to_thread(
        chroma.create_collection, collection_name(site_id), metadata={"hnsw:space": "cosine"}
    )
    for i in range(0, len(items), EMBED_BATCH):
        batch = items[i:i + EMBED_BATCH]
        vectors = await embed([c for c, _ in batch], "RETRIEVAL_DOCUMENT")
        await asyncio.to_thread(
            col.add,
            ids=[f"{site_id}-{i + j}" for j in range(len(batch))],
            documents=[c for c, _ in batch],
            embeddings=vectors,
            metadatas=[{"url": p.url, "title": p.title, "site_id": site_id} for _, p in batch],
        )
        progress.chunks_indexed += len(batch)
        log.info("Indexed %d / %d chunks", progress.chunks_indexed, len(items))


async def make_topics(site_name: str, titles: list[str]) -> list[str]:
    listing = "\n".join(f"- {t}" for t in dict.fromkeys(titles[:300]))
    prompt = (
        f"These are the page titles of the website {site_name}:\n{listing}\n\n"
        "Return 3 to 5 short topic phrases (2 to 5 words each, lowercase unless a proper noun) "
        "that describe what this website covers, as a JSON array of strings."
    )
    try:
        resp = await generate(
            # Topics only feed the suggested questions: never hold up a finished chatbot for minutes over them.
            config.CHAT_MODEL, "topics", contents=prompt, attempts=2 if fallback.enabled else 3,
            http_options=CHAT_HTTP_OPTIONS,
            temperature=0.2, response_mime_type="application/json", response_schema=list[str],
        )
        topics = [t.strip() for t in json.loads(resp.text) if isinstance(t, str) and t.strip()]
        return topics[:5]
    except Exception as e:
        if should_fall_back(e):
            try:
                data = await fallback.complete_json(prompt.replace(
                    "as a JSON array of strings.", 'as a JSON object like {"topics": ["...", "..."]}.'))
                topics = [t.strip() for t in data.get("topics", []) if isinstance(t, str) and t.strip()]
                log.info("Topics generated by Groq fallback")
                return topics[:5]
            except Exception as fe:
                log.warning("Groq topic fallback failed: %s", fe)
        log.warning("Topic generation failed: %s", e)
        return []


def delete_all() -> None:
    for c in chroma.list_collections():
        chroma.delete_collection(c if isinstance(c, str) else c.name)


def delete_site(site_id: str) -> None:
    try:
        chroma.delete_collection(collection_name(site_id))
    except Exception:
        pass  # never created (crawl failed or was cancelled early)


async def _retrieve(site_id: str, queries: list[str]) -> tuple[list[tuple[float, str, dict]], list[float]]:
    """Return hits for the first query whose best match clears the gate, plus every query's best score.

    Each query fetches RETRIEVE_CANDIDATES chunks by meaning; the reranker then reads the question with each
    chunk, re-orders them, and the best TOP_K are kept. Hit scores are reranker scores (or cosine without one).
    """
    col = await asyncio.to_thread(chroma.get_collection, collection_name(site_id))
    n = max(config.TOP_K, config.RETRIEVE_CANDIDATES) if reranker else config.TOP_K
    chosen, scores = [], []
    for query, vec in zip(queries, await embed(queries, "RETRIEVAL_QUERY")):
        res = await asyncio.to_thread(
            col.query, query_embeddings=[vec], n_results=n,
            include=["documents", "metadatas", "distances"],
        )
        hits = [(1 - d, doc, m) for d, doc, m in zip(res["distances"][0], res["documents"][0], res["metadatas"][0])]
        if reranker and hits:
            rs = await rerank(query, [doc for _, doc, _ in hits])
            hits = sorted(((r, doc, m) for r, (_, doc, m) in zip(rs, hits)), key=lambda h: h[0], reverse=True)
        hits = hits[:config.TOP_K]
        scores.append(hits[0][0] if hits else float("-inf"))
        if not chosen and scores[-1] >= GATE:
            chosen = hits
    return chosen, scores


def number_sources(hits) -> list[dict]:
    """One number per page, in retrieval order, so several chunks of a page share a citation."""
    sources: dict[str, dict] = {}
    for _, _, m in hits:
        sources.setdefault(m["url"], {"n": len(sources) + 1, "url": m["url"], "title": m["title"]})
    return list(sources.values())


def cache_key(question: str) -> str:
    """Case, punctuation and spacing don't change the question."""
    q = re.sub(r"['’]", "", question.lower())  # "what's" == "whats"
    return " ".join(re.sub(r"[^\w\s]", " ", q).split())


def _cache_get(site, key: str):
    cache = getattr(site, "answer_cache", None)
    if cache is None or key not in cache:
        return None
    cache.move_to_end(key)
    return cache[key]


def _cache_put(site, key: str, value) -> None:
    cache = getattr(site, "answer_cache", None)
    if cache is None or config.ANSWER_CACHE_SIZE <= 0:
        return
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > config.ANSWER_CACHE_SIZE:
        cache.popitem(last=False)


async def _llm_stream(system: str, history: list, question: str):
    """Yield answer text from Gemini; switch to Groq if Gemini fails before its first word."""
    contents = [
        types.Content(role="user" if h.role == "user" else "model",
                      parts=[types.Part(text=CITATION.sub("", h.content) if h.role == "assistant" else h.content)])
        for h in history
    ] + [types.Content(role="user", parts=[types.Part(text=question)])]
    # Gemini can fail on the request or after the stream opens (e.g. 503 "high demand"). Either way, if no
    # words have been shown yet, move straight on to the next model, then to Groq. A person is waiting.
    started, gemini_error = False, None
    for round_ in range(2):  # "high demand" spikes usually pass within seconds: one more quick round
        if round_:
            if not any(_cooldown.get(m, 0) <= time.monotonic() for m in model_chain(config.CHAT_MODEL)):
                break  # every model is out of quota, not just busy
            await asyncio.sleep(1.5)
        for model in model_chain(config.CHAT_MODEL):
            try:
                stream = await generate(model, "chat", stream=True, attempts=1, chain=False,
                                        contents=contents, system_instruction=system, temperature=0.1,
                                        http_options=CHAT_HTTP_OPTIONS)
                async for chunk in stream:
                    if chunk.text:
                        started = True
                        yield chunk.text
                return
            except (errors.APIError, *NETWORK_ERRORS) as e:
                if started or not switchable(e):
                    raise
                gemini_error = e
                note_failure(model, e)
                log.warning("chat: %s failed before the first word (%s)", model, err_name(e))
    if not should_fall_back(gemini_error):
        raise gemini_error
    log.warning("All Gemini models failed (%s); answering with Groq %s", err_name(gemini_error), config.GROQ_MODEL)
    messages = [{"role": h.role, "content": CITATION.sub("", h.content) if h.role == "assistant" else h.content}
                for h in history] + [{"role": "user", "content": question}]
    try:
        async for text in fallback.stream_chat(system, messages, temperature=0.1):
            yield text
    except Exception as fe:
        log.error("Groq fallback failed too: %s", fe)
        raise gemini_error from fe  # report the original problem (e.g. quota) to the user


async def stream_answer(site, question: str, history: list):
    """Yield SSE event dicts: {"type": "sources"}, {"type": "token", "text"} ..., then {"type": "done", "sources"}."""
    t0 = time.perf_counter()
    names = dict(site_name=site.name, site_url=site.url, topics=format_topics(site.topics))
    history = history[-8:]  # last 4 user/assistant turns
    small_talk = bool(SMALL_TALK.match(question.strip()))
    prev = next((h.content for h in reversed(history) if h.role == "user"), None)
    follow_up = bool(prev and FOLLOW_UP.search(question))

    # A question that doesn't lean on earlier turns gets the same answer every time: serve it from the cache.
    key = cache_key(question) if not follow_up else None
    cached = _cache_get(site, key) if key else None
    if cached:
        numbered, answer, sources = cached
        log.info("Question %r answered from cache", question[:80])
        yield {"type": "sources", "sources": numbered}
        yield {"type": "token", "text": answer}
        yield {"type": "done", "sources": sources}
        return

    hits = []
    if not small_talk:
        queries = [question] + ([f"{prev}\n{question}"] if follow_up else [])
        hits, scores = await _retrieve(site.id, queries)
        log.info("Question %r best %s %s (gate %.2f)", question[:80], "rerank score" if reranker else "similarity",
                 " / with previous question ".join(f"{x:.3f}" for x in scores), GATE)
        if not hits:
            log.info("No relevant content (%.0f ms)", (time.perf_counter() - t0) * 1000)
            yield {"type": "token", "text": NO_INFO.format(**names)}
            yield {"type": "done", "sources": []}
            return

    numbered = number_sources(hits)
    num = {s["url"]: s["n"] for s in numbered}
    context = "\n\n---\n\n".join(f"[{num[m['url']]}] Page: {m['title']} | {m['url']}\n{doc}" for _, doc, m in hits)
    system = SYSTEM_PROMPT.format(**names) + "\n\nCONTEXT:\n" + (context or "(no website content needed)")

    yield {"type": "sources", "sources": numbered}  # lets the page render citation links while streaming
    t1 = time.perf_counter()
    answer, first = "", None
    async for text in _llm_stream(system, history, question):
        if first is None:
            first = time.perf_counter()
        answer += text
        yield {"type": "token", "text": text}
    done = time.perf_counter()
    log.info("Answer timing: retrieval %.0f ms, first token %.0f ms, full answer %.0f ms",
             (t1 - t0) * 1000, ((first or done) - t1) * 1000, (done - t0) * 1000)

    sources = []
    if not NO_INFO_PREFIX.match(answer):
        cited = {int(n) for m in CITATION.findall(answer) for n in re.findall(r"\d+", m)}
        sources = [s for s in numbered if s["n"] in cited]
        if not sources:  # the model forgot to cite: fall back to the strongly matching pages
            strong = {m["url"] for sc, _, m in hits if sc >= GATE}
            sources = [s for s in numbered if s["url"] in strong]
    if key and answer.strip():
        _cache_put(site, key, (numbered, answer, sources))
    yield {"type": "done", "sources": sources}
