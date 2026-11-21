import asyncio
import gzip
import hashlib
import html as htmllib
import ipaddress
import logging
import re
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from bs4 import BeautifulSoup

from . import config

log = logging.getLogger("sitesense.crawler")

JS_FALLBACK_CHARS = 200  # below this, the page is probably rendered by JavaScript
MIN_TEXT_CHARS = 80  # below this, the page is treated as empty
# Interstitial pages from anti-bot services (Cloudflare, SiteGround, Sucuri, DDoS-Guard, Imunify…)
BOT_CHECK = re.compile(
    r"<title>\s*(one moment, please|just a moment|attention required|please wait|checking your browser|ddos-guard)"
    r"|request is being verified|sgcaptcha|cf-browser-verification|challenge-platform|cf_chl_"
    r"|sucuri website firewall|imunify360",
    re.I,
)
BOT_CHECK_MESSAGE = (
    "This website is protected by a bot check (a “verifying your browser” page) that blocks automated "
    "crawlers, so its pages can't be read. If you own the site, allow crawlers in your hosting or firewall "
    "security settings (or temporarily turn the bot check off) and try again."
)

SKIP_EXT = re.compile(
    r"\.(pdf|jpe?g|png|gif|webp|avif|svg|ico|bmp|tiff?|zip|rar|7z|gz|tar|tgz|exe|dmg|msi|apk|"
    r"css|js|mjs|json|xml|rss|atom|txt|csv|xlsx?|docx?|pptx?|odt|mp[34]|m4a|wav|ogg|avi|mov|"
    r"wmv|webm|woff2?|ttf|eot|otf)$",
    re.I,
)
SKIP_PATH = re.compile(
    r"/(wp-admin|wp-login|wp-json|login|log-in|signin|sign-in|signup|sign-up|register|logout|"
    r"cart|basket|checkout|my-account|account|feed|cdn-cgi)(/|$|\.)",
    re.I,
)
SKIP_QUERY = re.compile(r"(replytocom|add-to-cart|share=|action=)", re.I)


class CrawlError(Exception):
    """An error whose message is safe to show to the user."""


@dataclass
class Page:
    url: str
    title: str
    text: str


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


async def _assert_public(host: str) -> None:
    """SSRF guard: refuse hosts that resolve to loopback, private, or reserved addresses."""
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
        raise CrawlError("Local and internal addresses are not allowed.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        raise CrawlError(f"Could not find the website {host}. Check the URL.")
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise CrawlError("This address points to a private or local network and is not allowed.")


async def validate_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise CrawlError("Please enter a website URL.")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise CrawlError("That doesn't look like a valid URL.")
    if parts.scheme.lower() not in ("http", "https"):
        raise CrawlError("Only http:// and https:// websites are supported.")
    host = (parts.hostname or "").lower()
    if "." not in host.strip("."):
        raise CrawlError("That doesn't look like a valid website address (for example: yoursite.com).")
    if port not in (None, 80, 443):
        raise CrawlError("Only standard web ports (80 and 443) are supported.")
    await _assert_public(host)
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))


