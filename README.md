# SiteSense AI

Turn any website into an AI assistant. Paste a URL. SiteSense crawls the site's pages (from the sitemap and from internal links) and builds a knowledge base with local embeddings (FastEmbed, ONNX) and ChromaDB. A local reranker picks the most relevant passages, and Gemini writes the answers, with Groq as an optional fallback. The chatbot answers **only** from that website, links each claim to its source page, and can read answers aloud. You can also ask questions by voice.

Every visitor gets their own chatbot. Visitors are told apart by an anonymous session cookie, so one person building a chatbot never replaces another person's. Building a new website replaces only your own chatbot.

## Requirements

- Python 3.11 or newer
- A Google Gemini API key (free): https://aistudio.google.com/apikey
- Optional, recommended: a Groq API key (free): https://console.groq.com/keys. Groq takes over when Gemini is rate-limited, overloaded or out of quota.

## Setup

### Windows (PowerShell)

```powershell
cd "D:\Chatbot For Website"
py -3.13 -m venv .venv          # or: python -m venv .venv
.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements-browser.txt
playwright install chromium
copy .env.example .env
notepad .env                    # paste your key after GEMINI_API_KEY= (and optionally GROQ_API_KEY=)
uvicorn app.main:app --reload --reload-dir app --reload-dir static
```

### macOS / Linux

```bash
cd /path/to/project
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-browser.txt
playwright install chromium     # on Linux servers: playwright install --with-deps chromium
cp .env.example .env
nano .env                       # paste your key after GEMINI_API_KEY= (and optionally GROQ_API_KEY=)
uvicorn app.main:app --reload --reload-dir app --reload-dir static
```

Open http://127.0.0.1:8000.

On first start, the embedding model (~70 MB) and the reranker (~80 MB) are downloaded into `model_cache/`. Later starts load them in about a second. If `GEMINI_API_KEY` is missing, the server stops at startup with a message telling you where to set it.

`--reload-dir` keeps the auto-reloader from restarting the server whenever the model cache or database changes.

## Deploy with Docker

```bash
docker build -t sitesense .
docker run -p 8000:8000 --env-file .env sitesense
```

The image installs Chromium for Playwright. Every setting comes from environment variables, so the same image runs on Hugging Face Spaces, Railway, Fly.io or a VPS. Set `GEMINI_API_KEY` (and `GROQ_API_KEY`) as secrets in the host's dashboard. Behind the host's proxy, also set `TRUST_PROXY_HEADERS=true` so rate limits see each visitor's real IP. The container listens on `$PORT` if the host provides it, and on 8000 otherwise.

Run a **single** worker process: chatbots, queues and rate limits are held in memory.

## Deploy on Vercel

Deploy the repository root as FastAPI and set `GEMINI_API_KEY` in the deployment environment. The base requirements exclude Playwright to keep the Python function bundle smaller. Vercel crawls the HTML available without browser rendering and completes each build within the crawl request. Chatbot sessions and indexes are tied to a function instance and can expire when Vercel recycles it.

To verify a preview, build a chatbot for a site you know, check that the crawl reaches **Ready**, then ask about a fact on a linked page and confirm the citation points to that page. Refresh the preview and confirm the chatbot is still available. The backend logs report crawl, indexing and topic-generation time separately.

## How it works

1. **Discover**: normalizes the URL (adds `https://`, follows redirects, treats `www` and non-`www` as the same site) and reads `robots.txt`, `sitemap.xml`, sitemap index files and any sitemap listed in robots.txt.
2. **Queue**: at most `MAX_CONCURRENT_CRAWLS` crawls run at once, because crawling and headless Chromium are heavy. Later requests wait in line, and the progress screen shows how many are ahead.
3. **Crawl**: breadth-first, `CRAWL_CONCURRENCY` pages at a time, with timeouts and retries. Stays on the same domain, respects robots.txt and skips files (pdf, images, css, js…) and non-content paths (login, cart, checkout, wp-admin, `?replytocom`…). Text is extracted with trafilatura, keeping headings. A page with under 200 characters of text is re-rendered in headless Chromium. Near-empty pages and exact duplicates are skipped.
4. **Index**: ~900-character chunks with 150-character overlap, split on headings, then paragraphs, then sentences, each prefixed with its page title. Chunks are embedded locally with `BAAI/bge-small-en-v1.5` (FastEmbed, ONNX, CPU, no PyTorch) and stored in a ChromaDB collection for that visitor's site. One LLM call turns the page titles into 3–5 topics for the suggested questions; if that fails, the chatbot is still ready, just without suggestions.
5. **Retrieve and rerank**: the question is embedded and the `RETRIEVE_CANDIDATES` (20) closest chunks are fetched. A local cross-encoder reranker (`Xenova/ms-marco-MiniLM-L-6-v2`) reads the question together with each chunk, re-orders them, and the best `TOP_K` (6) are kept. If even the best reranker score is below `RERANK_THRESHOLD`, the bot returns the standard "I don't have information about that" reply **without calling the LLM**. Greetings and thanks skip this check so the bot can reply warmly. Follow-up questions ("how much does it cost?") are also retrieved together with the previous question.
6. **Answer**: Gemini streams the answer at temperature 0.1, using a strict system prompt, the last 4 chat turns and the retrieved passages. Each passage's page gets a number, and the model cites it after every sentence (`[2]`). The page renders each citation as a 🔗 link: hovering it shows the page title and URL, and clicking opens that page.
   - If Gemini fails before the first word (rate limit, quota, overload, or no response within `CHAT_TIMEOUT` seconds), the request is retried briefly and then answered by Groq (`GROQ_MODEL`) when `GROQ_API_KEY` is set.
   - Answers to questions that don't depend on earlier messages are cached per site, so asking the same question again (ignoring case and punctuation) is instant and uses no quota.
