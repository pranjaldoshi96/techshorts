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
  - Degraded cards (no validated AI text) carry the publisher title and a <= 55-word excerpt, no headline or
    summary, and aiText: false. 1.2 ignores unknown keys, so aiText, eventId and the edition's degraded flag
    ride along for health checks and R2.
The file is written to a temp name, checked by tools/check_edition.py (the 1.2 decoder itself), and only then
renamed into place; a failed check keeps the previous edition and alerts.
"""
import hashlib
import json
import logging
import os
import re
import statistics
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote

from . import (degraded as degraded_mod, editorial as editorial_mod, embed as embed_mod, images as images_mod,
               importance as importance_mod, rank, sources as sources_mod)
from .db import from_iso, iso
from .llm import LLMError

log = logging.getLogger("techgist")

V1_CATEGORY = {"ai": "AI", "startups": "Startups", "dev": "Dev", "reads": "Blogs"}
URL_SAFE = ":/?#[]@!$&'()*+,;=%~-._"
MAX_WINDOW = timedelta(days=14)
OUTAGE_SINGLETON_WINDOW = timedelta(hours=36)


def clean_url(u):
    """Percent-encodes what older iOS URL(string:) refuses (spaces, non-ASCII); one bad URL fails the whole decode."""
    return quote((u or "").strip(), safe=URL_SAFE) if u else u


def _words(s):
    return set(w for w in re.findall(r"[a-z0-9$]+", (s or "").lower()) if len(w) > 2)


# Loading events

MEMBER_SQL = """
SELECT i.url_canonical, i.url, i.source_id, i.title, i.feed_text, i.image, i.og_image, i.published_at,
       i.first_seen_at, w.status, w.text
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
        name = ctx.v1_name(m["source_id"])
        if name not in publishers:
            publishers.append(name)
    hn = json.loads(ev["hn"] or "null") or {}
    over = ctx.editorial.override.get(ev["id"]) or {}
    headline = over.get("headline") or ev["headline"]
    summary = over.get("summary") or ev["summary"]
    ai_text = bool(headline and summary)
    lead_tier1 = ctx.tier1(lead["source_id"])
    if not ai_text and not (len(publishers) >= 2 or lead_tier1):
        return None, "no_text"      # held; publishes once its text validates or coverage grows
    c = rank.Candidate(
        id=ev["id"], importance=ev["importance"] or 1, type=ev["type"], is_news=bool(ev["is_news"]),
        relevance=ev["relevance"] or 0, topics=json.loads(ev["topics"] or "[]"), region=ev["region"] or "global",
        n_publishers=max(1, len(publishers)), on_techmeme=bool(ev["on_techmeme"]), hn_points=hn.get("points") or 0,
        authority=max([ctx.authority(m["source_id"]) for m in news] or [0.8]),
        published_at=from_iso(ev["published_at"]), material_at=from_iso(ev["last_material_update_at"]),
        lead_source=lead["source_id"], lead_publisher=ctx.v1_name(lead["source_id"]),
        lead_reads=bool(lead_src and lead_src.reads), lead_tier1=lead_tier1, ai_text=ai_text,
        classified=ev["type"] is not None, boost=ctx.editorial.boost.get(ev["id"], 1.0),
        pin=frozenset(ctx.editorial.pins.get(ev["id"], ())))
    c.data = {"lead": lead, "members": members, "publishers": publishers, "headline": headline, "summary": summary,
              "hn": hn, "parent_id": ev["parent_id"], "title_words": _words(lead["title"]),
              "first_seen_at": min(m["first_seen_at"] for m in members),
              "images": image_sources(ctx, lead, members)}
    return c, None


def outage_singletons(ctx, taken):
    """LLM outage only: tier-1 items that never made it into an event (the LLM and embeddings share the key, so
    nothing new clusters) publish alone with their publisher title, so the feed keeps moving."""
    rows = ctx.db.q(
        "SELECT i.*, w.status wstatus, w.type, w.is_news, w.relevance, w.importance, w.topics, w.region, w.headline,"
        " w.summary, w.text FROM items i LEFT JOIN writes w ON w.url_canonical = i.url_canonical"
        " WHERE i.status IN ('new', 'held', 'written') AND i.published_at >= ?"
        " AND i.url_canonical NOT IN (SELECT url_canonical FROM event_members)",
        iso(ctx.now - OUTAGE_SINGLETON_WINDOW))
    out = []
    from .cluster import event_id
    for r in rows:
        s = ctx.src(r["source_id"])
        if s is None or s.is_signal or not ctx.tier1(s.id) or r["url_canonical"] in taken:
            continue
        if ctx.editorial.blocked(s, r["url"]):
            continue
        r = dict(r)
        ok = r["wstatus"] == "ok" and r["type"] is not None
        topics = json.loads(r["topics"] or "[]") if ok else []
        imp = r["importance"] or 1
        if ok:
            imp = importance_mod.compute([importance_mod.Member(r["url_canonical"], s.id, s.v1_name, True, r["type"],
                                                                imp, topics, [])], r["type"], topics).value
        c = rank.Candidate(
            id=event_id(r["url_canonical"]), importance=imp, type=r["type"] if ok else None,
            is_news=bool(r["is_news"]) if ok else True, relevance=r["relevance"] or 0 if ok else 0, topics=topics,
            region=r["region"] or s.region, n_publishers=1, on_techmeme=False, hn_points=0,
            authority=float(s.authority), published_at=from_iso(r["published_at"]), lead_source=s.id,
            lead_publisher=s.v1_name, lead_reads=s.reads, lead_tier1=True,
            ai_text=bool(ok and r["headline"] and r["summary"]), classified=ok)
        lead = {k: r[k] for k in ("url_canonical", "url", "source_id", "title", "feed_text", "image", "og_image",
                                  "published_at", "first_seen_at", "text")}
        c.data = {"lead": lead, "members": [lead], "publishers": [s.v1_name], "headline": r["headline"] if ok else None,
                  "summary": r["summary"] if ok else None, "hn": {}, "title_words": _words(r["title"]),
                  "first_seen_at": r["first_seen_at"], "images": image_sources(ctx, lead, [lead]), "parent_id": None}
        out.append(c)
    return out


