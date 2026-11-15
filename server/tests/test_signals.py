"""Signals (Techmeme, Hacker News, Lobsters): the JSON feeds, points that keep growing after the first poll, and
strong signals imported as items so a story only they carry can become a card. From the 2026-11-15 review of the
VPS test run. Needs feedparser (worker.fetch), like test_timestamps.py.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("feedparser")

from worker import cluster, embed, fetch, publish_v1, sources as sources_mod  # noqa: E402
from worker.db import iso  # noqa: E402
from test_spice_and_truth import NOW, S, SRCS, _db, _item, _judge  # noqa: E402


# Signals: live points, JSON feeds, promotion to cards

def _source(sid):
    return SRCS.by_id()[sid]


HN_BODY = json.dumps({"hits": [
    {"objectID": "101", "title": "Lovable apps expose passport details and 200K private messages",
     "url": "https://sec.example.org/lovable-leak?utm_source=hn", "points": 944, "num_comments": 360,
     "created_at_i": int((NOW - timedelta(hours=3)).timestamp())},
    {"objectID": "102", "title": "Ask HN: What are you working on?", "url": None, "points": 400,
     "num_comments": 800, "created_at_i": int((NOW - timedelta(hours=2)).timestamp())},
    {"objectID": "103", "title": "A small blog post", "url": "https://blog.example.net/post", "points": 30,
     "num_comments": 4, "created_at_i": int((NOW - timedelta(hours=1)).timestamp())}]}).encode()


def test_hn_api_and_lobsters_json_are_parsed():
    entries, feed = fetch.parse_entries(HN_BODY, _source("hn"), 100)
    assert not feed.bozo and len(entries) == 3
    e = next(x for x in entries if x.extra["points"] == 944)
    assert e.url_canonical == "https://sec.example.org/lovable-leak"
    assert fetch.canonical_url("https://www.nytimes.com/2026/11/15/x.html?unlocked_article_code=1.ab&smid=url-share") \
        == "https://www.nytimes.com/2026/11/15/x.html"
    assert e.extra == {"points": 944, "comments": 360, "hn_url": "https://news.ycombinator.com/item?id=101"}
    ask = next(x for x in entries if x.title.startswith("Ask HN"))
    assert ask.url == "https://news.ycombinator.com/item?id=102"
    lob = json.dumps([{"title": "Memory management in 40 C projects", "url": "https://c.example.org/survey",
                       "score": 71, "comment_count": 12, "comments_url": "https://lobste.rs/s/abc",
                       "created_at": "2026-11-15T10:00:00.000-06:00", "tags": ["c"]}]).encode()
    entries, _ = fetch.parse_entries(lob, _source("lobsters"), 100)
    assert entries[0].extra["points"] == 71 and entries[0].feed_published_at == datetime(
        2026, 11, 15, 16, 0, tzinfo=timezone.utc)
    assert fetch.parse_entries(b"<html>", _source("hn"), 100)[1].bozo


CFG = {"fetch": {"max_age_days": 7, "max_age_days_reads": 14, "first_run_skip_hours": 36, "max_items_per_feed": 100}}


def test_hn_points_keep_growing_after_the_first_poll():
    # 'All AI-generated code should be Open Source' stayed at 188 points in the DB while HN showed 358.
    db = _db()
    entries, _ = fetch.parse_entries(HN_BODY, _source("hn"), 100)
    fetch.ingest(db, _source("hn"), SRCS, entries, NOW, False, CFG)
    url = "https://sec.example.org/lovable-leak"
    assert json.loads(db.item(url)["extra"])["points"] == 944
    later = json.loads(HN_BODY)
    later["hits"][0]["points"], later["hits"][0]["num_comments"] = 1200, 500
    entries, _ = fetch.parse_entries(json.dumps(later).encode(), _source("hn"), 100)
    fetch.ingest(db, _source("hn"), SRCS, entries, NOW + timedelta(minutes=3), False, CFG)
    extra = json.loads(db.item(url)["extra"])
    assert extra["points"] == 1200 and extra["comments"] == 500


def test_strong_signals_become_items_and_keep_their_points():
    """"Lovable apps expose passport details" (HN 944) and "Bessent urges Nvidia to hire an outside CEO" (Bloomberg
    via Techmeme) were held only as signals, with no card."""
    db = _db()
    entries, _ = fetch.parse_entries(HN_BODY, _source("hn"), 100)
    fetch.ingest(db, _source("hn"), SRCS, entries, NOW, False, CFG)
    tm = "https://www.techmeme.com/261115/p8"
    db.insert_item({"url_canonical": tm, "url": tm, "source_id": "techmeme",
                    "title": "Sources: Scott Bessent urges Nvidia to hire an outside CEO (Jane Doe/Bloomberg)",
                    "feed_text": "Sources: Scott Bessent urges Nvidia to hire an outside CEO and wants answers.",
                    "extra": {"linked_url": "https://www.bloomberg.com/news/articles/bessent-nvidia"},
                    "first_seen_at": NOW, "published_at": NOW, "feed_published_at": NOW, "status": "signal"})
    assert fetch.promote_signals(db, SRCS, {}, NOW) == 2
    leak = db.item("https://sec.example.org/lovable-leak")
    assert leak["status"] == "new" and leak["source_id"] == "web"
    extra = json.loads(leak["extra"])
    assert extra["publisher"] == "sec.example.org" and extra["via"] == "hn"
    assert extra["signals"]["hn"]["points"] == 944
    bb = db.item("https://www.bloomberg.com/news/articles/bessent-nvidia")
    assert bb["status"] == "new" and bb["source_id"] == "bloomberg"
    assert bb["title"] == "Sources: Scott Bessent urges Nvidia to hire an outside CEO"
    assert bb["feed_text"].startswith("Sources: Scott Bessent")
    # Below the bar, a discussion page and anything already imported are left alone.
    assert db.item("https://blog.example.net/post")["status"] == "signal"
    assert db.item("https://news.ycombinator.com/item?id=102")["status"] == "signal"
    assert fetch.promote_signals(db, SRCS, {}, NOW) == 0
    # HN keeps counting on the imported article, and the signal attaches to its event with the points.
    later = json.loads(HN_BODY)
    later["hits"][0]["points"] = 1300
    entries, _ = fetch.parse_entries(json.dumps(later).encode(), _source("hn"), 100)
    fetch.ingest(db, _source("hn"), SRCS, entries, NOW + timedelta(minutes=3), False, CFG)
    assert json.loads(db.item(leak["url_canonical"])["extra"])["signals"]["hn"]["points"] == 1300
    item = _item(leak["url_canonical"], [1, 0, 0], leak["title"], source_id="web")
    item.publisher = "sec.example.org"
    store = cluster.Store(db, SRCS, S, NOW)
    eid, _ = cluster.create(store, item)
    cluster.attach_signals(store)
    cluster.refresh(store, store.events[eid], lambda s: True)
    row = store.events[eid].row
    assert json.loads(row["hn"])["points"] == 1300 and json.loads(row["publishers"]) == ["sec.example.org"]


def test_sources_file_is_wide_and_names_publishers_for_linked_articles():
    by_id = SRCS.by_id()
    assert len([s for s in SRCS.sources if s.polled]) >= 100
    for sid in ("bloomberg", "wsj", "nypost_tech", "businessinsider", "bleeping", "sec_press", "infoq", "lobsters",
                "et_funding", "crunchbase", "the_information", "mediagazer"):
        assert sid in by_id, sid
    assert not by_id["the_information"].is_signal and by_id["mediagazer"].kind == "techmeme"
    assert by_id["hn"].format == "hn_algolia" and "hnrss" not in by_id["hn"].url
    assert SRCS.for_host("https://www.reuters.com/technology/x").id == "reuters"
    assert SRCS.for_host("https://unknown.example.org/x") is None
    assert not by_id["reuters"].polled and not by_id["web"].polled
    # A publisher with no feed has nothing to fail a check on.
    assert sources_mod.effective_tier(by_id["reuters"], False) == 1
    assert sources_mod.effective_tier(by_id["techcrunch"], False) == 2


def test_a_source_can_seed_further_back_than_36_hours():
    # Reads shows 14 days and Dev 7; a source seeded with 36 hours of essays adds nothing to its tab for a week.
    by_id = SRCS.by_id()
    cfg = CFG
    assert by_id["stratechery"].seed_days == 14 and by_id["infoq"].seed_days == 7 and by_id["verge"].seed_days is None
    assert fetch.seed_horizon(by_id["verge"], cfg) == timedelta(hours=36)
    assert fetch.seed_horizon(by_id["infoq"], cfg) == timedelta(days=7)

    def status(source_id, age):
        e = fetch.Entry("https://x.test/a", "https://x.test/a", "A new release of the thing", NOW - age)
        return fetch.classify(e, by_id[source_id], SRCS, NOW, NOW, True, cfg)[0]
    assert status("verge", timedelta(days=3)) == "seen" and status("infoq", timedelta(days=3)) == "new"
    assert status("infoq", timedelta(days=8)) == "stale" and status("stratechery", timedelta(days=10)) == "new"

    from tools import reseed
    db = _db()
    for i, (sid, age, st) in enumerate([("infoq", 3, "seen"), ("infoq", 6.5, "seen"), ("verge", 3, "seen"),
                                        ("stratechery", 12, "seen"), ("infoq", 2, "written")]):
        u = f"https://x.test/{i}"
        db.insert_item({"url_canonical": u, "url": u, "source_id": sid, "title": f"Post {i} about a release",
                        "first_seen_at": NOW, "published_at": NOW - timedelta(days=age),
                        "feed_published_at": NOW - timedelta(days=age), "status": st})
    assert reseed.reseed(db, SRCS, cfg, NOW) == {"infoq": 2, "stratechery": 1}
    assert [db.item(f"https://x.test/{i}")["status"] for i in range(5)] == ["new", "new", "seen", "new", "written"]


# The review of the second VPS test run (2026-11-15)

def test_an_editors_pick_overrules_the_category_filter_and_the_screening_call():
    """Semafor's "Under pressure, Lutnick joins Trump in China" was dropped by rule source_tags while Techmeme
    headlined it; TechCrunch's "SNL gathers all the tech tycoons" was dropped triage_offtopic."""
    db = _db()
    semafor = _source("semafor")
    url = "https://www.semafor.com/article/11/15/2026/lutnick-joins-trump-in-china"
    tm = "https://www.techmeme.com/261115/p11"
    db.insert_item({"url_canonical": tm, "url": tm, "source_id": "techmeme",
                    "title": "Under pressure, Lutnick joins Trump in China (Semafor)", "extra": {"linked_url": url},
                    "first_seen_at": NOW, "published_at": NOW, "feed_published_at": NOW, "status": "signal"})
    picked = fetch.Entry(url=url, url_canonical=url, title="Under pressure, Lutnick joins Trump in China",
                         feed_published_at=NOW, tags=("Politics",))
    other = fetch.Entry(url=url + "-2", url_canonical=url + "-2", title="A politics story nobody picked",
                        feed_published_at=NOW, tags=("Politics",))
    fetch.ingest(db, semafor, SRCS, [picked, other], NOW, False, CFG)
    assert db.item(url)["status"] == "new" and db.item(url + "-2")["drop_rule"] == "source_tags"
    # Dropped first, picked later: the pick revives it, and it skips the screening call.
    snl = "https://techcrunch.com/2026/11/15/snl-gathers-all-the-tech-tycoons/"
    db.insert_item({"url_canonical": snl, "url": snl, "source_id": "techcrunch", "first_seen_at": NOW,
                    "title": "SNL gathers all the tech tycoons for a lesson in dating and spycraft",
                    "published_at": NOW, "feed_published_at": NOW, "status": "dropped",
                    "drop_rule": "triage_offtopic"})
    tm2 = "https://www.techmeme.com/261115/p16"
    db.insert_item({"url_canonical": tm2, "url": tm2, "source_id": "techmeme", "extra": {"linked_url": snl},
                    "title": "SNL gathers the tech tycoons (TechCrunch)", "first_seen_at": NOW, "published_at": NOW,
                    "feed_published_at": NOW, "status": "signal"})
    assert fetch.promote_signals(db, SRCS, {}, NOW) == 1
    row = db.item(snl)
    assert row["status"] == "new" and row["drop_rule"] is None and row["triaged"] == 1
    assert json.loads(row["extra"])["via"] == "techmeme"
    # A shopping or roundup drop is a rule about the item, not the topic: it stays dropped.
    deal = "https://techcrunch.com/2026/11/15/deal/"
    db.insert_item({"url_canonical": deal, "url": deal, "source_id": "techcrunch", "first_seen_at": NOW,
                    "title": "A deal", "published_at": NOW, "status": "dropped", "drop_rule": "title_shopping"})
    tm3 = "https://www.techmeme.com/261115/p17"
    db.insert_item({"url_canonical": tm3, "url": tm3, "source_id": "techmeme", "extra": {"linked_url": deal},
                    "title": "A deal (TechCrunch)", "first_seen_at": NOW, "published_at": NOW, "status": "signal"})
    assert fetch.promote_signals(db, SRCS, {}, NOW) == 0 and db.item(deal)["status"] == "dropped"