7. **Voice in**: while you speak, the browser's built-in recognizer (Chrome, Edge) shows live text. The audio is also recorded, converted to 16 kHz WAV and sent to `/api/transcribe`, where `STT_MODEL` transcribes it with the site's vocabulary (its name, topics and recurring proper nouns), so brand names are spelled the way the website spells them. If Gemini fails, Groq Whisper (`GROQ_STT_MODEL`) transcribes instead. The recording stops on its own after a pause in speech, and works even when the browser's online speech service is unreachable (company networks, VPNs).
8. **Voice out**: answers are read aloud with an edge-tts neural voice. If that service fails or is blocked, the browser's built-in speech (Web Speech API) reads the answer instead.

## Multiple visitors and limits

- Each browser gets a separate chatbot via an anonymous, HttpOnly session cookie.
- Chatbots unused for `SITE_TTL_MINUTES` are deleted. Beyond `MAX_ACTIVE_SITES`, the least recently used finished chatbot is removed to make room. A visitor whose chatbot expired is asked to build it again.
- Per-IP rate limits protect the shared API quota: `CRAWLS_PER_HOUR`, `QUESTIONS_PER_MINUTE`, `VOICE_PER_MINUTE`, `TTS_PER_MINUTE`. Over the limit, the visitor sees how long to wait.
- `MAX_PAGES_LIMIT` caps the pages a visitor can request; the page reads this limit from the server.

## API

