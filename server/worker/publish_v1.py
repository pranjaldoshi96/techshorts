"""The v1 edition.json that the live 1.2 app reads (spec 2.13), written on every publish until 1.2 is < 5% of sessions.

One article per event, in the exact shape Edition.swift and Article.swift decode:
  - category is the closed enum Top|AI|Startups|Dev|Blogs. An event in several tabs is filed under the non-Top tab
    where it ranks best (by rank / tab length); AI -> AI, Startups -> Startups, Dev -> Dev, Reads -> Blogs, and
    India -> Startups for funding/acquisition, else Top. Top is shown by 1.2 as "every article with a topRank",
    so a Top-filed article must have one: an India-only event that isn't in Top is left out, since 1.2 would
    never show it anyway.
  - topRank is the 0-based Top rank (1.2's own convention), title/headline the event headline, excerpt/content/
    summary the full summary, source the lead's v1_name, alsoCoveredBy the other publishers' v1 names,
    image the 720 px std JPEG.
  - spice (1-10) and notification {title, body} ride along on every written card for the next app release's push
    alerts: the title is at most 40 characters, the body a hook of at most 110, both validated and fact-checked
    with the card. notifications.json next to the edition lists the cards ranked for pushing (spice x importance
    x traction).
  - Degraded cards (no validated AI text) carry the publisher title and a <= 55-word excerpt, no headline or
    summary, and aiText: false. 1.2 ignores unknown keys, so aiText, eventId and the edition's degraded flag
    ride along for health checks and R2.
The file is written to a temp name, checked by tools/check_edition.py (the 1.2 decoder itself), and only then
renamed into place; a failed check keeps the previous edition and alerts.
"""
import hashlib
import itertools
import json
import logging
import os
import re
import statistics
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote

from . import (attribution as attribution_mod, degraded as degraded_mod, editorial as editorial_mod, embed as embed_mod, images as images_mod,
               importance as importance_mod, rank, sources as sources_mod, validate)
from .cluster import entity_keys
from .db import from_iso, iso
from .llm import LLMError

log = logging.getLogger("techgist")

V1_CATEGORY = {"ai": "AI", "startups": "Startups", "dev": "Dev", "reads": "Blogs"}
# The tab an event's main topic (its first) belongs in, preferred over the relative-rank rule in v1_category.
# A security incident that made Dev (rank.dev_security: it has a developer angle) is filed there, not under
# whichever other tab it also reached.
PRIMARY_TAB = {"ai": "ai", "startups_funding": "startups", "dev_tools": "dev", "security": "dev"}
V1_CATEGORIES = ("Top", "AI", "Startups", "Dev", "Blogs")
URL_SAFE = ":/?#[]@!$&'()*+,;=%~-._"
MAX_WINDOW = timedelta(days=14)
OUTAGE_SINGLETON_WINDOW = timedelta(hours=36)
COLUMN_IMPORTANCE = 6


def clean_url(u):
    """Percent-encodes what older iOS URL(string:) refuses (spaces, non-ASCII); one bad URL fails the whole decode."""
    return quote((u or "").strip(), safe=URL_SAFE) if u else u


def _words(s):
    return set(w for w in re.findall(r"[a-z0-9$]+", (s or "").lower()) if len(w) > 2)


# Loading events

MEMBER_SQL = """
SELECT i.url_canonical, i.url, i.source_id, i.title, i.feed_text, i.image, i.og_image, i.published_at,
       i.first_seen_at, i.feed_published_at, i.extra, w.status, w.text, w.entities, w.type
FROM event_members em JOIN items i ON i.url_canonical = em.url_canonical
LEFT JOIN writes w ON w.url_canonical = em.url_canonical
WHERE em.event_id = ?
"""


@dataclass
class Ctx:
    db: object
    cfg: dict
    srcs: object
    now: object
    outage: str = None
    editorial: object = None

    def __post_init__(self):
        self.by_id = self.srcs.by_id()
        self.editorial = self.editorial or editorial_mod.Editorial()

    def src(self, sid):
        return self.by_id.get(sid)

    def v1_name(self, sid):
        s = self.src(sid)
        return s.v1_name if s else sid

    def pub(self, m):
        """The publisher name a member shows: its source's, or the host of an article imported from a signal's
        link under the catch-all source (items.extra.publisher)."""
        try:
            name = json.loads(m.get("extra") or "{}").get("publisher")
        except (TypeError, ValueError):
            name = None
        return name or self.v1_name(m["source_id"])

    def authority(self, sid):
        s = self.src(sid)
        return float(s.authority) if s else 0.8

    def tier1(self, sid):
        s = self.src(sid)
        return s is not None and sources_mod.effective_tier(s, self.db.check_ok(s.id)) == 1


def image_sources(ctx, lead, members):
    """Candidate picture URLs in the order of spec section 3: the lead's og:image then feed media, then the other
    members' by authority. Sources set to `images: generated` contribute none."""
    out = []
    others = sorted((m for m in members if m["url_canonical"] != lead["url_canonical"]),
                    key=lambda m: (-ctx.authority(m["source_id"]), m["published_at"]))
    for m in [lead] + others:
        s = ctx.src(m["source_id"])
        if s is not None and s.images == "generated":
            continue
        for u in (m["og_image"], m["image"]):
            if u and u.startswith("http") and u not in out:
                out.append(u)
    return out


