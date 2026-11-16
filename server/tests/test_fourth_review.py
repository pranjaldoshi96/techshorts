"""Fixes from the review of the first live hours (2026-11-16): Techmeme's semicolon headlines, the article a
Techmeme entry links to, security and big-company stories in the wrong tabs, cards without a photo, and a card
whose headline credits another member than its link.
"""
import json
from datetime import timedelta

import pytest

pytest.importorskip("feedparser")

from worker import cluster, fetch, prefilter, publish_v1, rank  # noqa: E402
from test_second_run_review import _written  # noqa: E402
from test_spice_and_truth import NOW, S, SRCS, _cand, _db, _item  # noqa: E402

CFG = {"fetch": {"max_age_days": 7, "max_age_days_reads": 14, "first_run_skip_hours": 36, "max_items_per_feed": 100}}
CTX_CFG = {"cluster": {"t_join": 0.47}}
IPO = ("Sources: Anthropic's IPO is ~7x oversubscribed; Anthropic and some of its backers are seeking to raise up to "
       "$90.4B")
IPO_URL = "https://www.bloomberg.com/news/features/2026-11-15/anthropic-ipo"


def _techmeme(title, link, pub="Bloomberg", home="https://www.bloomberg.com/", thumb=True, n=18):
    """One entry of Techmeme's feed. With a thumbnail the description starts with the article's link; without one,
    with the outlet's front page."""
    first = f'<A HREF="{link}"><IMG VSPACE="4" HSPACE="4" BORDER="0" ALIGN="RIGHT" SRC="http://www.techmeme.com/' \
            f'261115/i{n}.jpg"></A>' if thumb else ""
    desc = (f'{first}<P><A HREF="https://www.techmeme.com/261115/p{n}#a261115p{n}" TITLE="Techmeme permalink">'
            f'<IMG WIDTH=11 HEIGHT=12 SRC="http://www.techmeme.com/img/pml.png"></A> Jane Doe / '
            f'<A HREF="{home}">{pub}</A>:<BR><SPAN STYLE="font-size:1.3em;"><B><A HREF="{link}">{title}</A></B></SPAN>'
            f'&nbsp; &mdash;&nbsp; The first words of the report &hellip; </P>')
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>Techmeme</title><item>'
            f'<title>{title} (Jane Doe/{pub})</title><link>https://www.techmeme.com/261115/p{n}#a261115p{n}</link>'
            f'<description><![CDATA[{desc}]]></description><pubDate>Sun, 15 Nov 2026 11:00:00 -0500</pubDate>'
            f'<guid>https://www.techmeme.com/261115/p{n}#a261115p{n}</guid></item></channel></rss>').encode()


def _tm():
    return SRCS.by_id()["techmeme"]


def test_a_techmeme_headline_with_a_semicolon_is_one_story():
    """prefilter_log 2026-11-15T23:13Z: the Anthropic IPO pick was dropped as title_digest, and no other item
    carried the story."""
    assert prefilter.check(IPO, IPO_URL) == "title_digest"          # the rule itself stands for news feeds
    assert prefilter.check(IPO, IPO_URL, digest=False) is None
    assert prefilter.check("Best Black Friday deals; save $200 on a MacBook", IPO_URL, digest=False)  # others still run
    db = _db()
    entries, _ = fetch.parse_entries(_techmeme(IPO, IPO_URL), _tm(), 100)
    stats = fetch.ingest(db, _tm(), SRCS, entries, NOW, False, CFG)
    assert stats.dropped == 0
    row = db.item(entries[0].url_canonical)
    assert row["status"] == "signal" and json.loads(row["extra"])["linked_url"] == IPO_URL
    assert fetch.promote_signals(db, SRCS, {}, NOW) == 1
    art = db.item(IPO_URL)
    assert art["status"] == "new" and art["source_id"] == "bloomberg" and art["title"] == IPO