def load_candidates(ctx):
    held = {}
    out = []
    for ev in ctx.db.q("SELECT * FROM events WHERE status = 'live' AND published_at >= ?", iso(ctx.now - MAX_WINDOW)):
        c, why = event_candidate(ctx, ev)
        if c is None:
            held[why] = held.get(why, 0) + 1
        else:
            out.append(c)
    if ctx.outage:
        taken = {c.data["lead"]["url_canonical"] for c in out}
        out += outage_singletons(ctx, taken)
    return out, held


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
        db.conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?,?)",
                        (key, "headline", embedder.model, len(v), None, embed_mod.to_blob(v), iso(now)))
        for c in cs:
            c.headline_vec = v
    return len(todo)


def suppress_duplicates(cands, threshold):
    """One card per event across the whole edition: when two events' headlines are within T_join (a clustering
    miss, e.g. a single-source ET Tech report of the Ring drone launch that four other outlets ran as one event),
    only the better-covered one stays, in every tab. Without this the weaker copy can reach a tab the real event
    doesn't qualify for. A follow-up and its parent are both kept: the follow-up is the new fact (spec 2.9)."""
    order = sorted(cands, key=lambda c: (-c.n_publishers, -c.importance, c.published_at, c.id))
    kept, dropped = [], []
    for c in order:
        v = c.headline_vec
        twin = None if v is None else next(
            (k for k in kept if k.headline_vec is not None and float(v @ k.headline_vec) >= threshold
             and c.data.get("parent_id") != k.id and k.data.get("parent_id") != c.id), None)
        if twin is None:
            kept.append(c)
        else:
            dropped.append((c.id, twin.id))
    keep = {c.id for c in kept}
    return [c for c in cands if c.id in keep], dropped


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
    """`placements` is [(tab id, rank, tab length)] for the non-Top tabs the event made."""
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
    a = {"url": clean_url(lead["url_canonical"]), "title": title, "excerpt": body, "content": body,
         "source": c.lead_publisher, "category": category, "publishedAt": iso(c.published_at)}
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
    return a


def build(ctx, embedder=None, out_dir=None, image_store=None):
    """Candidates → headline vectors → images → tabs → v1 articles. Returns (edition dict, report dict)."""
    now = ctx.now
    cands, held = load_candidates(ctx)
    degraded = bool(ctx.outage)
    # Only events some tab could take need vectors and pictures; best first, so per-tick caps go to likely cards.
    tab_specs = rank.tabs_for(ctx.cfg)
    live = [c for c in cands if any(rank.eligible(c, tab_specs[t], now) is None or t in c.pin for t in rank.TAB_ORDER)]
    live.sort(key=lambda c: -rank.cand_score(c, tab_specs["top"], now, degraded))
    headline_vectors(ctx.db, embedder, live, now)
    live, twins = suppress_duplicates(live, ctx.cfg["cluster"]["t_join"])
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
    tabs = rank.build_tabs(live, now, degraded, ctx.cfg["cluster"]["t_join"], stats, tab_specs)
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
            name = row["sha8"] if row else images_mod.generated(out_dir, (c.topics or ["default"])[0])
            used_images.add(name)
            image_url = f"{base}img/{name}_std.jpg"
        articles.append(article(c, cat, top_rank.get(eid), image_url))
    edition = {"publishedAt": iso(now), "articles": articles, "degraded": ctx.outage or False}
    report = {
        "degraded": ctx.outage, "candidates": len(cands), "held": held, "leftOut": left_out,
        "duplicatesSuppressed": len(twins),
        "degradedCards": sum(not a["aiText"] for a in articles), "articles": len(articles),
        "tabs": {tid: _tab_report(ranked, now) for tid, ranked in tabs.items()},
        "filtered": stats, "topScores": [round(scores[c.id], 4) for c, _ in tabs["top"][:5]],
        "images": {"used": len(used_images), "generated": sum(n.startswith("gen_") for n in used_images)},
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
    images_mod.prune(out_dir, used_images)
    return True, None


def _first_seen(tabs, eid):
    for ranked in (tabs or {}).values():
        for c, _ in ranked:
            if c.id == eid:
                return c.data.get("first_seen_at")
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
    db.commit()
    return report


def _resolve(db, eid):
    row = db.one("SELECT id FROM event_alias WHERE old_id = ?", eid)
    return row["id"] if row else eid