def event_candidate(ctx, ev):
    members = [dict(m) for m in ctx.db.q(MEMBER_SQL, ev["id"])]
    lead = next((m for m in members if m["url_canonical"] == ev["lead_url"]), None)
    if lead is None:
        return None, "no_lead"
    lead_src = ctx.src(lead["source_id"])
    if ctx.editorial.blocked(lead_src, lead["url"]) or ev["id"] in ctx.editorial.kill:
        return None, "editorial"
    news = [m for m in members if not (ctx.src(m["source_id"]) and ctx.src(m["source_id"]).is_signal)]
    if any(ctx.editorial.blocked(ctx.src(m["source_id"]), m["url"]) for m in news) and len(news) > 1:
        news = [m for m in news if not ctx.editorial.blocked(ctx.src(m["source_id"]), m["url"])]
    publishers = []
    for m in sorted(news, key=lambda m: (-ctx.authority(m["source_id"]), m["published_at"])):
        name = ctx.pub(m)
        if name not in publishers:
            publishers.append(name)
    hn = json.loads(ev["hn"] or "null") or {}
    social = json.loads(_col(ev, "social") or "null") or {}
    over = ctx.editorial.override.get(ev["id"]) or {}
    headline = over.get("headline") or ev["headline"]
    summary = over.get("summary") or ev["summary"]
    ai_text = bool(headline and summary)
    lead_tier1 = ctx.tier1(lead["source_id"])
    if not ai_text and (not (len(publishers) >= 2 or lead_tier1) or lead["status"] == "thin"):
        # Held; publishes once its text validates or coverage grows. An event led by a thin item (a paywalled
        # feed's title and sentence; cluster.thin_can_seed) never ships as a raw title: it waits for its brief.
        return None, "no_text"
    if self_promo(ctx.pub(lead), lead["title"]):
        return None, "self_promo"
    importance = ev["importance"] or 1
    ev_type, headline = column_type(ev["type"], lead["url"], headline)
    if ev_type != ev["type"]:
        # Scored as news, it was scored for a fact it doesn't report: a column on a known story adds a view, which
        # the prompt rates 4-6 ("An overview of Jensen Huang's messy days" came back 9 and led Top).
        importance = min(importance, COLUMN_IMPORTANCE)
    c = rank.Candidate(
        id=ev["id"], importance=importance, type=ev_type, is_news=bool(ev["is_news"]),
        relevance=ev["relevance"] or 0, topics=json.loads(ev["topics"] or "[]"), region=ev["region"] or "global",
        n_publishers=max(1, len(publishers)), on_techmeme=bool(ev["on_techmeme"]), hn_points=hn.get("points") or 0,
        authority=max([ctx.authority(m["source_id"]) for m in news] or [0.8]),
        published_at=from_iso(ev["published_at"]), material_at=from_iso(ev["last_material_update_at"]),
        lead_source=lead["source_id"], lead_publisher=ctx.pub(lead),
        lead_reads=bool(lead_src and lead_src.reads), lead_tier1=lead_tier1, ai_text=ai_text,
        lead_dev=bool(lead_src and lead_src.desk == "dev"),
        classified=ev["type"] is not None, boost=ctx.editorial.boost.get(ev["id"], 1.0),
        pin=frozenset(ctx.editorial.pins.get(ev["id"], ())), title=headline or lead["title"] or "",
        spice=_col(ev, "spice") or rank.SPICE_NEUTRAL,
        lobsters_points=social.get("lobsters") or 0, on_reddit=reddit_top(social.get("reddit")),
        unvetted=lead["source_id"] == sources_mod.WEB_ID,
        keys=frozenset().union(*(entity_keys(json.loads(m["entities"] or "{}")) for m in members)))
    c.data = {"lead": lead, "members": members, "publishers": publishers, "headline": headline, "summary": summary,
              "hn": hn, "parent_id": ev["parent_id"], "title_words": _words(lead["title"]),
              "first_seen_at": min(m["first_seen_at"] for m in members),
              "first_report_at": first_report(ctx.db, ev["id"], members),
              "images": image_sources(ctx, lead, members), "orgs": primary_orgs(members),
              # An owner's override replaces the card's text, so the alert written for the old text is dropped.
              "people": people(members),
              # No publisher's date at all (none in the feed, none on the page): the lead is dated by when the
              # worker first fetched it, which says nothing about when the news happened.
              "undated": lead["feed_published_at"] is None and lead["published_at"] == lead["first_seen_at"],
              "notif_title": None if over else _col(ev, "notif_title"),
              "notif_line": None if over else ev["notif_line"]}
    return c, None


def first_report(db, eid, members):
    """The earliest report of the story the engine saw (ISO): the earliest publisher time among the event's
    members, whatever their type or length, and the signal posts attached to it (a Techmeme headline is often
    the first sight of a story)."""
    times = [m["published_at"] for m in members]
    row = db.one("SELECT MIN(i.published_at) at FROM event_signals s JOIN items i ON i.url_canonical = s.url_canonical"
                 " WHERE s.event_id = ?", eid)
    if row is not None and row["at"]:
        times.append(row["at"])
    return min(times)


def reddit_top(points):
    """Is the event's Reddit post among the day's first rank.REDDIT_TOP? `points` is its place in the listing
    counted down from the top (fetch.REDDIT_LISTING for the first post)."""
    from .fetch import REDDIT_LISTING
    return (points or 0) > REDDIT_LISTING - rank.REDDIT_TOP


