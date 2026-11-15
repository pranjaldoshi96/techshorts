"""Polls feeds with conditional GETs and turns entries into items with honest timestamps (spec 2.3).

Network work (`fetch`) runs in a thread pool; everything that touches the database (`ingest`) runs serially
in the worker's main thread.
"""
import calendar
import hashlib
import html
import json
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
# Gift and share codes too: Hacker News linked an NYT story with ?unlocked_article_code=..., Techmeme without, and
# the story was imported twice.
_DROP_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ocid|cmpid|smid|guccounter|amp|outputtype|"
                          r"unlocked_article_code|giftcopy|gift|accesstoken|st|reflink|ref_src|share|sh|"
                          r"partner|cmp|ref)$", re.I)


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


def seed_horizon(source, cfg):
    """How old an item may be at a source's first poll and still be written."""
    if source.seed_days:
        return min(timedelta(days=float(source.seed_days)), max_age(source, cfg))
    return timedelta(hours=cfg["fetch"]["first_run_skip_hours"])


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


def _signal_extra(source, e, summary_html):
    """Points and comments from hnrss; the linked article from Techmeme (whose own link is a permalink)."""
    extra = {}
    source_id = source.id
    if source.kind == "techmeme":
        source_id = "techmeme"
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
    elif source.kind == "reddit":
        # A link post's entry carries the article behind an anchor labelled [link]; a text post links to itself.
        m = re.search(r'<a href="(https?://[^"]+)">\s*\[link\]\s*</a>', summary_html)
        if m and not re.search(r"//([\w.]+\.)?(reddit\.com|redd\.it)/", m.group(1)):
            extra["linked_url"] = canonical_url(html.unescape(m.group(1)))
    return extra


class _JsonFeed:
    """Stands in for feedparser's result when the body is JSON: `bozo` is the parse failure flag."""

    def __init__(self, bozo):
        self.bozo = bozo


def _iso_dt(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_json_entries(body, source, limit):
    """Hacker News through the Algolia search API, and Lobsters' hottest.json: both give the linked article, its
    points and its comment count directly. (hnrss.org, the RSS route, answered in 3-8 s from the VPS and timed out
    on 3 of 45 polls; the API answers in about a second.)"""
    try:
        data = json.loads(body)
    except ValueError:
        return [], _JsonFeed(True)
    out = []
    if source.format == "hn_algolia":
        for h in (data.get("hits") if isinstance(data, dict) else None) or []:
            title, oid = plain(h.get("title")), h.get("objectID")
            if not title or not oid:
                continue
            hn_url = f"https://news.ycombinator.com/item?id={oid}"
            url = (h.get("url") or hn_url).strip()
            if not url.startswith(("http://", "https://")):
                continue
            ts = h.get("created_at_i")
            out.append(Entry(url=url, url_canonical=canonical_url(url), title=title,
                             feed_published_at=datetime.fromtimestamp(ts, timezone.utc) if ts else None,
                             extra={"points": int(h.get("points") or 0), "comments": int(h.get("num_comments") or 0),
                                    "hn_url": hn_url}))
    elif source.format == "lobsters":
        for h in data if isinstance(data, list) else []:
            title = plain(h.get("title"))
            url = (h.get("url") or h.get("comments_url") or "").strip()
            if not title or not url.startswith(("http://", "https://")):
                continue
            out.append(Entry(url=url, url_canonical=canonical_url(url), title=title,
                             feed_published_at=_iso_dt(h.get("created_at")),
                             extra={"points": int(h.get("score") or 0), "comments": int(h.get("comment_count") or 0),
                                    "hn_url": h.get("comments_url")}, tags=tuple(h.get("tags") or ())))
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    out.sort(key=lambda x: x.feed_published_at or epoch, reverse=True)
    return out[:limit], _JsonFeed(False)


_TRENDING_ROW = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)


