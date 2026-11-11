"""WP4: the v1 edition from the 2026-11-11 fixture replay, its checks, images, degraded mode and the git mirror.

No network: images come from a fake session, headline vectors from tests/fixtures/wp4_headlines.json (recorded
query embeddings of the fixture headlines). The 1.2 decoder test needs swiftc (Mac) or Docker (the box) and is
skipped without either.
"""
import hashlib
import io
import itertools
import json
import os
import shutil
import subprocess
from datetime import timedelta

import numpy as np
import pytest

import test_cluster as tc
from tools import check_edition
from worker import degraded, editorial, embed, health, images, mirror, publish_v1, rank

HEADLINES = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "wp4_headlines.json")))["headlines"]
T_JOIN = tc.CFG["cluster"]["t_join"]
CATEGORIES = {"Top", "AI", "Startups", "Dev", "Blogs"}


def load_headline_vectors(db):
    for h, d in HEADLINES.items():
        db.conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?,?)",
                        ("headline:" + hashlib.sha1(h.encode()).hexdigest()[:16], "headline", "fixture", 1024, None,
                         embed.to_blob(embed.unpack_int8(d)), "2026-11-11T00:00:00Z"))


@pytest.fixture(scope="module")
def replayed():
    db = tc.replay("published")
    load_headline_vectors(db)
    return db


def build(db, outage=None, now=None, ed=None, out_dir=None, store=None):
    ctx = publish_v1.Ctx(db, tc.CFG, tc.SRCS, now or db.end, outage, ed)
    return publish_v1.build(ctx, None, out_dir, store)


@pytest.fixture(scope="module")
def edition(replayed):
    return build(replayed)


def by_title(edition_dict, start):
    return [a for a in edition_dict["articles"] if a["title"].startswith(start)]


# v1 output

def test_every_category_is_in_the_1_2_enum_and_every_top_article_is_ranked(edition):
    ed, report, _, tabs = edition
    arts = ed["articles"]
    assert arts and {a["category"] for a in arts} <= CATEGORIES
    assert all(isinstance(a["topRank"], int) for a in arts if a["category"] == "Top")
    ranks = sorted(a["topRank"] for a in arts if "topRank" in a)
    assert ranks == list(range(len(tabs["top"])))
    assert len({a["url"] for a in arts}) == len(arts)


def test_every_article_has_ai_text_or_is_flagged_degraded(edition):
    for a in edition[0]["articles"]:
        if a["aiText"]:
            assert a["headline"] and a["summary"] and a["title"] == a["headline"] and a["excerpt"] == a["summary"]
            assert 45 <= len(a["summary"].split()) <= 62 and len(a["headline"]) <= 72
        else:
            assert "headline" not in a and "summary" not in a and len(a["excerpt"].split()) <= 55


def test_rules_check_passes(edition, tmp_path):
    p = tmp_path / "edition.json"
    p.write_text(json.dumps(edition[0]))
    res = check_edition.check(str(p), {"state_dir": str(tmp_path)}, decoder=False)
    assert res.ok, res.problems


def test_sources_and_coverage_use_v1_names(edition):
    names = {s.v1_name for s in tc.SRCS.sources}
    for a in edition[0]["articles"]:
        assert a["source"] in names
        assert set(a.get("alsoCoveredBy", [])) <= names and a["source"] not in a.get("alsoCoveredBy", [])
    ipo = [a for a in edition[0]["articles"] if a.get("topRank") == 0][0]
    assert "Anthropic" in ipo["title"] and len(ipo["alsoCoveredBy"]) >= 4


# Top contents

def test_no_top_item_is_irrelevant_or_an_excluded_type(edition):
    _, _, _, tabs = edition
    for c, _ in tabs["top"]:
        assert c.relevance >= 2 and c.type not in rank.NEVER_TYPES and c.importance >= 6
        assert c.type in rank.TOP_TYPES or (c.type in rank.LABELLED and c.importance >= 8)


def test_toyota_disney_ring_stay_out_of_top(edition):
    top_urls = {c.data["lead"]["url_canonical"] for c, _ in edition[3]["top"]}
    for start in ("Toyota Plans to Sell a Kei Pickup", "The VR Disney Plus app", "Ring’s Indoor Drone Camera"):
        u = tc.url(start)
        assert u not in top_urls, start
        # and not as another member of a Top event either
        assert all(u not in {m["url_canonical"] for m in c.data["members"]} for c, _ in edition[3]["top"])