def _col(row, name):
    """A column that an older state.db row or a test's hand-built row may not have."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def column_type(ev_type, url, headline):
    """A column or opinion URL is commentary whatever an earlier write said: (type, headline) with the type made
    analysis and the headline labelled. Deterministic, so it holds for events written before the rule existed and
    for leads whose write failed, not only for new validated writes."""
    if ev_type in importance_mod.HARD_NEWS and attribution_mod.is_column(url):
        ev_type = "analysis"
        if headline and not ANALYSIS_LABEL.match(headline):
            headline = "Analysis: " + headline
    return ev_type, headline


ANALYSIS_LABEL = re.compile(r"^(Analysis|Opinion|Interview|[A-Z][\w'’-]+):\s")


# What a publisher announces about itself: a programme, event, speaker line-up, quiz, award or weekly round-up.
PROMO_WORDS = re.compile(r"\b(launch(es|ed)?|announce(s|d|ment)?|speakers?|edition|quiz|summit|conference|awards?|"
                         r"webinar|this week|top \d+|accelerator|programme|program|cohort|investment pool|"
                         r"applications? open|register|nominations?)\b", re.I)


def self_promo(publisher, title):
    """A publisher writing about its own programme or event ("Inc42, DevX Launch RE:FRAME With ₹15 Cr Investment
    Pool", "MediaNama This Week: Top 10 Stories", YourStory's quiz "Edition 258"): its name and a promo word in its
    own title. Only the source's title counts: a company blog naming itself in a product post ("Pay for Hugging Face
    Hub with your Google Cloud Account") is not caught, nor is a headline the model wrote."""
    if not publisher or not title:
        return False
    pat = re.compile(r"(?<![\w])" + re.escape(publisher) + r"(?![\w])", re.I)
    return bool(pat.search(title)) and bool(PROMO_WORDS.search(title))


def people(members):
    """Every person the members name, lower-cased (an alert title must be more than one of these)."""
    out = set()
    for m in members:
        for name in (json.loads(m.get("entities") or "{}") or {}).get("people") or []:
            if name.strip():
                out.add(" ".join(name.lower().split()))
    return out


def primary_orgs(members):
    """The first-named org of each validated member: what the story is about ("Anthropic" for the IPO pieces)."""
    out = set()
    for m in members:
        orgs = (json.loads(m.get("entities") or "{}") or {}).get("orgs") or []
        if orgs and orgs[0].strip():
            out.add(re.sub(r"[^\w]+", " ", orgs[0].lower()).strip())
    return out


def outage_singletons(ctx, events):
    """LLM outage only: tier-1 items first seen after the outage began that never made it into an event (the LLM
    and embeddings share the key, so nothing new clusters) publish alone with their publisher title, so the feed
    keeps moving. An item the LLM already judged keeps that judgement: a held NYT paywall piece rated relevance 1
    is no more shippable in an outage. And a singleton that repeats a story some event already carries is dropped
    (`same_story`), since without a headline vector the edition-wide duplicate guard can't see it."""
    since = degraded_mod.outage_since(ctx.db, ctx.outage, ctx.now)
    floor = ctx.now - OUTAGE_SINGLETON_WINDOW
    rows = ctx.db.q(
        "SELECT i.*, w.status wstatus, w.type, w.is_news, w.relevance, w.importance, w.topics, w.region, w.headline,"
        " w.summary, w.text, e.vec FROM items i LEFT JOIN writes w ON w.url_canonical = i.url_canonical"
        " LEFT JOIN embeddings e ON e.url_canonical = i.url_canonical AND e.kind = 'passage'"
        " WHERE i.status IN ('new', 'held', 'written') AND i.published_at >= ? AND i.first_seen_at >= ?"
        " AND i.url_canonical NOT IN (SELECT url_canonical FROM event_members)"
        " ORDER BY i.published_at DESC",
        iso(floor), iso(max(floor, since) if since else floor))
    taken = {c.data["lead"]["url_canonical"] for c in events}
    seen = StorySet(ctx, events)
    t_join = ctx.cfg["cluster"]["t_join"]
    out = []
    from .cluster import event_id
    for r in rows:
        s = ctx.src(r["source_id"])
        if s is None or s.is_signal or not ctx.tier1(s.id) or r["url_canonical"] in taken:
            continue
        if ctx.editorial.blocked(s, r["url"]):
            continue
        r = dict(r)
        vec = embed_mod.from_blob(r["vec"]) if r["vec"] is not None else None
        if seen.same_story(vec, r["title"], t_join):
            continue
        seen.add(vec, r["title"])
        classified = r["type"] is not None
        text_ok = r["wstatus"] == "ok"
        topics = json.loads(r["topics"] or "[]") if classified else []
        imp = r["importance"] or 1
        if classified:
            imp = importance_mod.compute([importance_mod.Member(r["url_canonical"], s.id, s.v1_name, True, r["type"],
                                                                imp, topics, [])], r["type"], topics).value
        c = rank.Candidate(
            id=event_id(r["url_canonical"]), importance=imp, type=r["type"],
            is_news=bool(r["is_news"]) if classified else True, relevance=r["relevance"] or 0, topics=topics,
            region=r["region"] or s.region, n_publishers=1, on_techmeme=False, hn_points=0,
            authority=float(s.authority), published_at=from_iso(r["published_at"]), lead_source=s.id,
            lead_publisher=s.v1_name, lead_reads=s.reads, lead_tier1=True,
            ai_text=bool(text_ok and r["headline"] and r["summary"]), classified=classified)
        lead = {k: r[k] for k in ("url_canonical", "url", "source_id", "title", "feed_text", "image", "og_image",
                                  "published_at", "first_seen_at", "text")}
        c.data = {"lead": lead, "members": [lead], "publishers": [s.v1_name],
                  "headline": r["headline"] if c.ai_text else None, "summary": r["summary"] if c.ai_text else None,
                  "hn": {}, "title_words": _words(r["title"]), "first_seen_at": r["first_seen_at"],
                  "images": image_sources(ctx, lead, [lead]), "parent_id": None, "singleton": True,
                  "orgs": set()}
        c.title = c.data["headline"] or r["title"] or ""
        out.append(c)
    return out


# Words that say nothing about which story a title is about.
STOP = set("""the and for with from that this will are was were has have had its into over after about than
new says say said how why what who when where more most can could would should not but you your our their they
them his her him she one two all out off now just also amid back big inc""".split())


def _keywords(title):
    return {w for w in _words(title) if w not in STOP}


class StorySet:
    """The stories an outage edition already carries, as members' stored passage vectors and title keywords.
    A singleton with a vector is the same story at cosine >= T_join (the clusterer's own rule). Without one,
    two shared keywords with any member title counts: looser than the 60% title guard on purpose, because a
    wrongly dropped singleton costs one card in an outage while a duplicate is visible to everyone ("Amazon and
    Google Will See Big Paydays from Anthropic IPO" next to the IPO event shares only "anthropic" and "ipo")."""

    def __init__(self, ctx, events):
        self.vecs, self.keys = [], []
        urls = [m["url_canonical"] for c in events for m in c.data["members"]]
        for i in range(0, len(urls), 500):
            chunk = urls[i:i + 500]
            for row in ctx.db.q("SELECT vec FROM embeddings WHERE kind = 'passage' AND url_canonical IN (%s)"
                                % ",".join("?" * len(chunk)), *chunk):
                self.vecs.append(embed_mod.from_blob(row["vec"]))
        for c in events:
            for m in c.data["members"]:
                self.keys.append(_keywords(m["title"]))

    def add(self, vec, title):
        if vec is not None:
            self.vecs.append(vec)
        self.keys.append(_keywords(title))

    def same_story(self, vec, title, t_join):
        if vec is not None:
            return any(float(vec @ v) >= t_join for v in self.vecs)
        k = _keywords(title)
        return any(len(k & o) >= 2 for o in self.keys)


def load_candidates(ctx):
    held = {}
    out = []
    for ev in ctx.db.q("SELECT * FROM events WHERE status = 'live' AND published_at >= ?", iso(ctx.now - MAX_WINDOW)):
        c, why = event_candidate(ctx, ev)
        if c is None:
            held[why] = held.get(why, 0) + 1
        else:
            out.append(c)
    out, dropped = drop_superseded(ctx, out)
    if dropped:
        held["superseded"] = dropped
    if ctx.outage:
        out += outage_singletons(ctx, out)
    return out, held


def drop_superseded(ctx, cands):
    """Leaves out a card that a later development of its story has overtaken (cluster.supersede: the judge's
    "supersedes" ruling, or a reversal such as "launches" -> "fails, payload lost"), as long as the newer event
    has a written card in this edition. "ULA launches Vulcan with 40 Amazon internet satellites" sat at rank 21
    beside the card reporting the rocket lost. Returns (kept candidates, number dropped)."""
    rows = {r["id"]: r["superseded_by"] for r in ctx.db.q(
        "SELECT id, superseded_by FROM events WHERE superseded_by IS NOT NULL")}
    if not rows:
        return cands, 0
    written = {c.id for c in cands if c.ai_text}

    def overtaken(eid, seen=()):
        """Some later event in the chain eid -> superseded_by -> ... has a written card that is itself kept."""
        if eid not in rows or eid in seen:
            return False
        nxt = _resolve(ctx.db, rows[eid])
        later = overtaken(nxt, (*seen, eid))
        return later or nxt in written
    kept = [c for c in cands if not overtaken(c.id) or c.pin]
    return kept, len(cands) - len(kept)


# Headline vectors for the duplicate guard

def headline_vectors(db, embedder, cands, now, limit=60):
    """Query-type embeddings of the headline each card will show, cached by text. On an embedding failure the
    guard falls back to title-word overlap for this publish."""
    want = {}
    for c in cands:
        text = c.data["headline"] or c.data["lead"]["title"]
        key = "headline:" + hashlib.sha1(text.encode()).hexdigest()[:16]
        row = db.one("SELECT vec FROM embeddings WHERE url_canonical = ?", key)
        if row is not None:
            c.headline_vec = embed_mod.from_blob(row["vec"])
        else:
            want.setdefault(key, (text, []))[1].append(c)
    todo = list(want.items())[:limit]
    if not todo or embedder is None:
        return 0
    try:
        vecs = embedder.embed([t for _, (t, _) in todo], "query")
    except LLMError as e:
        log.warning("headline embedding failed: %s", e)
        return 0
    for (key, (_, cs)), v in zip(todo, vecs):
        if v is None:       # the endpoint rejected this headline; the title-word guard covers the card
            continue
        db.conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?,?)",
                        (key, "headline", embedder.model, len(v), None, embed_mod.to_blob(v), iso(now)))
        for c in cs:
            c.headline_vec = v
    return len(todo)