def parse_github_trending(body, limit):
    """GitHub's trending page (HTML; there is no feed): one entry per repository, "owner/repo: description", with
    the stars it gained today as points. The page has no dates, so entries are dated at first sight."""
    page = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    out = []
    for row in _TRENDING_ROW.findall(page):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([\w.-]+/[\w.-]+)"', row, re.S)
        if not m:
            continue
        repo = m.group(1)
        desc = re.search(r'<p class="col-9[^"]*"[^>]*>(.*?)</p>', row, re.S)
        stars = re.search(r"([\d,]+)\s+stars today", row)
        url = f"https://github.com/{repo}"
        out.append(Entry(url=url, url_canonical=canonical_url(url),
                         title=f"{repo}: {plain(desc.group(1))}" if desc else repo,
                         extra={"points": int(stars.group(1).replace(",", "")) if stars else 0}))
    return out[:limit], _JsonFeed(not out)


def parse_entries(body, source, limit):
    if source.format == "github_trending":
        return parse_github_trending(body, limit)
    if source.format != "rss":
        return parse_json_entries(body, source, limit)
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
            # The byline names the writer for attribution prefixes on opinion pieces (attribution.py).
            extra=_signal_extra(source, e, summary_html) if source.is_signal else (
                {"author": plain(e.get("author"))} if e.get("author") else {}),
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