# Deduplication and freshness

def test_top_headlines_are_pairwise_below_t_join(edition):
    top = edition[3]["top"]
    vecs = []
    for c, _ in top:
        h = c.data["headline"] or c.data["lead"]["title"]
        assert h in HEADLINES, h        # every Top headline has a recorded vector
        vecs.append(embed.unpack_int8(HEADLINES[h]))
    worst = max((float(a @ b) for a, b in itertools.combinations(vecs, 2)), default=0)
    assert worst < T_JOIN, worst


def test_a_weaker_copy_of_an_event_is_suppressed_everywhere():
    """The live Ring case: four outlets' event (importance 5, not Top) and a single-source copy at 6 that the
    clusterer missed. The copy must not reach Top in the event's place; a follow-up is never suppressed."""
    now = tc.EDITION_AT
    v = np.array([1.0, 0.0])
    near = np.array([0.7, 0.714])

    def c(eid, n, imp, vec, parent=None):
        x = rank.Candidate(id=eid, importance=imp, type="launch", is_news=True, relevance=3, topics=["hardware"],
                           region="global", n_publishers=n, on_techmeme=False, hn_points=0, authority=1.0,
                           published_at=now, lead_source=eid, lead_publisher=eid, headline_vec=vec)
        x.data = {"parent_id": parent}
        return x
    ring, copy, other = c("ev_ring", 4, 5, v), c("ev_copy", 1, 6, near), c("ev_x", 1, 6, np.array([0.0, 1.0]))
    kept, dropped = publish_v1.suppress_duplicates([copy, ring, other], T_JOIN)
    assert [k.id for k in kept] == ["ev_ring", "ev_x"] and dropped == [("ev_copy", "ev_ring")]
    follow = c("ev_follow", 1, 9, near, parent="ev_ring")
    assert len(publish_v1.suppress_duplicates([ring, follow], T_JOIN)[0]) == 2


def test_one_card_for_the_ipo(replayed, edition):
    ipo_events = {tc.event_of(replayed, u) for u in tc.IPO_FILING}
    cards = [a for a in edition[0]["articles"] if a["eventId"] in ipo_events]
    assert len(ipo_events) == 1 and len(cards) == 1


def test_event_age_is_from_event_published_at(edition):
    """Freshness is judged from event_published_at, the earliest member's publisher time, never the worker's clock."""
    for ranked in edition[3].values():
        for c, _ in ranked:
            assert c.published_at == min(tc.from_iso(m["published_at"]) for m in c.data["members"])


def test_windows_at_publish_time(replayed, edition):
    for tid, ranked in edition[3].items():
        for c, _ in ranked:
            assert c.age_eff(replayed.end) <= rank.window_h(c, rank.TABS[tid]), (tid, c.id)


# 2.11 property on the fixture: Top #1 changes within 24 h

def test_fixture_top1_yields_within_a_day(replayed, edition):
    first = edition[3]["top"][0][0]
    later = replayed.end + timedelta(hours=24)
    cands, _ = publish_v1.load_candidates(publish_v1.Ctx(replayed, tc.CFG, tc.SRCS, later))
    fresh = rank.Candidate(id="ev_fresh", importance=6, type="funding", is_news=True, relevance=3,
                           topics=["startups_funding"], region="global", n_publishers=1, on_techmeme=True,
                           hn_points=0, authority=1.2, published_at=later - timedelta(hours=2),
                           lead_source="techcrunch", lead_publisher="TechCrunch")
    ranked = rank.assemble(cands + [fresh], rank.TABS["top"], later)
    assert ranked[0][0].id != first.id
    assert not (first.importance == 10 and first.material_at and later - first.material_at < timedelta(hours=6))


# Degraded mode