def suppress_duplicates(cands, dedup, ruled_apart=None):
    """One card per event across the whole edition: when two events' headlines are one story by the calibrated
    headline rule (rank.Dedup; a clustering miss, e.g. a single-source ET Tech report of the Ring drone launch that
    four other outlets ran as one event), only the better-covered one stays, in every tab. Without this the weaker
    copy can reach a tab the real event doesn't qualify for. A follow-up and its parent are both kept: the
    follow-up is the new fact (spec 2.9). The pairs are recorded as twins on publish, and the next clustering pass
    merges each twin into its primary (cluster.merge_twins), so a twin never ships later as a "new" card.

    The guard used to compare headline query vectors at T_join (0.47), a threshold calibrated for passage vectors
    plus a shared entity; short same-domain headlines clear it easily ("Workday outage locks millions out" vs
    "Microsoft Outlook hit by new outage" = 0.54), so distinct stories vanished."""
    if not isinstance(dedup, rank.Dedup):
        dedup = rank.Dedup(dedup)
    # `ruled_apart(a, b)`: the clustering judge has ruled the two events different developments (cluster.
    # merge_twins asks it before merging a pair this guard reports); such a pair is two cards.
    ruled_apart = ruled_apart or (lambda a, b: False)
    order = sorted(cands, key=lambda c: (-c.n_publishers, -c.importance, c.published_at, c.id))
    kept, dropped = [], []
    for c in order:
        twin = next((k for k in kept if dedup.duplicate(c, k)
                     and c.data.get("parent_id") != k.id and k.data.get("parent_id") != c.id
                     and not ruled_apart(c.id, k.id)), None)
        if twin is None:
            kept.append(c)
        else:
            dropped.append((c.id, twin.id))
    keep = {c.id for c in kept}
    return [c for c in cands if c.id in keep], dropped


