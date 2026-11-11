"""Fetch, timestamps and state (spec 2.3): published_at clamp, stale and first-run rules, canonical URLs,
conditional GET bookkeeping and tier-1 gating. No network: feeds come from tests/fixtures."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from worker import config, fetch, health, sources as sources_mod
from worker.db import DB, from_iso, iso
from worker.sources import Source, SourceList

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 11, 3, 30, tzinfo=timezone.utc)
CFG = config._merge(config.DEFAULTS, {})

NEWS = Source(id="example", name="Example", v1_name="Example", url="https://example.com/feed", tier=1, poll="3m")
HN = Source(id="hn", name="Hacker News", v1_name="Hacker News", url="https://hnrss.org/frontpage", tier="signal",
            poll="3m", fetch_page=False)
TECHMEME = Source(id="techmeme", name="Techmeme", v1_name="Techmeme", url="https://www.techmeme.com/feed.xml",
                  tier="signal", poll="3m", fetch_page=False)
SRCS = SourceList([NEWS, HN, TECHMEME], ["blocked.example"])


def dt(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = DB(str(tmp_path / "state.db"))
    yield d
    d.close()


def body(name):
    return (FIXTURES / name).read_bytes()


def ok(source, name, etag='"v1"', lm="Wed, 11 Nov 2026 03:00:00 GMT"):
    return fetch.FetchResult(source.id, status=200, body=body(name), etag=etag, last_modified=lm)


def poll(db, source, res, now):
    stats = fetch.record(db, source, SRCS, res, now, CFG)
    db.commit()
    return stats


def item(db, url):
    return db.item(fetch.canonical_url(url))


# published_at = min(feed date, first seen)

def test_future_feed_date_clamps_to_first_seen():
    # WP1 AC: feed date 2026-11-12T00:00Z, first seen 2026-11-11T03:30Z -> 03:30Z.
    assert fetch.resolve_published(dt("2026-11-12T00:00Z"), dt("2026-11-11T03:30Z")) == dt("2026-11-11T03:30Z")


def test_old_feed_date_is_kept():
    # WP1 AC: a 5-day-old item first seen now keeps its 5-day age.
    five_days_ago = NOW - timedelta(days=5)
    published = fetch.resolve_published(five_days_ago, NOW)
    assert NOW - published == timedelta(days=5)


def test_missing_feed_date_falls_back_to_first_seen():
    assert fetch.resolve_published(None, NOW) == NOW


def test_ingest_applies_timestamp_rules(db):
    db.update_source(NEWS.id, last_ok_at=iso(NOW - timedelta(hours=1)))  # not the first poll
    poll(db, NEWS, ok(NEWS, "feed_news.xml"), NOW)

    future = item(db, "https://example.com/2026/11/gilbert-tobin")
    assert future["feed_published_at"] == "2026-11-12T00:00:00Z"
    assert future["published_at"] == "2026-11-11T03:30:00Z"
    assert future["first_seen_at"] == "2026-11-11T03:30:00Z"
    assert future["status"] == "new"

    five = item(db, "https://example.com/2026/11/ipo-considered/")
    assert NOW - from_iso(five["published_at"]) == timedelta(days=5)
    assert five["status"] == "new"

    # Older than every tab window it could reach: ignored at first sight.
    assert item(db, "https://example.com/2026/10/old-post/")["status"] == "stale"

    undated = item(db, "https://example.com/2026/11/undated/")
    assert undated["feed_published_at"] is None and undated["published_at"] == iso(NOW)


def test_re_fetch_keeps_first_seen(db):
    db.update_source(NEWS.id, last_ok_at=iso(NOW - timedelta(hours=1)))
    poll(db, NEWS, ok(NEWS, "feed_news.xml"), NOW)
    db.update_source(NEWS.id, body_sha=None)  # force a re-ingest of the same body
    later = NOW + timedelta(hours=6)
    poll(db, NEWS, ok(NEWS, "feed_news.xml"), later)
    undated = item(db, "https://example.com/2026/11/undated/")
    assert undated["first_seen_at"] == iso(NOW) and undated["published_at"] == iso(NOW)


def test_reads_sources_allow_14_days():
    reads = Source(id="r", name="R", v1_name="R", url="https://r.example/feed", reads=True)
    assert not fetch.is_stale(NOW - timedelta(days=10), NOW, reads, CFG)
    assert fetch.is_stale(NOW - timedelta(days=10), NOW, NEWS, CFG)
    assert fetch.is_stale(NOW - timedelta(days=15), NOW, reads, CFG)


def test_first_run_marks_older_items_seen(db):
    stats = poll(db, NEWS, ok(NEWS, "feed_news.xml"), NOW)
    assert item(db, "https://example.com/2026/11/ipo-considered/")["status"] == "seen"      # 5 d > 36 h
    assert item(db, "https://example.com/2026/11/undated/")["status"] == "seen"            # age unknown
    assert item(db, "https://example.com/2026/11/activision/")["status"] == "new"          # 1.5 h old
    assert item(db, "https://example.com/2026/11/gilbert-tobin")["status"] == "new"
    assert stats.new == 2 and stats.seen == 2 and stats.stale == 1 and stats.dropped == 2


# Prefilter drops are stored and logged with their rule

def test_drops_are_logged_once_with_rule(db):
    poll(db, NEWS, ok(NEWS, "feed_news.xml"), NOW)
    assert item(db, "https://example.com/2026/11/blu-ray/")["drop_rule"] == "title_question"
    assert item(db, "https://example.com/deals/headphones/")["drop_rule"] == "url_deals"
    db.update_source(NEWS.id, body_sha=None)
    poll(db, NEWS, ok(NEWS, "feed_news.xml"), NOW + timedelta(minutes=3))
    counts = db.prefilter_counts(NOW - timedelta(hours=24))
    assert counts == {"title_question": 1, "url_deals": 1}
    h = health.build(db, SourceList([NEWS], []))
    assert h["prefilter"]["byRule"] == counts and h["prefilter"]["total"] == 2


def test_domain_blocklist(db):
    src = SourceList([NEWS], ["example.com"])
    fetch.record(db, NEWS, src, ok(NEWS, "feed_news.xml"), NOW, CFG)
    assert item(db, "https://example.com/2026/11/activision/")["drop_rule"] == "domain_blocklist"


def test_include_tags_filters_site_wide_feeds(db):
    semafor = Source(id="semafor", name="Semafor Tech", v1_name="Semafor Tech", url="https://semafor.example/rss",
                     include_tags=("Technology",))
    rss = (b'<rss version="2.0"><channel>'
           b'<item><title>Chip export rules tighten</title><link>https://semafor.example/a/chips</link>'
           b'<category>Technology</category><pubDate>Wed, 11 Nov 2026 03:00:00 GMT</pubDate></item>'
           b'<item><title>Senate vote delayed</title><link>https://semafor.example/a/senate</link>'
           b'<category>Politics</category><pubDate>Wed, 11 Nov 2026 03:00:00 GMT</pubDate></item>'
           b'</channel></rss>')
    db.update_source(semafor.id, last_ok_at=iso(NOW))
    fetch.record(db, semafor, SourceList([semafor], []), fetch.FetchResult(semafor.id, 200, rss), NOW, CFG)
    assert db.item("https://semafor.example/a/chips")["status"] == "new"
    assert db.item("https://semafor.example/a/senate")["drop_rule"] == "source_tags"


# Canonical URLs

@pytest.mark.parametrize("raw, canon", [
    ("https://Example.com/a/story?utm_source=rss&utm_medium=feed", "https://example.com/a/story"),
    ("https://example.com/a/story?id=7&fbclid=abc", "https://example.com/a/story?id=7"),
    ("https://example.com/a/story/amp/", "https://example.com/a/story/"),
    ("https://example.com/a/story/amp", "https://example.com/a/story"),
    ("https://example.com/a/story.amp.html", "https://example.com/a/story.html"),
    ("https://amp.example.com/a/story?amp=1#frag", "https://example.com/a/story"),
])
def test_canonical_url(raw, canon):
    assert fetch.canonical_url(raw) == canon


# Signal sources

def test_hn_sighting_then_news_source_takes_over(db):
    for s in (NEWS, HN):
        db.update_source(s.id, last_ok_at=iso(NOW - timedelta(hours=1)))
    poll(db, HN, ok(HN, "feed_hn.xml"), NOW - timedelta(minutes=30))
    row = item(db, "https://example.com/2026/11/activision/")
    assert row["status"] == "signal" and json.loads(row["extra"])["points"] == 412
    poll(db, NEWS, ok(NEWS, "feed_news.xml"), NOW)
    row = item(db, "https://example.com/2026/11/activision/")
    assert row["source_id"] == "example" and row["status"] == "new"
    # First seen is when HN listed it; the publisher's own date still drives published_at.
    assert row["first_seen_at"] == iso(NOW - timedelta(minutes=30))
    assert row["published_at"] == "2026-11-11T02:00:00Z"
    seen_by = {r["source_id"] for r in db.q("SELECT source_id FROM sightings WHERE url_canonical = ?",
                                            row["url_canonical"])}
    assert seen_by == {"hn", "example"}


def test_techmeme_records_linked_article(db):
    db.update_source(TECHMEME.id, last_ok_at=iso(NOW))
    poll(db, TECHMEME, ok(TECHMEME, "feed_techmeme.xml"), NOW)
    row = db.q("SELECT * FROM items WHERE source_id = 'techmeme'")[0]
    assert json.loads(row["extra"])["linked_url"] == "https://www.politico.com/news/2026/11/10/kalshi-01159140"


# Conditional GET bookkeeping

class FakeSession:
    def __init__(self, status, headers=None, content=b""):
        self.status, self.headers, self.content, self.sent = status, headers or {}, content, None

    def get(self, url, headers, timeout):
        self.sent = headers
        return type("R", (), {"status_code": self.status, "headers": self.headers, "content": self.content})()


def test_conditional_headers_sent_and_304_recorded(db):
    first = FakeSession(200, {"ETag": '"abc"', "Last-Modified": "Wed, 11 Nov 2026 03:00:00 GMT"},
                        body("feed_news.xml"))
    res = fetch.fetch(NEWS, db.source_state(NEWS.id), CFG, first)
    assert "If-None-Match" not in first.sent
    poll(db, NEWS, res, NOW)
    state = db.source_state(NEWS.id)
    assert state["etag"] == '"abc"' and state["next_poll_at"] == iso(NOW + timedelta(minutes=3))

    second = FakeSession(304)
    res = fetch.fetch(NEWS, db.source_state(NEWS.id), CFG, second)
    assert second.sent["If-None-Match"] == '"abc"'
    assert second.sent["If-Modified-Since"] == "Wed, 11 Nov 2026 03:00:00 GMT"
    stats = poll(db, NEWS, res, NOW + timedelta(minutes=3))
    state = db.source_state(NEWS.id)
    assert stats.new == 0 and state["last_status"] == 304 and state["etag"] == '"abc"'
    assert [r["status"] for r in db.q("SELECT status FROM polls ORDER BY at")] == [200, 304]


def test_failures_count_up_and_reset(db):
    for i in range(3):
        poll(db, NEWS, fetch.FetchResult(NEWS.id, status=503, error="HTTP 503"), NOW + timedelta(minutes=i))
    assert db.source_state(NEWS.id)["consecutive_failures"] == 3
    poll(db, NEWS, ok(NEWS, "feed_news.xml"), NOW + timedelta(minutes=5))
    assert db.source_state(NEWS.id)["consecutive_failures"] == 0


def test_empty_feed_is_a_failure(db):
    empty = fetch.FetchResult(NEWS.id, status=200, body=b"<rss><channel></channel></rss>")
    poll(db, NEWS, empty, NOW)
    state = db.source_state(NEWS.id)
    assert state["consecutive_failures"] == 1 and state["last_ok_at"] is None


# Tier-1 gating by check_sources

def test_failed_check_removes_tier1(db):
    assert sources_mod.effective_tier(NEWS, True) == 1
    assert sources_mod.effective_tier(NEWS, False) == 2
    assert sources_mod.effective_tier(NEWS, None) == 2   # never checked: not tier-1 yet
    assert sources_mod.effective_tier(HN, False) == "signal"
    db.update_source(NEWS.id, check_ok=0, check_detail="HTTP 403")
    h = health.build(db, SourceList([NEWS], []))
    assert h["sourceCheck"]["failed"] == ["example"] and h["sources"][0]["tier"] == 2


def test_sources_yaml_is_valid():
    srcs = sources_mod.load()
    ids = [s.id for s in srcs.sources]
    assert len(ids) == len(set(ids))
    names = {s.v1_name for s in srcs.sources}
    # The 1.2 FeedSource names the v1 output depends on (spec 2.13).
    assert {"TechCrunch", "The Verge", "BBC Technology", "New York Times", "CNBC Tech", "The Guardian"} <= names
    tier1 = {s.id for s in srcs.sources if s.tier == 1}
    assert {"techcrunch", "verge", "cnbc", "bbc", "nyt", "guardian", "ettech", "inc42"} <= tier1
    assert all(s.region == "in" for s in srcs.sources if s.id in ("ettech", "inc42", "yourstory", "entrackr"))
    assert not srcs.by_id()["nyt"].fetch_page and not srcs.by_id()["stratechery"].fetch_page


def test_three_minute_sources_are_due_on_the_third_tick(db):
    from worker import main
    slow = Source(id="slow", name="Slow", v1_name="Slow", url="https://slow.example/feed", poll="10m")
    srcs = SourceList([NEWS, slow], [])
    for s in srcs.sources:
        poll(db, s, fetch.FetchResult(s.id, status=304), NOW)
    # Ticks drift a few seconds late; the 3rd tick (≈ +178 s) must still poll the 3 min source.
    assert main.due_sources(db, srcs, NOW + timedelta(seconds=178)) == [NEWS]
    assert main.due_sources(db, srcs, NOW + timedelta(seconds=120)) == []