def classify(entry, source, sources, now, first_seen, first_run, cfg, vetted=False):
    """Decides an entry's status at first sight. Returns (status, drop_rule, published_at).

    Age rules run before the prefilter so health.json counts only drops of items that would otherwise ship.
    `vetted`: Techmeme links to this article, so its editors already judged it tech news; the feed's category
    filter (include_tags) does not apply. Semafor's "Under pressure, Lutnick joins Trump in China" was dropped for
    its category while Techmeme headlined it."""
    published = resolve_published(entry.feed_published_at, first_seen)
    if is_stale(published, now, source, cfg):
        return "stale", None, published
    # On a source's first poll, anything older than 36 h (or undated, so of unknown age) already existed. A source
    # may reach further back (seed_days), capped at the age limit above.
    # (GitHub's trending page has no dates at all: its entries are current by definition.)
    if first_run and source.format != "github_trending" and (
            entry.feed_published_at is None or now - published > seed_horizon(source, cfg)):
        return "seen", None, published
    if sources.blocked(entry.url):
        return "dropped", "domain_blocklist", published
    if source.include_tags and not set(entry.tags) & set(source.include_tags) and not vetted:
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
        if existing and source.is_signal:
            _update_signal(db, existing, source, e)
        # A URL first seen only on a signal source becomes a real item when a news source lists it.
        if existing and not (existing["status"] == "signal" and not source.is_signal):
            continue
        status, rule, published = classify(e, source, sources, now, first_seen, first_run, cfg,
                                           vetted=bool(source.include_tags)
                                           and techmeme_linked(db, e.url_canonical))
        extra = e.extra
        if existing and not source.is_signal:
            extra = {**e.extra, **_carried(existing)}
        item = dict(url_canonical=e.url_canonical, url=e.url, source_id=source.id, title=e.title,
                    feed_text=e.text, image=e.image, extra=extra, feed_published_at=e.feed_published_at,
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


def techmeme_linked(db, url):
    """Does a Techmeme-kind signal row link to `url`? (Cheap: a signal's extra holds its linked URL as JSON text.)"""
    return db.one("SELECT 1 FROM items WHERE status = 'signal' AND extra LIKE ? LIMIT 1",
                  f'%"linked_url": {json.dumps(url)}%') is not None


# Drops that are a judgement about the topic, which an editor's pick overrules (promote_signals revives them).
REVIVABLE = ("source_tags", "triage_offtopic", "triage_dull")
POINT_KEYS = ("points", "comments", "hn_url")


def _update_signal(db, existing, source, e):
    """Keeps a signal's points current. They were frozen at the value first seen (the row was skipped as "already
    known"), so a story first polled at 30 points that reached 800 kept the multiplier of a 30-point story.
    On the signal's own row they live at the top of `extra`; on a news item the signal also listed (or an article
    imported from the signal's link), under extra.signals.<source id>, where cluster.attach_signals reads them."""
    new = {k: e.extra[k] for k in POINT_KEYS if e.extra.get(k) is not None}
    if not new:
        return
    extra = json.loads(existing["extra"] or "{}")
    own = existing["status"] == "signal" and existing["source_id"] == source.id
    cur = extra if own else extra.setdefault("signals", {}).setdefault(source.id, {})
    if all(cur.get(k) == v for k, v in new.items()):
        return
    cur.update(new)
    db.conn.execute("UPDATE items SET extra = ? WHERE url_canonical = ?",
                    (json.dumps(extra), existing["url_canonical"]))


def _carried(existing):
    """What a signal row knows that must survive a news source taking the URL over: its points."""
    extra = json.loads(existing["extra"] or "{}")
    pts = {k: extra[k] for k in POINT_KEYS if extra.get(k) is not None}
    signals = dict(extra.get("signals") or {})
    if pts:
        signals[existing["source_id"]] = pts
    return {"signals": signals} if signals else {}


# Signals that become cards

# Links that are not articles: discussion pages, social posts, video, code hosts, PDFs.
NOT_ARTICLE_HOSTS = ("news.ycombinator.com", "x.com", "twitter.com", "youtube.com", "youtu.be", "github.com",
                     "gitlab.com", "reddit.com", "lobste.rs", "bsky.app", "threads.net", "linkedin.com",
                     "facebook.com", "instagram.com", "tiktok.com", "techmeme.com", "mediagazer.com")
PROMOTE_DEFAULTS = {"techmeme": 0, "hn": 150, "lobsters": 60, "max_age_h": 36, "per_tick": 10}
_TECHMEME_CREDIT = re.compile(r"\s*\((?:[^()]|\([^()]*\))*\)\s*$")


def host_name(url):
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def promote_signals(db, srcs, cfg, now):
    """Turns strong signals into items of their own, so a story that only Techmeme or Hacker News carries can
    become a card: Techmeme and Mediagazer picks (an editor chose them), and HN or Lobsters links past a points
    bar. The linked article is fetched and written like any item; the signal still attaches to its event by URL.

    The 2026-11-15 test run held "Bessent urges Nvidia to hire an outside CEO" (Techmeme), "Lovable apps expose
    passport details and 200K private messages" (HN, 944 points) and a dozen more only as signals, with no card,
    because the outlets that broke them were not sources. The publisher is the listed source that owns the host
    (sources.yaml `domains`), else the catch-all "web" source showing the host name.
    Returns the number of items created."""
    bars = {**PROMOTE_DEFAULTS, **((cfg.get("signals") or {}).get("promote") or {})}
    by_id = srcs.by_id()
    web = by_id.get("web")
    since = iso(now - timedelta(hours=bars["max_age_h"]))
    made = 0
    rows = db.q("SELECT * FROM items WHERE status = 'signal' AND published_at >= ? ORDER BY published_at DESC", since)
    for r in rows:
        if made >= bars["per_tick"]:
            break
        source = by_id.get(r["source_id"])
        extra = json.loads(r["extra"] or "{}")
        if source is None or not source.is_signal or extra.get("promoted"):
            continue
        kind = source.kind or ("hn" if source.id.startswith("hn") else source.id)
        bar = bars.get(kind)
        if bar is None or (extra.get("points") or 0) < bar:
            continue
        target = extra.get("linked_url") if kind == "techmeme" else r["url_canonical"]
        if not target:
            continue

        def done(why):
            extra["promoted"] = why
            db.conn.execute("UPDATE items SET extra = ? WHERE url_canonical = ?",
                            (json.dumps(extra), r["url_canonical"]))

        host = host_name(target)
        if any(host == h or host.endswith("." + h) for h in NOT_ARTICLE_HOSTS) or \
                urlsplit(target).path.lower().endswith((".pdf", ".zip", ".mp4")):
            done("not_article")
            continue
        publisher = srcs.for_host(target) or web
        if publisher is None:
            done("no_publisher")
            continue
        title = r["title"] if kind != "techmeme" else (_TECHMEME_CREDIT.sub("", r["title"]).strip() or r["title"])
        rule = "domain_blocklist" if srcs.blocked(target) else prefilter.check(title, target)
        if rule:
            db.log_drop(now, r["source_id"], target, title, rule)
            done(rule)
            continue
        pts = {k: extra[k] for k in POINT_KEYS if extra.get(k) is not None}
        new_extra = {"via": r["source_id"]}
        if publisher is web:
            new_extra["publisher"] = host
        if pts:
            new_extra["signals"] = {r["source_id"]: pts}
        item = dict(url_canonical=target, url=target, source_id=publisher.id, title=title,
                    feed_text=r["feed_text"] if kind == "techmeme" else "", image=r["image"], extra=new_extra,
                    feed_published_at=from_iso(r["feed_published_at"]), first_seen_at=from_iso(r["first_seen_at"]),
                    published_at=from_iso(r["published_at"]), status="new", drop_rule=None)
        existing = db.item(target)
        if existing is None:
            db.insert_item(item)
            db.add_sighting(target, r["source_id"], r["title"], from_iso(r["feed_published_at"]), now)
            done("item")
            made += 1
        elif existing["status"] == "signal":
            # An HN or Lobsters row is itself the article's URL (this row, or another signal's row for the same
            # article): the row becomes the article and keeps every signal's points.
            item["extra"] = {**new_extra, "signals": {**_carried(existing).get("signals", {}),
                                                      **new_extra.get("signals", {})}}
            db.replace_item(item)
            if existing["url_canonical"] != r["url_canonical"]:
                db.add_sighting(target, r["source_id"], r["title"], from_iso(r["feed_published_at"]), now)
                done("item")
            made += 1
        elif existing["status"] == "dropped" and existing["drop_rule"] in REVIVABLE:
            # The feed's category filter or the screening call dropped it as off-topic or dull, and Techmeme's
            # editors then picked it (or it passed the points bar on Hacker News or Lobsters): that judgement
            # wins, and the item is written after all.
            revive(db, existing, r["source_id"])
            done("revived")
            made += 1
        else:
            done("known")       # a news source already carries the linked article
    return made + revive_shared(db, by_id, bars, since)


def revive(db, existing, via):
    keep = json.loads(existing["extra"] or "{}")
    db.conn.execute("UPDATE items SET status = 'new', drop_rule = NULL, triaged = 1, attempts = 0, retry_at = NULL,"
                    " extra = ? WHERE url_canonical = ?",
                    (json.dumps({**keep, "via": via}), existing["url_canonical"]))


def revive_shared(db, by_id, bars, since):
    """A screening drop is not final: an item a news source listed, dropped as off-topic or dull, that Hacker
    News or Lobsters then carries past the import bar is written after all. Such a link has no signal row of its
    own (the URL is the news item's; its points live in the item's extra.signals), so promote_signals' loop over
    signal rows never saw it: TechCrunch's "SNL gathers all the tech tycoons for a lesson in dating and spycraft"
    was dropped as off-topic and stayed dropped whatever followed. Returns the number revived."""
    marks = ",".join("?" * len(REVIVABLE))
    n = 0
    for r in db.q(f"SELECT * FROM items WHERE status = 'dropped' AND drop_rule IN ({marks})"
                  " AND extra LIKE '%\"signals\"%' AND published_at >= ?", *REVIVABLE, since):
        for sid, pts in (json.loads(r["extra"] or "{}").get("signals") or {}).items():
            source = by_id.get(sid)
            kind = (source.kind or ("hn" if sid.startswith("hn") else sid)) if source else None
            bar = bars.get(kind) if kind in ("hn", "lobsters") else None
            if bar is not None and (pts.get("points") or 0) >= bar:
                revive(db, r, sid)
                n += 1
                break
    return n


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