def storylines(cands, dedup, threshold, org_threshold=None):
    """Groups cards into storylines for rank.STORYLINE_CAP: a follow-up with its parent, cards whose headlines
    are close (cosine >= `threshold`) and share a non-org entity (the Huang resignation, the investors' letter and
    the supply-chain worries), and cards whose headlines are close (cosine >= `org_threshold`) and are about the
    same company (the first-named org: three Anthropic IPO cards named no person in common and all sat in Top).
    Sets each candidate's `storyline` to its group's id."""
    org_threshold = threshold if org_threshold is None else org_threshold
    root = {c.id: c.id for c in cands}

    def find(x):
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            root[max(ra, rb)] = min(ra, rb)
    for c in cands:
        p = c.data.get("parent_id")
        if p in root:
            union(c.id, p)
    for a, b in itertools.combinations(cands, 2):
        cos = dedup.cosine(a, b)
        if cos is None:
            continue
        if (cos >= threshold and a.keys & b.keys) or (
                cos >= org_threshold and a.data.get("orgs", set()) & b.data.get("orgs", set())):
            union(a.id, b.id)
    for c in cands:
        c.storyline = find(c.id)
    return cands


def title_duplicates(tabs):
    """Fallback guard for cards with no headline vector (embeddings down): >= 60% shared title words."""
    for tid, ranked in tabs.items():
        kept = []
        for c, s in ranked:
            w = c.data["title_words"]
            if c.headline_vec is None and any(
                    w and o.data["title_words"] and len(w & o.data["title_words"]) / len(w | o.data["title_words"])
                    >= 0.6 for o, _ in kept):
                continue
            kept.append((c, s))
        tabs[tid] = kept
    return tabs


# v1 mapping

def v1_category(c, placements, in_top):
    """`placements` is [(tab id, rank, tab length)] for the non-Top tabs the event made. The tab of the event's
    main topic wins when the event made it (a funding round under Startups, not under Dev because Dev was a long
    tab with weak competition); otherwise the tab where it ranks best relative to the tab's length."""
    main = PRIMARY_TAB.get((c.topics or [None])[0])
    if c.lead_dev:
        main = "dev"        # a developer outlet's story that made Dev is filed there, whatever topic came first
    if c.type in ("funding", "acquisition") and "startups_funding" in (c.topics or []):
        main = "startups"
    if main and any(tid == main for tid, _, _ in placements):
        return V1_CATEGORY[main]
    options = []
    for tid, r, n in placements:
        if tid == "india":
            cat = "Startups" if c.type in ("funding", "acquisition") else ("Top" if in_top else None)
        else:
            cat = V1_CATEGORY[tid]
        if cat:
            options.append(((r + 1) / n, rank.TAB_ORDER.index(tid), cat))
    if options:
        return min(options)[2]
    return "Top" if in_top else None


def article(c, category, top_rank, image_url):
    lead = c.data["lead"]
    if c.ai_text:
        title, body = c.data["headline"], c.data["summary"]
    else:
        title = lead["title"]
        body = degraded_mod.excerpt(lead.get("feed_text") or lead.get("text") or "")
    # 1.2 sorts by publishedAt: a story with a material update (the IPO that was "upcoming" has listed) carries the
    # update's time, as its rewritten card reports the update.
    when = max(c.published_at, c.material_at) if c.material_at else c.published_at
    a = {"url": clean_url(lead["url_canonical"]), "title": title, "excerpt": body, "content": body,
         "source": c.lead_publisher, "category": category, "publishedAt": iso(when)}
    if image_url:
        a["image"] = clean_url(image_url)
    if c.hn_points:
        a["points"] = int(c.hn_points)
    also = [p for p in c.data["publishers"] if p != c.lead_publisher]
    if also:
        a["alsoCoveredBy"] = also
    if top_rank is not None:
        a["topRank"] = top_rank
    if c.ai_text:
        a["headline"], a["summary"] = c.data["headline"], c.data["summary"]
    a["aiText"] = c.ai_text
    a["eventId"] = c.id
    if c.classified:
        a["spice"] = int(c.spice)
    note = notification(c)
    if note:
        a["notification"] = note
    return a


