"""Polls feeds with conditional GETs and turns entries into items with honest timestamps (spec 2.3).

Network work (`fetch`) runs in a thread pool; everything that touches the database (`ingest`) runs serially
in the worker's main thread.
"""
import calendar
import hashlib
import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

from . import prefilter
from .db import from_iso, iso

# Tracking parameters that make one story look like several URLs.
_DROP_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ocid|cmpid|smid|guccounter|amp|outputtype)$", re.I)


def canonical_url(url):
    """Strips utm_*, fbclid and AMP variants so the same story from two feeds is one item."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host += f":{parts.port}"
    if host.startswith("amp."):
        host = host[4:]
    path = re.sub(r"/amp(/?)$", r"\1", parts.path)
    path = re.sub(r"\.amp(\.html)?$", r"\1", path) or "/"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not _DROP_PARAMS.match(k)])
    return urlunsplit(((parts.scheme or "https").lower(), host, path, query, ""))


# Timestamps

def resolve_published(feed_published_at, first_seen_at):
    """published_at = min(feed date, first seen). A future feed date can't make a story look newer than when we
    first saw it; a missing feed date falls back to first sight."""
    if feed_published_at is None:
        return first_seen_at
    return min(feed_published_at, first_seen_at)


def max_age(source, cfg):
    days = cfg["fetch"]["max_age_days_reads"] if source.reads else cfg["fetch"]["max_age_days"]
    return timedelta(days=min(days, 14))


def is_stale(published_at, now, source, cfg):
    return now - published_at > max_age(source, cfg)


# Parsing

@dataclass
class Entry:
    url: str
    url_canonical: str
    title: str
    feed_published_at: datetime = None
    text: str = ""
    image: str = None
    extra: dict = field(default_factory=dict)
    tags: tuple = ()


def _struct_to_dt(st):
    return datetime.fromtimestamp(calendar.timegm(st), timezone.utc) if st else None


def plain(s):
    """HTML to one line of text (feed titles and summaries)."""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _image(e):
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key) or []:
            if m.get("url") and (m.get("medium") in (None, "image") or "image" in (m.get("type") or "image")):
                return m["url"]
    for enc in e.get("enclosures") or []:
        if (enc.get("type") or "").startswith("image/") and enc.get("href"):
            return enc["href"]
    return None


def _signal_extra(source_id, e, summary_html):
    """Points and comments from hnrss; the linked article from Techmeme (whose own link is a permalink)."""
    extra = {}
    if source_id.startswith("hn"):
        for key, rx in (("points", r"Points:\s*(\d+)"), ("comments", r"# Comments:\s*(\d+)")):
            m = re.search(rx, summary_html)
            if m:
                extra[key] = int(m.group(1))
        m = re.search(r"Comments URL:\s*<a href=\"([^\"]+)\"", summary_html)
        if m:
            extra["hn_url"] = m.group(1)
    elif source_id == "techmeme":
        links = [u for u in re.findall(r'href="(https?://[^"]+)"', summary_html) if "techmeme.com" not in u]
        if links:
            extra["linked_url"] = canonical_url(html.unescape(links[0]))
    return extra


def parse_entries(body, source, limit):
    feed = feedparser.parse(body)
    out = []
    for e in feed.entries:
        link = (e.get("link") or "").strip()
        title = plain(e.get("title"))
        if not title or not link.startswith(("http://", "https://")):
            continue
        summary_html = e.get("summary") or ""
        content_html = max((c.get("value") or "" for c in e.get("content") or []), key=len, default="")
        text = plain(content_html if len(content_html) > len(summary_html) else summary_html)
        out.append(Entry(
            url=link,
            url_canonical=canonical_url(link),
            title=title,
            feed_published_at=_struct_to_dt(e.get("published_parsed") or e.get("updated_parsed")),
            text=text,
            image=_image(e),
            extra=_signal_extra(source.id, e, summary_html) if source.is_signal else {},
            tags=tuple(t.get("term") for t in e.get("tags") or () if t.get("term")),
        ))
    # Archive feeds (OpenAI lists 1,300+ posts) are not always newest-first; keep the newest `limit`.
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    out.sort(key=lambda x: x.feed_published_at or epoch, reverse=True)
    return out[:limit], feed


# Network

@dataclass
class FetchResult:
    source_id: str
    status: int = 0                 # HTTP status, 0 on a network error
    body: bytes = b""
    etag: str = None
    last_modified: str = None
    error: str = None
    ms: int = 0


def fetch(source, state, cfg, session=None, conditional=True):
    """One GET with If-None-Match / If-Modified-Since from the last 200."""
    headers = {"User-Agent": cfg["fetch"]["user_agent"], "Accept": "application/rss+xml, application/atom+xml, */*"}
    if conditional and state is not None:
        if state["etag"]:
            headers["If-None-Match"] = state["etag"]
        if state["last_modified"]:
            headers["If-Modified-Since"] = state["last_modified"]
    started = time.monotonic()
    res = FetchResult(source.id)
    try:
        r = (session or requests).get(source.url, headers=headers, timeout=cfg["fetch"]["timeout_s"])
        res.status = r.status_code
        if r.status_code == 200:
            res.body = r.content
            res.etag = r.headers.get("ETag")
            res.last_modified = r.headers.get("Last-Modified")
        elif r.status_code != 304:
            res.error = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        res.error = type(e).__name__
    res.ms = int((time.monotonic() - started) * 1000)
    return res


# Ingest

@dataclass
class IngestStats:
    entries: int = 0
    new: int = 0
    dropped: int = 0
    stale: int = 0
    seen: int = 0
    sightings: int = 0


def classify(entry, source, sources, now, first_seen, first_run, cfg):
    """Decides an entry's status at first sight. Returns (status, drop_rule, published_at).

    Age rules run before the prefilter so health.json counts only drops of items that would otherwise ship."""
    published = resolve_published(entry.feed_published_at, first_seen)
    if is_stale(published, now, source, cfg):
        return "stale", None, published
    # On a source's first poll, anything older than 36 h (or undated, so of unknown age) already existed.
    if first_run and (entry.feed_published_at is None
                      or now - published > timedelta(hours=cfg["fetch"]["first_run_skip_hours"])):
        return "seen", None, published
    if sources.blocked(entry.url):
        return "dropped", "domain_blocklist", published
    if source.include_tags and not set(entry.tags) & set(source.include_tags):
        return "dropped", "source_tags", published
    rule = prefilter.check(entry.title, entry.url)
    if rule:
        return "dropped", rule, published
    return ("signal" if source.is_signal else "new"), None, published


def ingest(db, source, sources, entries, now, first_run, cfg):
    stats = IngestStats(entries=len(entries))
    for e in entries:
        existing = db.item(e.url_canonical)
        first_seen = from_iso(existing["first_seen_at"]) if existing else now
        if db.add_sighting(e.url_canonical, source.id, e.title, e.feed_published_at, now):
            stats.sightings += 1
        # A URL first seen only on a signal source becomes a real item when a news source lists it.
        if existing and not (existing["status"] == "signal" and not source.is_signal):
            continue
        status, rule, published = classify(e, source, sources, now, first_seen, first_run, cfg)
        item = dict(url_canonical=e.url_canonical, url=e.url, source_id=source.id, title=e.title,
                    feed_text=e.text, image=e.image, extra=e.extra, feed_published_at=e.feed_published_at,
                    first_seen_at=first_seen, published_at=published, status=status, drop_rule=rule)
        (db.replace_item if existing else db.insert_item)(item)
        if status == "dropped":
            db.log_drop(now, source.id, e.url, e.title, rule)
            stats.dropped += 1
        elif status == "stale":
            stats.stale += 1
        elif status == "seen":
            stats.seen += 1
        else:
            stats.new += 1
    return stats


def record(db, source, sources, res, now, cfg):
    """Applies one FetchResult: updates the source's conditional-GET state and ingests new entries."""
    state = db.source_state(source.id)
    first_run = state["last_ok_at"] is None
    next_poll = iso(now + timedelta(seconds=source.poll_seconds))
    stats = IngestStats()
    if res.status == 304:
        db.update_source(source.id, last_polled_at=iso(now), next_poll_at=next_poll, last_status=304,
                         last_error=None, last_ok_at=iso(now), consecutive_failures=0)
    elif res.status == 200:
        sha = hashlib.sha1(res.body).hexdigest()
        entries, feed = parse_entries(res.body, source, cfg["fetch"]["max_items_per_feed"])
        error = None if entries else ("parse error" if feed.bozo else "no items")
        if entries and sha != state["body_sha"]:
            stats = ingest(db, source, sources, entries, now, first_run, cfg)
        stats.entries = len(entries)
        fields = dict(last_polled_at=iso(now), next_poll_at=next_poll, last_status=200, last_error=error,
                      etag=res.etag, last_modified=res.last_modified)
        if error:
            fields["consecutive_failures"] = state["consecutive_failures"] + 1
        else:
            fields.update(body_sha=sha, last_ok_at=iso(now), consecutive_failures=0)
        db.update_source(source.id, **fields)
        res.error = error
    else:
        db.update_source(source.id, last_polled_at=iso(now), next_poll_at=next_poll, last_status=res.status,
                         last_error=res.error, consecutive_failures=state["consecutive_failures"] + 1)
    db.log_poll(now, source.id, res.status, stats.entries, stats.new, res.ms, res.error)
    return stats

