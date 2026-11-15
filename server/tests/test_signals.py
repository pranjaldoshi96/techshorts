"""Signals (Techmeme, Hacker News, Lobsters): the JSON feeds, points that keep growing after the first poll, and
strong signals imported as items so a story only they carry can become a card. From the 2026-11-15 review of the
VPS test run. Needs feedparser (worker.fetch), like test_timestamps.py.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("feedparser")

from worker import cluster, fetch, sources as sources_mod  # noqa: E402
from test_spice_and_truth import NOW, S, SRCS, _db, _item  # noqa: E402


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