NOTIF_TITLE_MAX, NOTIF_BODY_MAX = 40, 110


def bare_name(title, c):
    """True when an alert title says nothing but who: a person's or an outlet's name, or a single word. One
    card's alert was titled just "Gary Marcus" (the publisher's name, used when the writer gave no title)."""
    t = " ".join(re.sub(r"[^\w\s'’&.-]", " ", (title or "").lower()).split()).strip(" .")
    if len(t.split()) < 2:
        return True
    names = {n.lower() for n in (c.lead_publisher, c.lead_source) if n} | set(c.data.get("people") or ())
    names |= {o for o in c.data.get("orgs") or ()}
    return t in names or any(t == n.split()[-1] for n in names if n)


def alert_title(c):
    """The alert's title: the writer's, when it fits and says more than a name; else the card's headline when that
    fits, else the headline cut at a clause boundary (a colon, comma or dash) that fits. Every fallback is text
    that passed the card's checks. None when nothing fits: the card then has no alert rather than an empty one."""
    title = (c.data.get("notif_title") or "").strip()
    if title and len(title) <= NOTIF_TITLE_MAX and not bare_name(title, c):
        return title
    head = (c.data.get("headline") or "").strip()
    if head and len(head) <= NOTIF_TITLE_MAX and not bare_name(head, c):
        return head
    # A cut headline must stay a true statement: only its opening clause, never a cut mid-claim, and never a
    # clause that leans on what was dropped ("Report: X" keeps its label; "Marcus: ..." is never cut down to the
    # claim without its author).
    for m in re.finditer(r"(?<!\bReport)(?<!\bSources)[:,;–—]\s|\s[-–—]\s", head):
        part, tail = head[:m.start()].strip(), head[m.start():]
        # The dropped tail must not be what made the clause true: its attribution, hedge or condition
        # ("Nvidia sold chips to China, lawsuit claims" must never become "Nvidia sold chips to China").
        if validate.HEDGE_KEPT.search(tail) or validate.SOURCING_KEPT.search(tail) or _QUALIFIER.search(tail):
            continue
        if 12 <= len(part) <= NOTIF_TITLE_MAX and len(part.split()) >= 3 and not bare_name(part, c):
            return part
    return None


_QUALIFIER = re.compile(r"\b(says?|said|claims?|alleg\w+|accus\w+|suspect\w*|if|unless|not|no|denies|denied)\b", re.I)


def notification(c):
    """{title, body} for a push alert, or None. Only for a card with validated text: the hook went through the
    same grounding, tense and fact checks as the headline. Without a usable title (see alert_title) there is no
    alert."""
    body = (c.data.get("notif_line") or "").strip()
    if not c.ai_text or not body or len(body) > NOTIF_BODY_MAX:
        return None
    title = alert_title(c)
    if not title:
        return None
    return {"title": title, "body": body}


NOTIFY_MAX_AGE_H = 12
NOTIFY_LIST = 20


def notification_candidates(tabs, now):
    """The edition's cards ranked for push alerts: spice x importance x traction with recency
    (rank.notification_score), fresh cards that are worth a tap only (rank.notifiable: spice 7+, no minor
    commentary, no unvetted single-source site). The next app release decides how many to send; this is the order
    to send them in."""
    seen, out = set(), []
    for ranked in tabs.values():
        for c, _ in ranked:
            note = notification(c)
            if c.id in seen or note is None or c.age_eff(now) > NOTIFY_MAX_AGE_H or not rank.notifiable(c):
                continue
            if c.data.get("undated"):
                continue        # an alert says "this just happened"; a fetch time cannot vouch for that
            seen.add(c.id)
            out.append({"eventId": c.id, "score": round(rank.notification_score(c, now), 4), "spice": int(c.spice),
                        "importance": int(c.importance), "publishers": int(c.n_publishers),
                        "onTechmeme": bool(c.on_techmeme), "hnPoints": int(c.hn_points or 0),
                        "onReddit": bool(c.on_reddit), "type": c.type,
                        "ageH": round(c.age_eff(now), 1), "title": note["title"], "body": note["body"],
                        "headline": c.data.get("headline"), "url": clean_url(c.data["lead"]["url_canonical"])})
    out.sort(key=lambda n: (-n["score"], n["eventId"]))
    return out[:NOTIFY_LIST]


