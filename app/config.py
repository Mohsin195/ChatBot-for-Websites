import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Models are public, so never send a (possibly expired) Hugging Face login token when downloading them.
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
if not GEMINI_API_KEY:
    sys.exit(
        "\nERROR: GEMINI_API_KEY is not set.\n"
        "Copy .env.example to .env and paste your key after GEMINI_API_KEY=\n"
        "Get a free key at https://aistudio.google.com/apikey\n"
    )

CHAT_MODEL = os.getenv("CHAT_MODEL", "gemini-3.5-flash-lite")
# Tried in order when a model is out of quota, overloaded or retired (each model has its own free quota).
BACKUP_MODELS = [m.strip() for m in os.getenv("BACKUP_MODELS", "gemini-3.1-flash-lite,gemini-3.5-flash").split(",") if m.strip()]
# Thinking adds seconds before the first answer word; grounded RAG answers rarely need it.
# One of minimal | low | medium | high, or empty to use the model's default.
CHAT_THINKING = os.getenv("CHAT_THINKING", "minimal").strip().lower()
# Voice questions are transcribed by Gemini with the site's own vocabulary (brand and product names).
STT_MODEL = os.getenv("STT_MODEL", "gemini-3.5-flash-lite")
# Seconds to wait for Gemini to start answering (and between streamed words) before retrying or falling back.
CHAT_TIMEOUT = _float("CHAT_TIMEOUT", 15)
STT_TIMEOUT = _float("STT_TIMEOUT", 8)  # seconds per model for voice transcription

# Optional fallback when Gemini is rate-limited, overloaded or out of quota (free key: https://console.groq.com/keys)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")

# Local models (ONNX via FastEmbed, CPU only, no API quota)
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
RERANK_MODEL = os.getenv("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2").strip()
if RERANK_MODEL.lower() == "off":
    RERANK_MODEL = ""
MODEL_CACHE_DIR = os.getenv("MODEL_CACHE_DIR", "model_cache")

# Retrieval: fetch RETRIEVE_CANDIDATES chunks by meaning, rerank them, keep the best TOP_K.
TOP_K = _int("TOP_K", 6)
RETRIEVE_CANDIDATES = _int("RETRIEVE_CANDIDATES", 12)
# With a reranker, a question is answered only if the best chunk's reranker score reaches RERANK_THRESHOLD.
# Without one, the cosine similarity of the best chunk must reach RELEVANCE_THRESHOLD.
RERANK_THRESHOLD = _float("RERANK_THRESHOLD", -10.0)
RELEVANCE_THRESHOLD = _float("RELEVANCE_THRESHOLD", 0.60)
ANSWER_CACHE_SIZE = _int("ANSWER_CACHE_SIZE", 100)  # cached answers per website

CRAWL_CONCURRENCY = _int("CRAWL_CONCURRENCY", 8)
DEFAULT_MAX_PAGES = _int("DEFAULT_MAX_PAGES", 200)
MAX_PAGES_LIMIT = _int("MAX_PAGES_LIMIT", 500)
REQUEST_TIMEOUT = _float("REQUEST_TIMEOUT", 15)
USE_PLAYWRIGHT = _bool("USE_PLAYWRIGHT", True)
USER_AGENT = os.getenv("USER_AGENT", "SiteSenseBot/1.0 (+website-to-chatbot crawler)")

# Multi-user limits. Each visitor (browser session) gets their own website and chatbot.
MAX_CONCURRENT_CRAWLS = _int("MAX_CONCURRENT_CRAWLS", 2)   # more crawls wait in a queue
MAX_ACTIVE_SITES = _int("MAX_ACTIVE_SITES", 20)            # oldest idle chatbot is removed beyond this
SITE_TTL_MINUTES = _int("SITE_TTL_MINUTES", 60)            # chatbots unused this long are deleted
# Per client IP; 0 disables a limit.
CRAWLS_PER_HOUR = _int("CRAWLS_PER_HOUR", 10)
QUESTIONS_PER_MINUTE = _int("QUESTIONS_PER_MINUTE", 15)
VOICE_PER_MINUTE = _int("VOICE_PER_MINUTE", 12)
TTS_PER_MINUTE = _int("TTS_PER_MINUTE", 20)
# Set true only behind a reverse proxy (Hugging Face Spaces, Render, Nginx…) so the real client IP is used.
TRUST_PROXY_HEADERS = _bool("TRUST_PROXY_HEADERS", False)

CHROMA_DIR = os.getenv("CHROMA_DIR", "chroma_data")
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-AriaNeural")