def extract(html: str, url: str) -> tuple[str, str, set[str], dict]:
    """Return (title, markdown text with headings, links, homepage metadata)."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    og = soup.find("meta", attrs={"property": "og:site_name"})
    icon = soup.find("link", rel=lambda v: v and "icon" in v.lower())
    meta = {
        "site_name": (og.get("content") or "").strip() if og else "",
        "icon": urljoin(url, icon["href"]) if icon and len(icon.get("href", "")) > 6 else urljoin(url, "/favicon.ico"),
    }
    links = {urljoin(url, a["href"]) for a in soup.find_all("a", href=True)}
    text = trafilatura.extract(
        html, url=url, output_format="markdown", include_tables=True,
        include_links=False, include_images=False, favor_recall=True,
    ) or ""
    if len(text) < JS_FALLBACK_CHARS:
        # trafilatura drops short pages (e.g. contact pages); fall back to visible body text
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "svg", "form"]):
            tag.decompose()
        body = soup.body.get_text("\n", strip=True) if soup.body else ""
        if len(body) > len(text):
            text = body
    return title or url, text.strip(), links, meta


class Renderer:
    """Headless Chromium on its own thread and event loop.

    A dedicated loop keeps Playwright working on Windows even when uvicorn runs a
    selector event loop (which cannot spawn subprocesses).
    """

    def __init__(self):
        self.loop = None
        self.pw = None
        self.browser = None
        self.disabled = not config.USE_PLAYWRIGHT
        self.sem = asyncio.Semaphore(3)

    async def _route(self, route):
        req = route.request
        host = urlsplit(req.url).hostname or ""
        if req.resource_type in ("image", "media", "font", "stylesheet"):
            return await route.abort()
        try:
            private = not ipaddress.ip_address(host).is_global
        except ValueError:
            private = host == "localhost" or "." not in host
        await (route.abort() if private else route.continue_())

    async def _render(self, url: str) -> str:
        if self.browser is None:
            from playwright.async_api import async_playwright
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch()
        page = await self.browser.new_page(user_agent=config.USER_AGENT)
        try:
            await page.route("**/*", self._route)
            try:
                await page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:
                pass  # slow or chatty pages: use whatever has rendered so far
            return await page.content()
        finally:
            await page.close()

    async def _shutdown(self):
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()

    async def render(self, url: str) -> str | None:
        if self.disabled:
            return None
        if self.loop is None:
            loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
            threading.Thread(target=lambda: (loop.run_forever(), loop.close()), daemon=True).start()
            self.loop = loop
        async with self.sem:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._render(url), self.loop)
                return await asyncio.wait_for(asyncio.wrap_future(fut), 40)
            except Exception as e:
                if self.browser is None:
                    self.disabled = True
                    log.warning("Playwright unavailable, JavaScript rendering disabled: %s", e)
                else:
                    log.warning("Render failed for %s: %s", url, e)
                return None

    async def close(self):
        if self.loop:
            try:
                await asyncio.wait_for(asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)), 10)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)


class Crawler:
    """Breadth-first crawl of one site, seeded from the homepage and its sitemaps.

    `progress` is any object with stage/url/pages_found/pages_crawled/message attributes.
    """

    def __init__(self, start_url: str, max_pages: int, progress):
        self.start_url = start_url
        self.max_pages = max_pages
        self.progress = progress
        self.pages: list[Page] = []
        self.seen: set[str] = set()
        self.hashes: set[str] = set()
        self.verified_hosts: set[str] = set()
        self.attempts = 0
        self.found = 0
        self.skipped = Counter()  # reason -> count, for the crawl summary
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.base = ""  # domain without www; set after the homepage redirect is resolved
        self.site_name = ""
        self.icon = ""
        self.robots = RobotFileParser()
        self.renderer = Renderer()

    async def _guard(self, request: httpx.Request):
        # Runs for every request, including redirects: stay on-site and on public IPs.
        host = request.url.host
        if self.base and _bare(host) != self.base:
            raise CrawlError(f"Off-site redirect to {host} blocked")
        if host not in self.verified_hosts:
            await _assert_public(host)
            self.verified_hosts.add(host)

    async def _get(self, url: str, attempts: int = 3) -> httpx.Response | None:
        for i in range(attempts):
            try:
                r = await self.client.get(url)
                if r.status_code in (429, 500, 502, 503, 504) and i < attempts - 1:
                    await asyncio.sleep(2 ** i)
                    continue
                return r
            except CrawlError as e:
                log.info("Skipped %s: %s", url, e)
                return None
            except httpx.HTTPError as e:
                if i == attempts - 1:
                    log.warning("Failed %s: %s", url, type(e).__name__)
                    return None
                await asyncio.sleep(2 ** i)
        return None

    def _key(self, url: str) -> str | None:
        """Canonical form used for de-duplication, or None if the URL should be skipped."""
        try:
            p = urlsplit(url)
        except ValueError:
            return None
        host = (p.hostname or "").lower()
        if p.scheme not in ("http", "https") or _bare(host) != self.base:
            return None
        path = re.sub(r"/{2,}", "/", p.path or "/")
        if SKIP_EXT.search(path) or SKIP_PATH.search(path) or SKIP_QUERY.search(p.query):
            return None
        return urlunsplit((self.scheme, self.host, path.rstrip("/") or "/", p.query, ""))

    def _enqueue(self, url: str):
        key = self._key(url)
        if not key or key in self.seen:
            return
        self.seen.add(key)
        if not self.robots.can_fetch(config.USER_AGENT, key):
            return
        p = urlsplit(url)
        self.queue.put_nowait(urlunsplit((self.scheme, self.host, p.path or "/", p.query, "")))
        self.found += 1
        self.progress.pages_found = self.found

    async def _load_robots(self, root: str):
        r = await self._get(f"{root}/robots.txt", attempts=2)
        if r is not None and r.status_code in (401, 403):
            self.robots.disallow_all = True
        elif r is not None and r.status_code == 200:
            self.robots.parse(r.text.splitlines())
        else:
            self.robots.allow_all = True

    async def _sitemap_urls(self, root: str) -> list[str]:
        pending = list(self.robots.site_maps() or []) + [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"]
        # Sites hosted under a sub-path (e.g. GitHub Pages) keep their sitemap there.
        folder, _, last = urlsplit(self.progress.url).path.rstrip("/").rpartition("/")
        folder = folder if "." in last else f"{folder}/{last}".rstrip("/")
        if folder:
            pending.insert(0, f"{root}{folder}/sitemap.xml")
        done, urls = set(), []
        while pending and len(done) < 100:
            sm = pending.pop(0)
            if sm in done:
                continue
            done.add(sm)
            r = await self._get(sm, attempts=2)
            if r is None or r.status_code != 200:
                continue
            body = r.content
            if body[:2] == b"\x1f\x8b":
                try:
                    body = gzip.decompress(body)
                except OSError:
                    continue
            text = body.decode("utf-8", "ignore")
            locs = [
                htmllib.unescape(re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.strip()))
                for m in re.findall(r"<loc>(.*?)</loc>", text, re.S | re.I)
            ]
            if re.search(r"<sitemapindex", text, re.I):
                pending += locs
            else:
                urls += locs
        log.info("Sitemaps: %d files read, %d URLs listed", len(done), len(urls))
        return urls

    async def _process(self, url: str):
        r = await self._get(url)
        if r is None or r.status_code != 200:
            self.skipped["unreachable" if r is None else f"http {r.status_code}"] += 1
            return
        if "html" not in r.headers.get("content-type", ""):
            self.skipped["not html"] += 1
            return
        final = str(r.url)
        key = self._key(final)
        if key:
            self.seen.add(key)  # avoid re-crawling the redirect target
        title, text, links, meta = await asyncio.to_thread(extract, r.text, final)
        rendered = None
        if len(text) < JS_FALLBACK_CHARS:
            rendered = await self.renderer.render(final)
            if rendered:
                t2, x2, l2, m2 = await asyncio.to_thread(extract, rendered, final)
                links |= l2
                if len(x2) > len(text):
                    title, text, meta = t2, x2, m2
        if key == self._key(self.progress.url) and not self.site_name:
            self.site_name, self.icon = meta["site_name"], meta["icon"]
        for link in links:
            self._enqueue(link)
        if len(text) < MIN_TEXT_CHARS:
            blocked = BOT_CHECK.search(r.text) and (rendered is None or BOT_CHECK.search(rendered))
            self.skipped["bot check" if blocked else "empty"] += 1
            return
        digest = hashlib.sha1(text.encode()).hexdigest()
        if digest in self.hashes:
            self.skipped["duplicate"] += 1
            return
        if len(self.pages) >= self.max_pages:
            return
        self.hashes.add(digest)
        self.pages.append(Page(final, title, text))
        self.progress.pages_indexed = len(self.pages)

    async def _worker(self):
        while True:
            url = await self.queue.get()
            try:
                # Fetch budget leaves room for duplicates/empty pages without crawling forever.
                if len(self.pages) < self.max_pages and self.attempts < self.max_pages * 2 + 50:
                    self.attempts += 1
                    await self._process(url)
                    self.progress.pages_crawled += 1
                    if self.progress.pages_crawled % 25 == 0:
                        log.info("Crawled %d / %d found (%d kept)", self.progress.pages_crawled,
                                 self.progress.pages_found, len(self.pages))
            except Exception:
                log.exception("Error processing %s", url)
            finally:
                self.queue.task_done()

    async def run(self) -> list[Page]:
        headers = {"User-Agent": config.USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        async with httpx.AsyncClient(
            headers=headers, timeout=config.REQUEST_TIMEOUT, follow_redirects=True,
            event_hooks={"request": [self._guard]},
            limits=httpx.Limits(max_connections=config.CRAWL_CONCURRENCY * 2),
        ) as self.client:
            try:
                return await self._run()
            finally:
                await self.renderer.close()

    async def _run(self) -> list[Page]:
        self.progress.stage, self.progress.message = "discovering", "Connecting to the website…"
        r = await self._get(self.start_url)
        if r is None:
            raise CrawlError("Could not reach the website. It may be down, too slow, or blocking automated access.")
        if r.status_code in (401, 403, 429):
            raise CrawlError(f"The website refused access (HTTP {r.status_code}). It may block crawlers.")
        if r.status_code >= 400:
            raise CrawlError(f"The website returned an error (HTTP {r.status_code}).")
        self.scheme, self.host = r.url.scheme, r.url.host
        self.base = _bare(self.host)
        root = f"{self.scheme}://{self.host}"
        self.progress.url = f"{root}{r.url.path or '/'}"

        await self._load_robots(root)
        if not self.robots.can_fetch(config.USER_AGENT, self.progress.url):
            raise CrawlError("This website's robots.txt does not allow crawling.")

        self.progress.message = "Reading sitemaps…"
        self._enqueue(self.progress.url)
        for u in await self._sitemap_urls(root):
            self._enqueue(u)
        log.info("Starting crawl of %s with %d seed URLs", root, self.queue.qsize())

        self.progress.stage, self.progress.message = "crawling", "Crawling pages…"
        workers = [asyncio.create_task(self._worker()) for _ in range(config.CRAWL_CONCURRENCY)]
        try:
            await self.queue.join()
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        log.info("Crawl finished: %d fetched, %d pages kept, skipped: %s",
                 self.attempts, len(self.pages), dict(self.skipped) or "none")
        if not self.pages and self.skipped["bot check"]:
            raise CrawlError(BOT_CHECK_MESSAGE)
        return self.pages