def build(ctx, embedder=None, out_dir=None, image_store=None):
    """Candidates → headline vectors → images → tabs → v1 articles. Returns (edition dict, report dict)."""
    now = ctx.now
    cands, held = load_candidates(ctx)
    degraded = bool(ctx.outage)
    # Only events some tab could take need vectors and pictures; best first, so per-tick caps go to likely cards.
    tab_specs = rank.tabs_for(ctx.cfg)
    live = [c for c in cands
            if any(rank.eligible(c, tab_specs[t], now, degraded) is None or t in c.pin for t in rank.TAB_ORDER)]
    live.sort(key=lambda c: -rank.cand_score(c, tab_specs["top"], now, degraded))
    headline_vectors(ctx.db, embedder, live, now)
    dedup = rank.Dedup.from_cfg(ctx.cfg)
    apart = {(r["a"][5:], r["b"]) for r in ctx.db.q(
        "SELECT a, b FROM judgements WHERE a LIKE 'pair:%' AND verdict != 'same'")}
    ruled_apart = lambda a, b: tuple(sorted((a, b))) in apart  # noqa: E731
    live, twins = suppress_duplicates(live, dedup, ruled_apart)
    dcfg = ctx.cfg.get("dedup") or {}
    t_story = dcfg.get("t_storyline", ctx.cfg["cluster"]["t_join"])
    storylines(live, dedup, t_story, dcfg.get("t_storyline_org", t_story))
    pictures = {}
    if image_store is not None:
        need_page = [c.data["lead"] for c in live[:120] if c.data["lead"]["og_image"] is None]
        if need_page:
            image_store.page_images(need_page[:40])
            for c in live:
                lead = c.data["lead"]
                if lead["og_image"] is None:
                    row = ctx.db.one("SELECT og_image FROM items WHERE url_canonical = ?", lead["url_canonical"])
                    lead["og_image"] = row["og_image"] if row else None
                    c.data["images"] = image_sources(ctx, lead, c.data["members"] if len(c.data["members"]) > 1
                                                     else [lead])
        pictures = image_store.resolve({c.id: c.data["images"] for c in live})
        for c in live:
            row = pictures.get(c.id)
            c.image_hash = row["dhash"] if row else None
    stats = {}
    tabs = rank.build_tabs(live, now, degraded, dedup, stats, tab_specs, ruled_apart)
    tabs = title_duplicates(tabs)

    base = (ctx.cfg.get("publish") or {}).get("base_url", "").rstrip("/") + "/"
    top_rank = {c.id: i for i, (c, _) in enumerate(tabs["top"])}
    placements = {}
    for tid in rank.TAB_ORDER[1:]:
        for i, (c, _) in enumerate(tabs[tid]):
            placements.setdefault(c.id, []).append((tid, i, len(tabs[tid])))
    by_id = {c.id: c for ranked in tabs.values() for c, _ in ranked}
    scores = {c.id: s for c, s in tabs["top"]}
    articles, used_images, left_out = [], set(), 0
    gen_taken = {}          # topic -> {variant: cards showing it}, so no two cards share a generated picture
    order = [c.id for c, _ in tabs["top"]] + [eid for eid in by_id if eid not in top_rank]
    for eid in order:
        c = by_id[eid]
        cat = v1_category(c, placements.get(eid, []), eid in top_rank)
        if cat is None:
            left_out += 1
            continue
        image_url = None
        if out_dir is not None:
            row = pictures.get(eid)
            if row:
                name = row["sha8"]
            else:
                # In rank order (Top first, then each tab), each card takes a look no earlier card of its topic
                # has: with 24 looks a topic, the first screen of any tab never shows one twice.
                topic = (c.topics or ["default"])[0]
                taken = gen_taken.setdefault(topic if topic in images_mod.TOPIC_COLORS else "default", {})
                k = images_mod.pick_variant(eid, taken)
                taken[k] = taken.get(k, 0) + 1
                name = images_mod.generated(out_dir, topic, k)
            used_images.add(name)
            image_url = f"{base}img/{name}_std.jpg"
        articles.append(article(c, cat, top_rank.get(eid), image_url))
    edition = {"publishedAt": iso(now), "articles": articles, "degraded": ctx.outage or False}
    spices = [c.spice for c in by_id.values() if c.classified]
    report = {
        "notifications": notification_candidates(tabs, now),
        "spice": {"cards": len(spices), "median": statistics.median(spices) if spices else None,
                  "atLeast7": sum(v >= rank.SPICY for v in spices),
                  "histogram": {str(i): sum(v == i for v in spices) for i in range(1, 11)},
                  "topMedian": statistics.median([c.spice for c, _ in tabs["top"]]) if tabs["top"] else None},
        "degraded": ctx.outage, "candidates": len(cands), "held": held, "leftOut": left_out,
        "duplicatesSuppressed": len(twins), "twins": twins,
        "degradedCards": sum(not a["aiText"] for a in articles), "articles": len(articles),
        "tabs": {tid: _tab_report(ranked, now) for tid, ranked in tabs.items()},
        "filtered": stats, "topScores": [round(scores[c.id], 4) for c, _ in tabs["top"][:5]],
        "images": {"used": len(used_images), "generated": sum(n.startswith("gen_") for n in used_images)},
        # A 1.2 tab that would show nothing (e.g. Blogs while Reads' min_importance sits above the LLM's essay
        # ratings): the tick alerts on it, since 1.2 users see an empty tab with no explanation.
        "emptyCategories": [cat for cat in V1_CATEGORIES if not any(a["category"] == cat for a in articles)],
    }
    return edition, report, used_images, tabs


def _tab_report(ranked, now):
    ages = [c.age_h(now) for c, _ in ranked]
    pubs = {}
    for c, _ in ranked:
        pubs[c.lead_publisher] = pubs.get(c.lead_publisher, 0) + 1
    return {"count": len(ranked), "medianAgeH": round(statistics.median(ages), 1) if ages else None,
            "maxPerPublisher": max(pubs.values()) if pubs else 0, "degraded": sum(not c.ai_text for c, _ in ranked)}


def content_sha(edition):
    return hashlib.sha256(json.dumps(edition["articles"], sort_keys=True).encode()).hexdigest()[:16]