def test_a_techmeme_entry_links_its_headline_not_the_outlets_front_page():
    """Live edition 2026-11-16T05:29Z: 'Alibaba says it filtered Qwen4-Max training data for safety' opened
    http://www.scmp.com/ (an entry with no thumbnail starts with the outlet's link)."""
    art = "https://www.scmp.com/tech/big-tech/article/3374540/alibaba-says-new-flagship"
    title = "Alibaba says it filtered Qwen4-Max's training data to remove offensive cyber capabilities"
    for thumb in (True, False):
        entries, _ = fetch.parse_entries(
            _techmeme(title, art, "South China Morning Post", "http://www.scmp.com/", thumb), _tm(), 100)
        assert entries[0].extra["linked_url"] == art
    assert fetch.techmeme_article('<a href="https://www.techmeme.com/x">p</a> <a href="http://www.scmp.com/">S</a>') \
        is None


def test_a_front_page_is_never_imported_and_a_row_that_recorded_one_heals():
    db = _db()
    art = "https://www.scmp.com/tech/big-tech/article/3374540/alibaba-says-new-flagship"
    title = "Alibaba says it filtered Qwen4-Max's training data to remove offensive cyber capabilities"
    entries, _ = fetch.parse_entries(_techmeme(title, art, "South China Morning Post", "http://www.scmp.com/", False),
                                     _tm(), 100)
    tm = entries[0].url_canonical
    # The row as the old code stored it, already offered to promote_signals.
    db.insert_item({"url_canonical": tm, "url": tm, "source_id": "techmeme", "title": entries[0].title,
                    "feed_text": "x", "extra": {"linked_url": "http://www.scmp.com/", "promoted": "item"},
                    "first_seen_at": NOW, "published_at": NOW, "feed_published_at": NOW, "status": "signal"})
    fetch.ingest(db, _tm(), SRCS, entries, NOW + timedelta(minutes=3), False, CFG)
    extra = json.loads(db.item(tm)["extra"])
    assert extra == {"linked_url": art}
    assert fetch.promote_signals(db, SRCS, {}, NOW + timedelta(minutes=3)) == 1 and db.item(art)["status"] == "new"
    # A signal whose only link is a front page is marked and never becomes an item.
    home = "https://www.techmeme.com/261115/p30"
    db.insert_item({"url_canonical": home, "url": home, "source_id": "techmeme", "title": "A pick (X/Y)",
                    "feed_text": "x", "extra": {"linked_url": "https://artificialanalysis.ai/"},
                    "first_seen_at": NOW, "published_at": NOW, "feed_published_at": NOW, "status": "signal"})
    assert fetch.promote_signals(db, SRCS, {}, NOW + timedelta(minutes=3)) == 0
    assert json.loads(db.item(home)["extra"])["promoted"] == "not_article"
    assert db.item("https://artificialanalysis.ai/") is None


def _event(db, items, lead=None):
    _written(db, items)
    store = cluster.Store(db, SRCS, S, NOW)
    eid, _ = cluster.create(store, items[0])
    ev = store.events[eid]
    for it in items[1:]:
        cluster.join(store, ev, it, 0.9, "judge")
    cluster.refresh(store, ev, lambda s: True)
    return eid, ev


def test_a_card_whose_link_is_a_front_page_is_not_shipped():
    db = _db()
    eid, _ = _event(db, [_item("http://www.scmp.com/", [1, 0, 0], "Alibaba says it filtered training data")])
    cands, held = publish_v1.load_candidates(publish_v1.Ctx(db, CTX_CFG, SRCS, NOW))
    assert not cands and held == {"not_article": 1}


def test_the_card_opens_the_report_its_headline_credits():
    """Top rank 6 on 2026-11-16: 'Reuters: SK Hynix plans ~$1.05B to support Korean suppliers' with source
    Bloomberg and a link to Bloomberg's article about a testing hub, which does not give that figure."""
    db = _db()
    bb = _item("https://www.bloomberg.com/news/articles/sk-hynix-testing-hub", [1, 0, 0],
               "SK Hynix Plans Chip Testing Hub to Boost Smaller Korean Players", source_id="bloomberg")
    rt = _item("https://www.reuters.com/world/asia-pacific/sk-hynix-spend-1-billion", [0.9, 0.4, 0],
               "SK Hynix to spend $1 billion over five years on suppliers", source_id="reuters",
               pub=NOW + timedelta(minutes=5))
    eid, ev = _event(db, [bb, rt])
    db.conn.execute("UPDATE events SET lead_url = ?, headline = ? WHERE id = ?",
                    (bb.url, "Reuters: SK Hynix plans ~$1.05B to support Korean suppliers", eid))
    ctx = publish_v1.Ctx(db, CTX_CFG, SRCS, NOW)
    c = next(x for x in publish_v1.load_candidates(ctx)[0] if x.id == eid)
    a = publish_v1.article(c, "Top", 0, None)
    assert a["url"] == rt.url and a["source"] == "Reuters" and a["alsoCoveredBy"] == ["Bloomberg"]
    # A headline crediting the lead, nobody, or an outlet that is not a member leaves the card on its lead.
    for headline in ("Bloomberg: SK Hynix plans chip testing hub", "SK Hynix plans chip testing hub",
                     "AFR: SK Hynix plans chip testing hub"):
        db.conn.execute("UPDATE events SET headline = ? WHERE id = ?", (headline, eid))
        c = next(x for x in publish_v1.load_candidates(ctx)[0] if x.id == eid)
        a = publish_v1.article(c, "Top", 0, None)
        assert a["url"] == bb.url and a["source"] == "Bloomberg" and a["alsoCoveredBy"] == ["Reuters"]


