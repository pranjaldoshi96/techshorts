"""Article text for the LLM (spec 2.5).

Order: page text (trafilatura, only for `fetch_page` sources) → boilerplate stripped → capped at 800 words.
Under 120 words, the longer of the feed text and og:description + feed excerpt is used instead. Text only ever
comes from the item's own page or feed entry, never from another item. Under 60 words the item is "thin": it
may join an event later but never leads one, and without text it is held for retries (see llm.py).

Network work (`fetch_page`) runs in the writer's thread pool; `choose` is pure so tests need no network.
"""
import html as html_mod
import re
import time
import threading
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import requests

MAX_WORDS = 800
MIN_PAGE_WORDS = 120
THIN_WORDS = 60
EXCERPT_WORDS = 60

# Counted after stripping, so a newsletter's "subscribe" footer can't make thin text look long enough.
BOILERPLATE = re.compile(r"subscribe|subscriber|sign up|newsletter|podcast feed|members-only|support our journalism",
                         re.I)
# The closing quote stays with its sentence (a lookbehind, not a consumed match).
_SENTENCE = re.compile(r"(?:(?<=[.!?…])|(?<=[.!?…][\"'”’)]))\s+(?=[\"'“‘(]?[A-Z0-9])")


def words(s):
    return len((s or "").split())


def cap_words(text, n=MAX_WORDS):
    parts = text.split()
    return text if len(parts) <= n else " ".join(parts[:n])


def sentences(text):
    return [s.strip() for s in _SENTENCE.split(text or "") if s.strip()]


def strip_boilerplate(paragraphs):
    return [p for p in paragraphs if p.strip() and not BOILERPLATE.search(p)]


def clean_feed(text):
    """Feed text arrives as one line (fetch.plain), so boilerplate is stripped per sentence instead."""
    return " ".join(strip_boilerplate(sentences(text)))


@dataclass
class ArticleText:
    text: str
    origin: str                 # page | feed | og+feed | none
    paragraphs: list = field(default_factory=list)
    og_image: str = None        # the page's og:image, when the page was fetched (images.py uses it first)

    @property
    def words(self):
        return words(self.text)

    @property
    def thin(self):
        return self.words < THIN_WORDS

    def lead(self, n=3, min_words=200):
        """First n paragraphs for the entailment check; keeps going until min_words so a site that splits its
        lede into one-line paragraphs (CNBC, BBC) still gives the judge the opening facts."""
        out = []
        for p in self.paragraphs or sentences(self.text):
            out.append(p)
            if len(out) >= n and words(" ".join(out)) >= min_words:
                break
        return " ".join(out)


def _join_capped(paragraphs, n=MAX_WORDS):
    out, total = [], 0
    for p in paragraphs:
        w = p.split()
        if total + len(w) > n:
            out.append(" ".join(w[:n - total]))
            break
        out.append(p)
        total += len(w)
    return [p for p in out if p]


def choose(page_paragraphs, feed_text, og_description=None):
    """Picks the item's text from what was fetched. Pure; every input belongs to the same item."""
    page = _join_capped(strip_boilerplate(page_paragraphs or []))
    if words(" ".join(page)) >= MIN_PAGE_WORDS:
        return ArticleText(" ".join(page), "page", page)
    feed = clean_feed(feed_text)
    og = clean_feed(html_mod.unescape(og_description or ""))
    excerpt = " ".join(feed.split()[:EXCERPT_WORDS])
    # og:description is often the feed excerpt itself; don't feed the model the same sentence twice.
    og_plus = excerpt if not og or _norm(og) in _norm(excerpt) else (
        og if _norm(excerpt[:80]) in _norm(og) else f"{og} {excerpt}".strip())
    options = [("page", " ".join(page), page), ("feed", feed, []), ("og+feed", og_plus, [])]
    # A short page extract still wins when it's the longest real text available.
    origin, text, paras = max(options, key=lambda o: words(o[1]))
    if not text.strip():
        return ArticleText("", "none", [])
    return ArticleText(cap_words(text), origin, _join_capped(paras) if paras else [])


def _norm(s):
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


# HTML