All endpoints act on the caller's own session (cookie set automatically on the first request).

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/crawl` | `{url, max_pages}` | `{site_id}`; the crawl runs in the background (queued if all crawl slots are busy) |
| GET | `/api/status` | | `{site_id, stage, pages_found, pages_crawled, chunks_indexed, message, max_pages_limit, …}`; stage is `idle`, `queued`, `discovering`, `crawling`, `indexing`, `ready` or `error` |
| POST | `/api/chat` | `{site_id, question, history}` | SSE stream: `{"type":"sources","sources":[{n,url,title}]}`, `{"type":"token"}` events, then `{"type":"done","sources":[…cited…]}` |
| POST | `/api/transcribe?site_id=…` | raw audio (`audio/wav`, `webm`, `ogg`, `mpeg`, `mp4`) | `{text}` transcribed with the site's vocabulary |
| POST | `/api/tts` | `{text}` | `{url}`; then GET that URL for streamed `audio/mpeg` (markdown, citations and URLs removed first) |
| POST | `/api/reset` | | deletes the caller's chatbot |

A chat request for a site that isn't the caller's current one gets HTTP 409. Rate-limited requests get HTTP 429 with a `Retry-After` header.

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | (required) | Google Gemini API key. |
| `CHAT_MODEL` | `gemini-flash-latest` | Model for answers and topic generation. |
| `CHAT_THINKING` | `minimal` | Gemini thinking level: `minimal`, `low`, `medium`, `high`, or empty for the model default. More thinking means a slower first word. |
| `CHAT_TIMEOUT` | `30` | Seconds to wait for Gemini to start answering before retrying or falling back. |
| `STT_MODEL` | `gemini-flash-lite-latest` | Model that transcribes voice questions. |
| `GROQ_API_KEY` | (empty) | Enables the Groq fallback for answers, topics and voice transcription. |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Groq chat model used as fallback. |
| `GROQ_STT_MODEL` | `whisper-large-v3-turbo` | Groq speech-to-text model used as fallback. |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | FastEmbed embedding model (runs locally on CPU). Changing it requires building chatbots again. |
| `RERANK_MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | FastEmbed reranker. Empty disables reranking. |
| `MODEL_CACHE_DIR` | `model_cache` | Folder for downloaded local models. |
| `TOP_K` | `6` | Passages given to the LLM per question. |
| `RETRIEVE_CANDIDATES` | `20` | Passages fetched before reranking. |
| `RERANK_THRESHOLD` | `-10` | Minimum reranker score of the best passage before the LLM is called. See tuning below. |
| `RELEVANCE_THRESHOLD` | `0.60` | Minimum cosine similarity instead, used only when reranking is disabled. |
| `ANSWER_CACHE_SIZE` | `100` | Cached answers per chatbot; `0` disables caching. |
| `CRAWL_CONCURRENCY` | `8` | Pages fetched at the same time within one crawl. |
| `DEFAULT_MAX_PAGES` | `200` | Page cap shown by default. |
| `MAX_PAGES_LIMIT` | `500` | Highest page cap a visitor can choose. |
| `REQUEST_TIMEOUT` | `15` | Seconds before a page request times out (each page gets 3 attempts). |
| `USE_PLAYWRIGHT` | `true` | Render JavaScript-heavy pages in headless Chromium. Set `false` if Chromium isn't installed. |
| `USER_AGENT` | `SiteSenseBot/1.0 (…)` | User-Agent sent to websites and matched against robots.txt. |
| `MAX_CONCURRENT_CRAWLS` | `2` | Crawls running at once; more wait in a queue. |
| `MAX_ACTIVE_SITES` | `20` | Chatbots kept at once. |
| `SITE_TTL_MINUTES` | `60` | Unused chatbots are deleted after this long. |
| `CRAWLS_PER_HOUR` | `10` | Per IP; `0` disables. |
| `QUESTIONS_PER_MINUTE` | `15` | Per IP; `0` disables. |
| `VOICE_PER_MINUTE` | `12` | Voice transcriptions per IP; `0` disables. |
| `TTS_PER_MINUTE` | `20` | Read-aloud requests per IP; `0` disables. |
| `TRUST_PROXY_HEADERS` | `false` | Set `true` behind a reverse proxy so the real client IP is used. Never enable it without a proxy, or clients could fake their IP. |
| `CHROMA_DIR` | `chroma_data` | Folder for the ChromaDB database. |
| `TTS_VOICE` | `en-US-AriaNeural` | edge-tts voice. List voices with `edge-tts --list-voices`. |

## Tuning `RERANK_THRESHOLD`

Every question logs its best reranker score:

```
sitesense.rag: Question 'what are your opening hours' best rerank score 6.412 (gate -10.00)
```

With the default reranker, passages that answer the question usually score above 0. List-style pages (catalogues, category lists) can score as low as −8 even when they contain the answer. Unrelated questions ("What is the capital of France?") consistently scored around −10 to −11 in testing.

1. Build a chatbot for a site you know well.
2. Ask 5–10 questions the site answers and 5–10 unrelated ones, then compare the logged scores.
3. Set the threshold between the lowest on-topic score and the highest off-topic score.
   - **Real questions get "I don't have information"**: lower it (for example −10.5).
   - **Off-topic questions reach the LLM**: raise it (for example −9). Even then, the system prompt tells the model to refuse, so a slightly low threshold costs an LLM call but does not produce made-up answers.
4. Restart the server after editing `.env`.

## Notes and limits

- **Gemini usage**: embeddings and reranking run locally, so indexing and searching don't use any API quota. Gemini is used for topics, answers and voice transcription. Free-tier daily quotas are small; with `GROQ_API_KEY` set, Groq answers when Gemini's quota runs out.
- **Chat model**: `gemini-flash-latest` is an alias Google points at its current Flash model, and it can be slow or overloaded at busy times. With a Groq key, slow or failing requests switch to Groq after `CHAT_TIMEOUT`.
- **Language**: the default embedding and reranker models are English. For other languages, choose multilingual models supported by FastEmbed (for example `jinaai/jina-reranker-v2-base-multilingual`).
- **Voice input** needs a secure page (https, or localhost) and microphone permission. When the browser's live recognizer is unavailable (Firefox, or blocked networks), the recording is still transcribed by the server.
- **Security**: URLs are validated. Only http(s) on standard ports is allowed, and hosts that resolve to localhost or private IPs are refused. Every request, including redirects, is checked again.
- **Sites that block crawlers**, require a login, or serve content only behind interaction will show a clear error or produce fewer pages.