def _with(c, summary=""):
    c.data = {"summary": summary}
    return c


def test_security_and_big_company_stories_are_filed_where_they_belong():
    ai, dev, st, top = (rank.TABS[t] for t in ("ai", "dev", "startups", "top"))
    # ev_d8f5a3c861: type news, topics [security, ai, consumer], was filed under AI.
    ublock = _with(_cand("ublock", topics=("security", "ai", "consumer"), hn=211,
                         title="uBlock Origin fixes one-click flaw enabling cross-site code"),
                   "The element picker could be driven by a website to run script on another site.")
    assert rank.eligible(ublock, ai, NOW) == "topic" and rank.eligible(ublock, dev, NOW) is None
    # A story whose main topic is not AI stays in the AI tab when its card is about AI.
    cohere = _with(_cand("cohere", topics=("security", "ai"), title="Cohere says Qwen model tried uploading file"))
    assert rank.eligible(cohere, ai, NOW) is None
    # Plain security news with no developer term in its headline is not Dev (SpaceX Starship was tagged dev_tools).
    starship = _with(_cand("starship", topics=("security", "dev_tools", "bigtech"), title="Starship test delayed"))
    assert rank.eligible(starship, dev, NOW) == "topic"
    # ev_a2ad9f1b1e (ransomnews.com, HN 232): a site nobody vetted may not take a Top rank on 232 points, and it
    # had no other tab, so it shipped nowhere. It is developer news.
    bitly = _with(_cand("bitly", type="security_incident", topics=("security",), hn=232, spice=8,
                        title="Bitly confirms hijackers repurposed old links in tweets"))
    bitly.unvetted = True
    assert rank.eligible(bitly, top, NOW) == "unvetted" and rank.eligible(bitly, dev, NOW) is None
    # ev_7cc69d3a0c: topics [ai, bigtech, startups_funding], type news, was filed under Startups.
    kress = _with(_cand("kress", importance=7, topics=("ai", "bigtech", "startups_funding"),
                        title="WSJ: Nvidia interim CEO Colette Kress expected to rein in deals"))
    assert rank.eligible(kress, st, NOW) == "topic" and rank.eligible(kress, ai, NOW) is None
    rnd = _with(_cand("round", type="funding", topics=("ai", "bigtech", "startups_funding"), title="X raises $2B"))
    assert rank.eligible(rnd, st, NOW) is None
    startup = _with(_cand("startup", topics=("startups_funding", "bigtech"), title="A startup hires Google's CFO"))
    assert rank.eligible(startup, st, NOW) is None


def test_a_card_without_a_photo_does_not_open_a_tab_while_another_can():
    """Top rank 0 on 2026-11-16 was a drawn grey cover (qwen.ai has no usable picture; Bloomberg refuses the bot)."""
    top = rank.TABS["top"]
    big = _cand("big", importance=9, spice=7, n=2)
    big.no_picture = True
    other = _cand("other", importance=6, spice=6)
    ranked = [c.id for c, _ in rank.assemble([big, other], top, NOW)]
    assert ranked == ["other", "big"]                    # it yields the first place only, and is not dropped
    assert [c.id for c, _ in rank.assemble([big], top, NOW)] == ["big"]
    plain = _cand("plain", importance=9, spice=7, n=2)
    assert rank.cand_score(big, top, NOW) == pytest.approx(rank.NO_PICTURE * rank.cand_score(plain, top, NOW))