def test_llm_outage_publishes_degraded_with_at_least_15_top(replayed, tmp_path):
    db = replayed
    db.log_write(db.end, None, "write", "m", "http_401")
    outage = degraded.llm_outage(db, db.end)
    assert outage == "auth"
    # New tier-1 items arrive during the outage: nothing classifies or clusters them.
    titles = ["Apple recalls chargers in Europe", "Samsung opens a chip plant in Texas",
              "Intel names a new chief financial officer", "Spotify raises prices for family plans",
              "Microsoft settles a Teams bundling case", "Stripe launches stablecoin accounts"]
    for i, (src, title) in enumerate(zip(["bbc", "guardian", "cnbc", "verge", "nyt", "techcrunch"], titles)):
        u = f"https://example.com/{src}/outage-{i}"
        db.insert_item({"url_canonical": u, "url": u, "source_id": src, "title": title,
                        "feed_text": "A company announced a thing on Tuesday. It matters for builders. " * 3,
                        "first_seen_at": db.end, "published_at": db.end - timedelta(minutes=10 * i),
                        "feed_published_at": db.end - timedelta(minutes=10 * i), "status": "new"})
    try:
        ed, report, _, tabs = build(db, outage)
        assert ed["degraded"] == "auth"
        assert len(tabs["top"]) >= 15, len(tabs["top"])
        arts = ed["articles"]
        assert sum(not a["aiText"] for a in arts) >= 6
        assert all((a.get("headline") and a.get("summary")) or a["aiText"] is False for a in arts)
        # Previously validated text is reused.
        assert any(a["aiText"] for a in arts if "topRank" in a)
        p = tmp_path / "e.json"
        p.write_text(json.dumps(ed))
        assert check_edition.check(str(p), {"state_dir": str(tmp_path)}, decoder=False).ok
    finally:
        db.conn.execute("DELETE FROM write_log")
        db.conn.execute("DELETE FROM items WHERE url_canonical LIKE 'https://example.com/%'")