REDDIT = b"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>ULA loses Vulcan rocket and 40 Amazon satellites</title>
<link href="https://www.reddit.com/r/technology/comments/abc/ula_loses_vulcan/"/><updated>2026-11-15T16:00:00+00:00</updated>
<content type="html">&lt;a href="https://www.reddit.com/user/x"&gt; /u/x &lt;/a&gt; &lt;span&gt;&lt;a href="https://spaceflightnow.com/2026/11/15/vulcan/?utm_source=reddit"&gt;[link]&lt;/a&gt;&lt;/span&gt; &lt;span&gt;&lt;a href="https://www.reddit.com/r/technology/comments/abc/ula_loses_vulcan/"&gt;[comments]&lt;/a&gt;&lt;/span&gt;</content></entry>
<entry><title>A text post</title><link href="https://www.reddit.com/r/technology/comments/def/a_text_post/"/>
<updated>2026-11-15T15:00:00+00:00</updated>
<content type="html">&lt;span&gt;&lt;a href="https://www.reddit.com/r/technology/comments/def/a_text_post/"&gt;[link]&lt;/a&gt;&lt;/span&gt;</content></entry>
</feed>"""
def test_reddit_is_a_traction_signal_by_url_and_place_and_never_a_card():
    """sources.yaml had no Reddit signal at first; then every listed post counted, matched by title or by link.
    Now a post counts through the article it links, by its place in the day's listing. GitHub trending, which
    attached to nothing, is no longer a source."""
    reddit = _source("reddit_technology")
    assert reddit.is_signal and reddit.kind == "reddit" and "github_trending" not in SRCS.by_id()
    entries, feed = fetch.parse_entries(REDDIT, reddit, 100)
    assert not feed.bozo and len(entries) == 2
    link = next(e for e in entries if e.title.startswith("ULA"))
    # The listing is sorted by score: the first post gets fetch.REDDIT_LISTING points, the next one less.
    assert link.extra == {"linked_url": "https://spaceflightnow.com/2026/11/15/vulcan/", "points": 50}
    assert next(e for e in entries if e.title == "A text post").extra == {"points": 49}
    db = _db()
    fetch.ingest(db, reddit, SRCS, entries, NOW, False, CFG)
    assert fetch.promote_signals(db, SRCS, {}, NOW) == 0            # never imported as an article
    # The Reddit post attaches to the event of the article it links, and the event's traction counts it.
    report = _item("https://spaceflightnow.com/2026/11/15/vulcan/", [1, 0, 0], "ULA loses Vulcan rocket",
                   source_id="spaceflightnow", people=())
    other = _item("https://example.test/text", [0, 1, 0], "A text post", source_id="verge", people=())
    store = cluster.Store(db, SRCS, S, NOW, _judge({"A text post": "same"}, []))
    eid, _ = cluster.create(store, report)
    oid, _ = cluster.create(store, other)
    for r in db.q("SELECT url_canonical, title FROM items WHERE status = 'signal'"):
        db.conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?,?)",
                        (r["url_canonical"], "query", "m", 3, None, embed.to_blob(embed.normalize([0, 1, 0])),
                         iso(NOW)))
    # A title match made before this rule is removed; the text post (same title as an event, the judge agreeing)
    # attaches to nothing.
    db.conn.execute("INSERT INTO event_signals VALUES (?,?,?,?,?,?,?,?)",
                    (oid, "https://www.reddit.com/r/technology/comments/zzz/old/", "reddit_technology", "judge", None,
                     None, None, iso(NOW)))
    assert cluster.attach_signals(store) == 1
    assert [(r["event_id"], r["how"], r["points"]) for r in db.q("SELECT * FROM event_signals")] == [(eid, "url", 50)]
    cluster.refresh(store, store.events[eid], lambda s: True)
    assert json.loads(store.events[eid].row["social"]) == {"reddit": 50}
    assert publish_v1.reddit_top(50) and publish_v1.reddit_top(41) and not publish_v1.reddit_top(40)
    assert not publish_v1.reddit_top(1) and not publish_v1.reddit_top(None)     # listed, place unknown or low


def test_outlets_that_break_news_are_polled_every_three_minutes():
    """21 feeds polled at 3m and 104 at the default 10m: Engadget items were seen 14-22 min after publication."""
    by_id = SRCS.by_id()
    for sid in ("ft", "fortune", "nypost_business", "gizmodo", "spaceflightnow", "livemint_companies", "et_startups",
                "engadget", "axios", "bleeping", "decoder"):
        assert by_id[sid].poll == "3m", sid
    assert sum(s.poll == "3m" and not s.is_signal for s in SRCS.sources) >= 55
    assert by_id["stratechery"].poll == "10m" and by_id["rust_blog"].poll == "10m"
    # More open feeds for the Dev and Startups tabs.
    for sid in ("geekwire", "siliconangle", "tc_startups", "infoworld", "sdtimes", "jetbrains"):
        assert sid in by_id