# The old publisher and the outside freshness check (shell)

SERVER = __import__("pathlib").Path(__file__).resolve().parent.parent


def test_the_old_publisher_refuses_to_run_without_the_rollback_marker(tmp_path):
    """A hand-run publish.sh on the old box would have force-replaced the worker's edition branch."""
    import subprocess
    r = subprocess.run(["bash", str(SERVER / "publish.sh")], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TECHSHORTS_STATE": str(tmp_path / "st")})
    assert r.returncode == 3 and "not publishing" in r.stderr
    assert not (tmp_path / "st" / "site" / ".git").exists() and not (tmp_path / "st" / "lock").exists()


def test_the_outside_check_tries_an_unreadable_edition_once_more(tmp_path):
    """One of raw.githubusercontent.com's addresses was unreachable on 2026-11-16: one failed fetch is not an
    outage, and must not alert."""
    import shutil
    import subprocess
    import sys
    import threading
    import time
    for name in ("freshness_cron.sh", "check_freshness.py", "alert.py"):
        shutil.copy(SERVER / "deploy" / name, tmp_path / name)
    edition = tmp_path / "edition.json"

    def appear():
        time.sleep(0.4)
        edition.write_text(json.dumps({"publishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
    threading.Thread(target=appear).start()
    env = {"PATH": f"{__import__('os').path.dirname(sys.executable)}:/usr/bin:/bin", "HOME": str(tmp_path),
           "FRESHNESS_RETRY_S": "1"}
    r = subprocess.run(["bash", str(tmp_path / "freshness_cron.sh"), str(edition)], env=env, capture_output=True)
    assert r.returncode == 0
    log = (tmp_path / "freshness.log").read_text()
    assert "status=0" in log and "alert" not in log and not (tmp_path / "alerted").exists()
    # Still unreadable after the retry: reported as such (no topic here, so nothing is sent).
    r = subprocess.run(["bash", str(tmp_path / "freshness_cron.sh"), str(tmp_path / "missing.json")],
                       env={**env, "FRESHNESS_RETRY_S": "0"}, capture_output=True)
    assert r.returncode == 2 and "status=2" in (tmp_path / "freshness.log").read_text().splitlines()[-1]


def test_the_repair_tool_restores_dropped_picks_and_closes_front_page_cards():
    from tools import repair_signals
    db = _db()
    tm = "https://www.techmeme.com/261115/p18"
    db.insert_item({"url_canonical": tm, "url": tm, "source_id": "techmeme", "title": IPO + " (Bloomberg)",
                    "feed_text": "x", "extra": {"linked_url": IPO_URL}, "first_seen_at": NOW, "published_at": NOW,
                    "feed_published_at": NOW, "status": "dropped", "drop_rule": "title_digest"})
    tm2 = "https://www.techmeme.com/261116/p1"
    db.insert_item({"url_canonical": tm2, "url": tm2, "source_id": "techmeme", "title": "Alibaba says (SCMP)",
                    "feed_text": "x", "extra": {"linked_url": "http://www.scmp.com/", "promoted": "item"},
                    "first_seen_at": NOW, "published_at": NOW, "feed_published_at": NOW, "status": "signal"})
    home = _item("http://www.scmp.com/", [1, 0, 0], "Alibaba says it filtered training data", source_id="web")
    eid, _ = _event(db, [home])
    db.conn.execute("UPDATE items SET extra = ? WHERE url_canonical = ?",
                    (json.dumps({"via": "techmeme", "publisher": "scmp.com"}), home.url))
    done = {what for what, _, _ in repair_signals.repair(db, SRCS, NOW)}
    assert done == {"signal_again", "offered_again", "front_page_dropped", "event_closed"}
    assert db.item(tm)["status"] == "signal" and db.item(tm)["drop_rule"] is None
    assert "promoted" not in json.loads(db.item(tm2)["extra"])
    assert db.item(home.url)["drop_rule"] == "not_article"
    assert db.one("SELECT status FROM events WHERE id = ?", eid)["status"] == "void"
    assert fetch.promote_signals(db, SRCS, {}, NOW) == 1 and db.item(IPO_URL)["status"] == "new"
    assert repair_signals.repair(db, SRCS, NOW) == []