_OG = [re.compile(rx, re.I | re.S) for rx in (
    r'<meta[^>]+property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\']',
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*property=["\']og:description["\']',
    r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
)]


_OG_IMAGE = [re.compile(rx, re.I | re.S) for rx in (
    r'<meta[^>]+property=["\']og:image(?::secure_url|:url)?["\'][^>]*content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:image(?::secure_url|:url)?["\']',
    r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]*content=["\']([^"\']+)["\']',
)]


def og_image(page_html, base_url=None):
    head = page_html[:200_000]
    for rx in _OG_IMAGE:
        m = rx.search(head)
        if m and m.group(1).strip():
            u = html_mod.unescape(m.group(1)).strip()
            return urljoin(base_url, u) if base_url else u
    return None


def og_description(page_html):
    head = page_html[:200_000]
    for rx in _OG:
        m = rx.search(head)
        if m and m.group(1).strip():
            return html_mod.unescape(m.group(1)).strip()
    return None


class _Paragraphs(HTMLParser):
    """Fallback extractor (the one enrich.py used): <p> text, preferring what's inside <article>."""
    SKIP = ("script", "style", "nav", "footer", "aside", "figure", "noscript", "form")

    def __init__(self):
        super().__init__()
        self.skip = 0
        self.in_p = False
        self.in_article = 0
        self.buf = []
        self.paras = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "article":
            self.in_article += 1
        elif tag == "p":
            self.in_p, self.buf = True, []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "article":
            self.in_article = max(0, self.in_article - 1)
        elif tag == "p" and self.in_p:
            text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if words(text) >= 8:
                self.paras.append((self.in_article > 0, text))
            self.in_p = False

    def handle_data(self, data):
        if self.in_p and not self.skip:
            self.buf.append(data)


# trafilatura is not safe to call from several threads at once: a 6-thread replay died with "double free or
# corruption" inside it. Extraction takes milliseconds next to the fetch, so one lock costs nothing.
_EXTRACT_LOCK = threading.Lock()


def extract_paragraphs(page_html):
    try:
        import trafilatura
        with _EXTRACT_LOCK:
            out = trafilatura.extract(page_html, include_comments=False, include_tables=False, favor_precision=True)
        if out:
            return [re.sub(r"\s+", " ", p).strip() for p in out.split("\n") if p.strip()]
    except ImportError:
        pass
    p = _Paragraphs()
    p.feed(page_html[:1_500_000])
    inside = [t for a, t in p.paras if a]
    return [html_mod.unescape(t) for t in (inside if words(" ".join(inside)) >= 80 else [t for _, t in p.paras])]


# Network

class Robots:
    """robots.txt per host, cached for a day. Unreachable robots.txt (5xx, timeout) means no page fetch this time
    (RFC 9309); a missing one (4xx) allows everything."""
    TTL = 86400

    def __init__(self, user_agent, session=None, timeout=10):
        self.ua = user_agent
        self.session = session or requests.Session()
        self.timeout = timeout
        self.cache = {}
        self.lock = threading.Lock()

    def allowed(self, url):
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        with self.lock:
            hit = self.cache.get(host)
        if hit is None or time.time() - hit[0] > self.TTL:
            hit = (time.time(), self._load(host))
            with self.lock:
                self.cache[host] = hit
        rp = hit[1]
        return rp is not None and rp.can_fetch(self.ua, url)

    def _load(self, host):
        rp = robotparser.RobotFileParser()
        try:
            r = self.session.get(host + "/robots.txt", headers={"User-Agent": self.ua}, timeout=self.timeout)
        except requests.RequestException:
            return None
        if r.status_code >= 500:
            return None
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        return rp


def fetch_page(url, source, user_agent, robots, session=None, timeout=15, with_image=False):
    """Returns (paragraphs, og_description), plus og_image when `with_image`. Paragraphs only for fetch_page
    sources; paywalled ones (fetch_page: false) still give their og tags."""
    empty = ([], None, None) if with_image else ([], None)
    if not robots.allowed(url):
        return empty
    try:
        r = (session or requests).get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    except requests.RequestException:
        return empty
    if not r.ok or "html" not in r.headers.get("content-type", ""):
        return empty
    page = r.text
    out = ((extract_paragraphs(page) if source.fetch_page else []), og_description(page))
    return out + (og_image(page, r.url),) if with_image else out


def article_text(item, source, user_agent, robots, session=None):
    paragraphs, og, image = fetch_page(item["url"], source, user_agent, robots, session, with_image=True)
    t = choose(paragraphs, item.get("feed_text") or "", og)
    t.og_image = image or ""        # "" = looked, none found; None = never looked
    return t