def _held_item(db, u, src, title, first_seen, write=None, vec_of=None):
    db.insert_item({"url_canonical": u, "url": u, "source_id": src, "title": title,
                    "feed_text": "A company announced a thing on Tuesday. It matters for builders. " * 3,
                    "first_seen_at": first_seen, "published_at": first_seen - timedelta(minutes=5),
                    "feed_published_at": first_seen - timedelta(minutes=5), "status": "held" if write else "new"})
    if write:
        cols = {"url_canonical": u, "status": "thin", "written_at": tc.iso(first_seen), "text_words": 26,
                "is_news": 1, "region": "us", **write}
        db.conn.execute("INSERT INTO writes (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                        list(cols.values()))
    if vec_of:
        db.conn.execute("INSERT INTO embeddings SELECT ?, kind, model, dims, input_sha, vec, at FROM embeddings"
                        " WHERE url_canonical = ? AND kind = 'passage'", (u, vec_of))


def test_outage_keeps_earlier_judgements_and_one_card_per_story(replayed):
    """Items the LLM judged before the outage keep that judgement: the held Toyota truck (relevance 1), a held
    explainer and an aquarium screen saver stay out of Top, even though their tier-1 leads would carry an
    unclassified singleton there. And a singleton that repeats the IPO event is dropped, with or without a stored
    vector, so the degraded Top passes the same content and dedup rules as the normal one."""
    db = replayed
    end = db.end
    db.log_write(end - timedelta(hours=3), None, "write", "m", "ok")      # last success: the outage starts here
    db.log_write(end - timedelta(minutes=30), None, "write", "m", "http_401")
    outage = degraded.llm_outage(db, end)
    assert outage == "auth" and degraded.outage_since(db, outage, end) == end - timedelta(hours=3)
    ex = "https://example.com/"
    toyota = tc.url("Toyota Plans to Sell a Kei Pickup")
    judged = {
        ex + "nyt/blackberries": ("nyt", "What BlackBerries and Horseshoes Reveal About Our Presidents",
                                  {"type": "explainer", "relevance": 1, "importance": 2, "is_news": 0,
                                   "topics": '["policy"]'}),
        ex + "verge/aquarium": ("verge", "A Fan-Favorite Aquarium Screen Saver Is Leaving Roku",
                                {"type": "news", "relevance": 1, "importance": 1, "topics": '["consumer"]'}),
        # A relevant, Top-worthy copy of an IPO member, held thin before the outage; its stored passage vector is
        # the original's, so only the vector check can tell.
        ex + "nyt/ipo-paydays": ("nyt", "Big Tech's Windfall: What the Listing Means for Two Backers",
                                 {"type": "news", "relevance": 3, "importance": 7,
                                  "topics": '["ai_business"]'}),
    }
    new = {   # first seen during the outage, never classified
        ex + "cnbc/ipo-unvectored": ("cnbc", "Anthropic IPO: Amazon and Google stand to gain billions"),
        ex + "bbc/outage-chargers": ("bbc", "Apple recalls chargers in Europe"),
        ex + "guardian/outage-chip": ("guardian", "Samsung opens a chip plant in Texas"),
    }
    try:
        for u, (src, title, w) in judged.items():
            _held_item(db, u, src, title, end - timedelta(hours=1), w,
                       vec_of=tc.url("Amazon and Google Will See Big Paydays") if "ipo" in u else None)
        for u, (src, title) in new.items():
            _held_item(db, u, src, title, end - timedelta(minutes=10))
        ed, report, _, tabs = build(db, outage)
        top = tabs["top"]
        assert len(top) >= 15, len(top)
        urls = {c.data["lead"]["url_canonical"] for c, _ in top}
        assert toyota not in urls and not urls & set(judged)
        assert ex + "cnbc/ipo-unvectored" not in urls
        assert {ex + "bbc/outage-chargers", ex + "guardian/outage-chip"} <= urls
        ipo_events = {tc.event_of(db, u) for u in tc.IPO_FILING}
        assert sum(c.id in ipo_events for c, _ in top) == 1
        for c, _ in top:
            if c.classified:    # the normal Top-contents rules
                assert c.relevance >= 2 and c.type not in rank.NEVER_TYPES and c.importance >= 6, c.id
            else:               # only never-judged tier-1 items from the outage itself
                assert c.lead_tier1 and c.data["lead"]["first_seen_at"] >= tc.iso(end - timedelta(hours=3))
        # Pairwise dedup: recorded headline vectors below T_join; cards without one share < 2 title keywords
        # with every other Top card's members.
        vecs = [embed.unpack_int8(HEADLINES[h]) for h in (c.data["headline"] or c.data["lead"]["title"]
                                                          for c, _ in top) if h in HEADLINES]
        assert max((float(a @ b) for a, b in itertools.combinations(vecs, 2)), default=0) < T_JOIN
        for c, _ in top:
            if c.data.get("singleton"):
                k = publish_v1._keywords(c.data["lead"]["title"])
                for o, _ in top:
                    if o is not c:
                        assert all(len(k & publish_v1._keywords(m["title"])) < 2 for m in o.data["members"]), \
                            (c.data["lead"]["title"], o.data["lead"]["title"])
    finally:
        db.conn.execute("DELETE FROM write_log")
        for t in ("items", "writes", "embeddings"):
            db.conn.execute(f"DELETE FROM {t} WHERE url_canonical LIKE 'https://example.com/%'")


def test_held_lead_with_two_publishers_still_publishes_degraded(replayed):
    db = replayed
    ev = db.one("SELECT * FROM events WHERE status = 'live' AND n_publishers >= 2 AND headline IS NOT NULL"
                " ORDER BY importance DESC LIMIT 1")
    db.conn.execute("UPDATE events SET headline = NULL, summary = NULL WHERE id = ?", (ev["id"],))
    try:
        ed = build(db)[0]
        a = next(a for a in ed["articles"] if a["eventId"] == ev["id"])
        assert a["aiText"] is False and "headline" not in a and a["title"]
    finally:
        db.conn.execute("UPDATE events SET headline = ?, summary = ? WHERE id = ?",
                        (ev["headline"], ev["summary"], ev["id"]))


def test_editorial_kill_pin_override(replayed):
    db = replayed
    base = build(db)[3]
    top = [c.id for c, _ in base["top"]]
    ed = editorial.Editorial(kill={top[0]}, pins={top[-1]: {"top"}},
                             override={top[1]: {"headline": "Owner headline here", "summary": "Owner summary."}})
    edition_dict, _, _, tabs = build(db, ed=ed)
    ids = [c.id for c, _ in tabs["top"]]
    assert top[0] not in ids and ids[0] == top[-1]
    assert next(a for a in edition_dict["articles"] if a["eventId"] == top[1])["title"] == "Owner headline here"


# The 1.2 decoder

def _decoder_available():
    return shutil.which("swiftc") or shutil.which("docker")


@pytest.mark.skipif(not _decoder_available(), reason="needs swiftc or docker for tools/v1decode")
def test_the_1_2_decoder_decodes_the_edition(edition, tmp_path_factory):
    state = tmp_path_factory.mktemp("state")
    p = state / "edition.json"
    p.write_text(json.dumps(edition[0], ensure_ascii=False))
    res = check_edition.check(str(p), {"state_dir": str(state)})
    assert res.ok, res.problems
    assert res.decoder["articles"] == len(edition[0]["articles"])
    assert res.decoder["topRanked"] == len(edition[3]["top"])
    # And it refuses what 1.2 can't decode.
    bad = dict(edition[0], articles=[dict(edition[0]["articles"][0], category="India")])
    p.write_text(json.dumps(bad))
    res = check_edition.check(str(p), {"state_dir": str(state)})
    assert not res.ok and any("1.2 decoder" in x for x in res.problems)


def test_rules_catch_what_1_2_would_reject_or_hide():
    good = {"url": "https://a.com/x", "title": "T", "excerpt": "E", "content": "E", "source": "BBC Technology",
            "category": "Top", "publishedAt": "2026-11-11T04:30:33Z", "topRank": 0, "aiText": False}
    cases = {
        "category": dict(good, category="India"),
        "topRank": {k: v for k, v in good.items() if k != "topRank"},
        "url": dict(good, url="https://a.com/x y"),
        "publishedAt": dict(good, publishedAt="2026-11-11T04:30:33.120Z"),
        "AI text": {k: v for k, v in good.items() if k != "aiText"},
    }
    for name, art in cases.items():
        res = check_edition.Result()
        check_edition.rules({"publishedAt": "2026-11-11T04:30:33Z", "articles": [art]}, res)
        assert not res.ok, name
    res = check_edition.Result()
    check_edition.rules({"publishedAt": "2026-11-11T04:30:33Z", "articles": [good]}, res)
    assert res.ok, res.problems
    assert publish_v1.clean_url("https://a.com/café bar?q=1") == "https://a.com/caf%C3%A9%20bar?q=1"


# Images

def picture(w, h, color=(200, 30, 30), pattern=True):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), color)
    if pattern:
        d = ImageDraw.Draw(img)
        for i in range(0, w, 37):
            d.line((i, 0, w - i, h), fill=(i % 255, 90, 200 - i % 200), width=9)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