def write(ctx, edition, report, used_images, out_dir, check=True, tabs=None):
    """Checks and atomically installs edition.json. Returns (ok, problem)."""
    from tools import check_edition
    os.makedirs(out_dir, exist_ok=True)
    final = os.path.join(out_dir, "edition.json")
    tmp = final + ".tmp"
    with open(tmp, "w") as f:
        json.dump(edition, f, separators=(",", ":"), ensure_ascii=False)
    res = check_edition.check(tmp, ctx.cfg, decoder=check)
    sha = content_sha(edition)
    top = sum(a.get("topRank") is not None for a in edition["articles"])
    if not res.ok:
        problem = "; ".join(res.problems[:3])
        ctx.db.conn.execute("INSERT INTO publishes VALUES (?,?,?,?,?,?,?)",
                            (iso(ctx.now), 0, ctx.outage, len(edition["articles"]), top, sha, problem))
        os.replace(tmp, final + ".rejected")
        from . import health
        health.notify(ctx.db, "v1_check", f"edition not published: {problem}")
        return False, problem
    os.replace(tmp, final)
    ctx.db.conn.execute("INSERT INTO publishes VALUES (?,?,?,?,?,?,?)",
                        (iso(ctx.now), 1, ctx.outage, len(edition["articles"]), top, sha, None))
    for a in edition["articles"]:
        ctx.db.conn.execute(
            "INSERT INTO shipped VALUES (?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET last_at = excluded.last_at",
            (a["eventId"], a["publishedAt"], iso(ctx.now), iso(ctx.now), _first_seen(tabs, a["eventId"])))
        first = _first_seen(tabs, a["eventId"], "first_report_at")
        if first:
            ctx.db.conn.execute("INSERT INTO story_first_report VALUES (?,?) ON CONFLICT(event_id) DO UPDATE SET"
                                " at = MIN(at, excluded.at)", (a["eventId"], first))
    # The cards ranked for push alerts, next to the edition (the next app release's sender reads it).
    notes = final[:-len("edition.json")] + "notifications.json"
    with open(notes + ".tmp", "w") as f:
        json.dump({"publishedAt": edition["publishedAt"], "candidates": report.get("notifications") or []}, f,
                  separators=(",", ":"), ensure_ascii=False)
    os.replace(notes + ".tmp", notes)
    for twin, primary in report.get("twins") or ():
        ctx.db.conn.execute("INSERT OR IGNORE INTO twins VALUES (?,?,?,?)", (twin, primary, None, iso(ctx.now)))
    images_mod.prune(out_dir, used_images)
    return True, None


def _first_seen(tabs, eid, key="first_seen_at"):
    for ranked in (tabs or {}).values():
        for c, _ in ranked:
            if c.id == eid:
                return c.data.get(key)
    return None


def publish(db, cfg, srcs, now, out_dir, embedder=None, outage=None, check=True, session=None, editorial=None,
            force=False):
    """One publish. Skips writing when nothing changed and the last publish is under `publish.every_min` old."""
    ed = editorial or editorial_mod.load(cfg, now, resolve=lambda e: _resolve(db, e))
    ctx = Ctx(db, cfg, srcs, now, outage, ed)
    store = images_mod.Images(db, cfg, out_dir, session=session, now=now)
    edition, report, used, tabs = build(ctx, embedder, out_dir, store)
    sha = content_sha(edition)
    last = db.one("SELECT at, sha FROM publishes WHERE ok = 1 ORDER BY at DESC LIMIT 1")
    every = timedelta(minutes=(cfg.get("publish") or {}).get("every_min", 5))
    if not force and last is not None and last["sha"] == sha and now - from_iso(last["at"]) < every \
            and os.path.exists(os.path.join(out_dir, "edition.json")):
        report["published"] = False
        report["skipped"] = "unchanged"
        return report
    ok, problem = write(ctx, edition, report, used, out_dir, check, tabs)
    report["published"], report["problem"] = ok, problem
    if ok:
        report["imageUrls"] = image_url_check(db, cfg, edition, now, session)
    db.commit()
    return report


IMAGE_CHECK_EVERY = timedelta(minutes=30)


def image_url_check(db, cfg, edition, now, session=None, sample=3):
    """Do the edition's image URLs resolve? The files are written under out_dir/img and the URLs point at
    publish.base_url, so they resolve only once out_dir is served there. In a test run with no git_remote nothing
    is served (the VPS test edition's images all returned 404): that is reported as served: false rather than
    fetched. With a remote, a few URLs are fetched every 30 min and a failure alerts."""
    pub = cfg.get("publish") or {}
    urls = [a["image"] for a in edition["articles"] if a.get("image")]
    if not pub.get("git_remote"):
        return {"served": False, "why": "publish.git_remote is null: the edition and img/ exist only in out_dir",
                "urls": len(urls)}
    last = from_iso(db.get_meta("image_check_at"))
    if last and now - last < IMAGE_CHECK_EVERY:
        return json.loads(db.get_meta("image_check") or "null")
    import requests
    bad = []
    for u in urls[:sample]:
        try:
            r = (session or requests).head(u, timeout=10, allow_redirects=True)
            if r.status_code != 200:
                bad.append(f"{u}: HTTP {r.status_code}")
        except requests.RequestException as e:
            bad.append(f"{u}: {type(e).__name__}")
    out = {"served": True, "checked": min(sample, len(urls)), "failed": bad}
    db.set_meta("image_check_at", iso(now))
    db.set_meta("image_check", json.dumps(out))
    if bad:
        from . import health
        health.notify(db, "image_urls", f"edition image URLs do not resolve: {bad[0]}")
    return out


def _resolve(db, eid):
    row = db.one("SELECT id FROM event_alias WHERE old_id = ?", eid)
    return row["id"] if row else eid