class FakeResp:
    def __init__(self, data, status=200):
        self.ok, self.status_code = status == 200, status
        self.raw = io.BytesIO(data)
        self.raw.read = (lambda f: lambda n=-1, decode_content=True: f(n))(self.raw.read)


class FakeSession:
    def __init__(self, files):
        self.files, self.calls = files, []

    def get(self, url, **kw):
        self.calls.append(url)
        return FakeResp(*self.files[url]) if url in self.files else FakeResp(b"", 404)


def test_image_checks_and_variants(tmp_path):
    from PIL import Image
    files = {"https://i/good.jpg": (picture(1600, 900),), "https://i/narrow.jpg": (picture(500, 300),),
             "https://i/square.jpg": (picture(1000, 1000),), "https://i/tiny.jpg": (b"x" * 500,)}
    db = tc.new_db()
    store = images.Images(db, tc.CFG, str(tmp_path), session=FakeSession(files), now=tc.EDITION_AT)
    got = store.resolve({"ev_a": ["https://i/narrow.jpg", "https://i/good.jpg"], "ev_b": ["https://i/square.jpg"],
                         "ev_c": ["https://i/tiny.jpg"]})
    assert got["ev_b"] is None and got["ev_c"] is None
    reasons = {r["src"]: r["reason"] for r in db.q("SELECT * FROM images")}
    assert reasons["https://i/narrow.jpg"] == "narrow" and reasons["https://i/square.jpg"] == "aspect"
    assert reasons["https://i/tiny.jpg"] == "small_file"
    row = got["ev_a"]
    std = Image.open(tmp_path / "img" / f"{row['sha8']}_std.jpg")
    thumb = Image.open(tmp_path / "img" / f"{row['sha8']}_thumb.webp")
    assert std.size == (720, 450) and std.format == "JPEG" and std.info.get("progressive")
    assert thumb.size == (360, 225) and thumb.format == "WEBP"
    # Known results aren't fetched again.
    calls = len(store.session.calls)
    store.resolve({"ev_a": ["https://i/narrow.jpg", "https://i/good.jpg"]})
    assert len(store.session.calls) == calls


def test_dhash_matches_the_same_picture_reencoded():
    from PIL import Image
    a = Image.open(io.BytesIO(picture(1600, 900)))
    buf = io.BytesIO()
    a.resize((1200, 675)).save(buf, "JPEG", quality=60)
    b = Image.open(io.BytesIO(buf.getvalue()))
    other = Image.open(io.BytesIO(picture(1600, 900, color=(20, 200, 20), pattern=False)))
    assert rank.hamming(images.dhash(a), images.dhash(b)) <= rank.IMAGE_HAMMING
    assert rank.hamming(images.dhash(a), images.dhash(other)) > rank.IMAGE_HAMMING


def test_generated_image_and_edition_image_urls(replayed, tmp_path):
    from PIL import Image
    name = images.generated(str(tmp_path), "ai")
    assert Image.open(tmp_path / "img" / f"{name}_std.jpg").size == (720, 450)
    store = images.Images(replayed, tc.CFG, str(tmp_path), session=FakeSession({}), now=replayed.end)
    store.page_images = lambda rows: None       # no page fetches offline
    ed = build(replayed, out_dir=str(tmp_path), store=store)[0]
    base = tc.CFG["publish"]["base_url"]
    for a in ed["articles"]:
        assert a["image"].startswith(base + "img/gen_") and a["image"].endswith("_std.jpg")
        assert os.path.exists(tmp_path / a["image"][len(base):])


def test_prune_keeps_used_and_recent(tmp_path):
    d = tmp_path / "img"
    d.mkdir()
    for n in ("old_std.jpg", "used_std.jpg", "new_std.jpg"):
        (d / n).write_bytes(b"x")
    old = os.path.getmtime(d / "new_std.jpg") - 15 * 86400
    os.utime(d / "old_std.jpg", (old, old))
    os.utime(d / "used_std.jpg", (old, old))
    assert images.prune(str(tmp_path), {"used"}) == 1
    assert sorted(os.listdir(d)) == ["new_std.jpg", "used_std.jpg"]


# Publish, health and the mirror

def test_publish_writes_checks_and_skips_unchanged(replayed, tmp_path, monkeypatch):
    db = replayed
    out = tmp_path / "site"
    session = FakeSession({})
    cfg = dict(tc.CFG, state_dir=str(tmp_path))
    monkeypatch.setattr(images.Images, "page_images", lambda self, rows: None)
    kw = dict(check=False, session=session, editorial=editorial.Editorial())
    try:
        r1 = publish_v1.publish(db, cfg, tc.SRCS, db.end, str(out), **kw)
        assert r1["published"] and (out / "edition.json").exists()
        r2 = publish_v1.publish(db, cfg, tc.SRCS, db.end + timedelta(minutes=1), str(out), **kw)
        assert r2["published"] is False and r2["skipped"] == "unchanged"
        r3 = publish_v1.publish(db, cfg, tc.SRCS, db.end + timedelta(minutes=6), str(out), **kw)
        assert r3["published"]
        assert db.one("SELECT COUNT(*) n FROM shipped")["n"] == r1["articles"]
        h = health.build(db, tc.SRCS, r3, db.end + timedelta(minutes=6))
        assert h["edition"]["editionAgeMin"] == 0 and h["edition"]["top"] == len(build(db)[3]["top"])
        assert h["freshness"]["publisherToAppMin"]["p50"] is not None
    finally:
        db.conn.execute("DELETE FROM publishes")
        db.conn.execute("DELETE FROM shipped")


def test_publish_refuses_an_edition_that_fails_the_check(replayed, tmp_path, monkeypatch):
    db = replayed
    out = tmp_path / "site"
    out.mkdir()
    (out / "edition.json").write_text('{"previous": true}')
    monkeypatch.setattr(publish_v1, "V1_CATEGORY", {**publish_v1.V1_CATEGORY, "dev": "Developers"})
    monkeypatch.setattr(images.Images, "page_images", lambda self, rows: None)
    cfg = dict(tc.CFG, state_dir=str(tmp_path))
    r = publish_v1.publish(db, cfg, tc.SRCS, db.end, str(out), check=False, session=FakeSession({}),
                           editorial=editorial.Editorial())
    try:
        assert r["published"] is False and "Developers" in r["problem"]
        assert json.loads((out / "edition.json").read_text()) == {"previous": True}
        assert db.one("SELECT ok FROM publishes ORDER BY at DESC LIMIT 1")["ok"] == 0
    finally:
        db.conn.execute("DELETE FROM publishes")
        db.conn.execute("DELETE FROM meta WHERE key LIKE 'alert:%'")


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_mirror_force_pushes_one_amended_commit(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    out = tmp_path / "site"
    out.mkdir()
    (out / "edition.json").write_text("{}")
    (out / "edition.json.tmp").write_text("partial")
    db = tc.new_db()
    cfg = {"publish": {"git_remote": str(remote), "git_branch": "edition", "git_every_min": 10, "deploy_key": ""}}
    assert mirror.push(db, cfg, str(out), tc.EDITION_AT) == "pushed"
    assert mirror.push(db, cfg, str(out), tc.EDITION_AT + timedelta(minutes=5)) == "waiting"
    (out / "edition.json").write_text('{"v": 2}')
    assert mirror.push(db, cfg, str(out), tc.EDITION_AT + timedelta(minutes=11)) == "pushed"
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", "edition"], capture_output=True,
                         text=True).stdout.strip().splitlines()
    assert len(log) == 1
    files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "edition"],
                           capture_output=True, text=True).stdout.split()
    assert "edition.json" in files and "edition.json.tmp" not in files
    assert mirror.push(db, {"publish": {}}, str(out), tc.EDITION_AT) is None


def test_v1decode_sources_are_the_1_2_release_files():
    """The frozen decoder copies must stay the 1.2 release's (eafaa0b); a change here is a v1 contract change."""
    import tools.check_edition as ce
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for f, src in (("Article.swift", "ios/Techgist/Models/Article.swift"),
                   ("Edition.swift", "ios/Techgist/Models/Edition.swift"),
                   ("Disk.swift", "ios/Techgist/Services/Disk.swift")):
        r = subprocess.run(["git", "-C", root, "show", f"eafaa0b:{src}"], capture_output=True)
        if r.returncode != 0:
            pytest.skip("not a git checkout with the 1.2 commit")
        assert open(os.path.join(ce.SWIFT_DIR, f), "rb").read() == r.stdout, f


def test_tick_without_llm_publishes_a_degraded_edition(tmp_path, monkeypatch):
    """The worker's own tick with no usable key: no LLM stage runs, and the edition still goes out, degraded."""
    pytest.importorskip("feedparser")
    from collections import Counter
    from worker import main
    db = tc.replay("published")
    load_headline_vectors(db)
    monkeypatch.setattr(main, "maybe_check_sources", lambda *a: None)
    monkeypatch.setattr(main, "fetch_due", lambda *a, **k: Counter())
    monkeypatch.setattr(main, "utcnow", lambda: db.end)
    monkeypatch.setattr(images.Images, "page_images", lambda self, rows: None)
    monkeypatch.setattr(images.Images, "download", lambda self, src: ("failed", "offline", None, None, None, None,
                                                                       None))
    cfg = dict(tc.CFG, state_dir=str(tmp_path))
    out = tmp_path / "site"
    counts = main.tick(db, cfg, client=None, embedder=None, out_dir=str(out), started_at=db.end)
    assert counts["published"] == 1
    ed = json.loads((out / "edition.json").read_text())
    h = json.loads((out / "health.json").read_text())
    assert ed["degraded"] == "no_key" and h["llmOutage"] == "no_key" and h["edition"]["degraded"] == "no_key"
    assert sum("topRank" in a for a in ed["articles"]) >= 10
