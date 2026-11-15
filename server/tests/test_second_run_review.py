"""Regression tests for the review of the second VPS test run (2026-11-15, 77 cards against the live feed's 235).

Each test names the card, event or row it guards against. No network: the LLM is a scripted client.
"""
import json
from datetime import timedelta

import numpy as np
import pytest

from worker import cluster, degraded, embed, health, importance, llm, publish_v1, rank, validate
from worker.db import iso
from test_spice_and_truth import NOW, S, SEBI, SEBI_SRC, SEBI_TITLE, SRCS, FakeHTTP, _cand, _db, _item, _judge, _stored, out

CFG = {"llm": {"model": "w", "judge_model": "j"}, "embed": {"dims": 3}}


class Replies:
    """A chat client that answers from a list, in order, and records who was asked."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def chat(self, model, messages, **kw):
        self.calls.append((model, kw.get("kind"), messages[-1]["content"]))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


# Paywalled scoops: a thin item from an outlet that breaks news starts an event, and traction gets it a brief

FT_TITLE = ('Sources: Jensen Huang has discussed a comeback as Nvidia CEO with confidants, as allies form a "war room" '
            'to lobby for his return')
FT_TEXT = ("Tech supporters lobby the chipmaker's board to reinstate the former chief executive after he resigned "
           "following an affair with an alleged Chinese spy, people familiar with the matter said.")
BB_TITLE = "Huang Discusses Comeback as Nvidia Chief With Allies, FT Says"
BB_TEXT = ("Jensen Huang has discussed returning as chief executive officer of Nvidia Corp. with allies, the Financial "
           "Times reported, citing people familiar with the matter.")
BRIEF = {"spice": 9, "headline": "FT: Jensen Huang has discussed a comeback as Nvidia CEO, sources say",
         "headline_short": "Huang discusses Nvidia comeback",
         "s1": "Huang has discussed a return with confidants, people familiar with the matter told the Financial Times.",
         "s2": "Allies have formed a \"war room\" to lobby the chipmaker's board to reinstate him, the report says.",
         "notif_title": "Huang discusses Nvidia comeback",
         "notif_line": "Allies of Jensen Huang have formed a \"war room\" to lobby Nvidia's board for his return."}


def _written(db, items):
    """Item rows plus their writes rows (status and type), which the edition reads."""
    _stored(db, items)
    for it in items:
        db.conn.execute("INSERT INTO writes (url_canonical, status, written_at, type, entities) VALUES (?,?,?,?,?)",
                        (it.url, it.status, iso(NOW), it.type, json.dumps(it.entities)))


def _thin(url, title, text, source_id, imp=7, **kw):
    it = _item(url, kw.pop("vec", [1, 0, 0]), title, source_id=source_id, status="thin", imp=imp, **kw)
    it.text, it.feed_text, it.text_words, it.headline, it.summary = text, text, len(text.split()), None, None
    return it


def test_a_paywalled_scoop_starts_an_event_gets_a_brief_and_is_upgraded_by_an_open_report():
    """ev_553277a985: the FT's and Bloomberg's reports of Huang's comeback talks (Techmeme's lead story) were held
    no_text for 150 min, and nothing shipped until The Next Web wrote the story up."""
    db, calls = _db(), []
    ft = _thin("https://ft.test/comeback", FT_TITLE, FT_TEXT, "ft")
    blog = _thin("https://engadget.test/huang", "Huang may be back", "A short line.", "engadget", vec=[0, 1, 0])
    bb = _thin("https://bloomberg.test/comeback", BB_TITLE, BB_TEXT, "bloomberg", vec=[0.8, 0.6, 0],
               pub=NOW + timedelta(minutes=5))
    _written(db, [ft, blog, bb])
    store = cluster.Store(db, SRCS, S, NOW, _judge({BB_TITLE: "same"}, calls))
    eid, how = cluster.assign(store, ft)
    assert how == "thin_seed"                                           # the FT breaks news: its item may wait alone
    assert cluster.assign(store, blog) == (None, "thin_unmatched")      # a gadget blog's one-liner may not
    assert cluster.assign(store, bb) == (eid, "judge")
    ev = store.events[eid]
    cluster.refresh(store, ev, lambda s: True)
    assert ev.row["headline"] is None and ev.row["lead_url"] in (ft.url, bb.url) and ev.row["n_publishers"] == 2
    # No raw-title card for it: the edition waits for the brief.
    ctx = publish_v1.Ctx(db, {"cluster": {"t_join": 0.47}}, SRCS, NOW)
    assert publish_v1.event_candidate(ctx, db.one("SELECT * FROM events WHERE id = ?", eid)) == (None, "no_text")
    assert cluster.brief_due(store, ev)
    lines = cluster.brief_lines(store, ev)
    assert [name for name, _, _ in lines] == ["Financial Times", "Bloomberg"] and lines[0][2] == FT_TEXT

    client = Replies(BRIEF, {"entailed": "yes"})
    assert llm.brief_event(client, CFG, store, ev) is True
    assert [(m, k) for m, k, _ in client.calls] == [("w", "brief"), ("j", "entail")]
    assert "FINANCIAL TIMES: Sources: Jensen Huang" in client.calls[0][2] and "BLOOMBERG:" in client.calls[1][2]
    cluster.refresh(store, ev, lambda s: True)
    row = ev.row
    assert row["text_from"] == cluster.BRIEF and row["headline"].startswith("FT: Jensen Huang has discussed")
    assert row["summary"] == row["summary_short"] == f"{BRIEF['s1']} {BRIEF['s2']}" and row["spice"] == 9
    assert not cluster.brief_due(store, ev)
    c, why = publish_v1.event_candidate(ctx, db.one("SELECT * FROM events WHERE id = ?", eid))
    assert why is None and c.ai_text and publish_v1.notification(c)["title"] == "Huang discusses Nvidia comeback"

    # An open outlet reports it: its own validated card replaces the brief.
    tnw = _item("https://tnw.test/huang", [0.9, 0.4, 0], "Jensen Huang is discussing a return to Nvidia, FT reports",
                source_id="thenextweb", pub=NOW + timedelta(minutes=40))
    _written(db, [tnw])
    cluster.join(store, ev, tnw, 0.9, "judge")
    cluster.refresh(store, ev, lambda s: True)
    assert ev.row["lead_url"] == tnw.url and ev.row["text_from"] == tnw.url and ev.row["headline"] == tnw.title


def test_a_brief_needs_traction_material_and_a_named_outlet():
    db = _db()
    wsj = _thin("https://wsj.test/drones", "The Backyard Battle Over Amazon Delivery Drones", "A feature.", "wsj",
                imp=4)
    _stored(db, [wsj])
    store = cluster.Store(db, SRCS, S, NOW, _judge({}, []))
    eid, _ = cluster.assign(store, wsj)
    ev = store.events[eid]
    cluster.refresh(store, ev, lambda s: True)
    assert not cluster.brief_due(store, ev)             # one outlet, importance 4, 10 words: nothing to stand on
    # The same checks as any card, on the lines only: a brief that does not say whose reporting it is fails, and so
    # does one that states the discussed comeback as done.
    lines = f"Financial Times: {FT_TITLE}. {FT_TEXT}"

    def brief(**kw):
        o = llm.WriteOutput.model_validate({"is_news": True, "type": "news", "relevance": 3, "importance": 7,
                                            "region": "global", **BRIEF, **kw})
        return validate.check(o, lines, FT_TITLE, "", None, None, lines, brief=True, outlets=["Financial Times"])
    assert brief().ok
    v = brief(headline="Jensen Huang has discussed a comeback as Nvidia CEO with confidants",
              s1="Huang has discussed a return with confidants as allies lobby the board to reinstate him.")
    assert not v.ok and "sourcing_dropped" in v.reasons
    v = brief(s1="Too short.")
    assert not v.ok and v.reasons == ["too_short"]
    assert validate.names_outlet("WSJ: Huang built a backdoor route", ["Wall Street Journal"])
    assert not validate.names_outlet("Huang built a backdoor route", ["Wall Street Journal"])
    # A failed brief leaves the event without text, to be tried again later.
    ft = _thin("https://ft.test/comeback", FT_TITLE, FT_TEXT, "ft", vec=[0, 1, 0])
    _stored(db, [ft])
    fid, _ = cluster.assign(store, ft)
    fev = store.events[fid]
    cluster.refresh(store, fev, lambda s: True)
    assert cluster.brief_due(store, fev)
    assert llm.brief_event(Replies(BRIEF, {"entailed": "no", "unsupported": "x"}, BRIEF, {"entailed": "no"}),
                           CFG, store, fev) is False
    assert fev.row["headline"] is None and not cluster.brief_due(store, fev)        # not again for 20 min
    store.now = NOW + timedelta(minutes=21)
    assert cluster.brief_due(store, fev)


def test_a_validated_short_report_leads_before_a_longer_one_that_failed_and_a_listed_outlet_before_a_linked_site():
    db = _db()
    failed = _item("https://info.test/huang", [1, 0, 0], "Huang Weighs Nvidia Comeback", source_id="the_information",
                   status="failed")
    failed.headline = None
    short = _item("https://nypost.test/huang", [0.9, 0.4, 0], "Huang's allies push Nvidia board to re-appoint him",
                  source_id="nypost_business")
    short.text_words = 114
    _stored(db, [failed, short])
    store = cluster.Store(db, SRCS, S, NOW)
    eid, _ = cluster.create(store, failed)
    cluster.join(store, store.events[eid], short, 0.9, "judge")
    assert cluster.choose_lead(store, store.events[eid]) is short
    # litnews.ai (imported from a Hacker News link) led the DRAM price-fixing card over Tom's Hardware.
    web = _item("https://litnews.test/dram", [1, 0, 0], "Judge keeps DRAM price-fixing case", source_id="web")
    toms = _item("https://toms.test/dram", [0.9, 0.4, 0], "DRAM price-fixing case goes ahead", source_id="tomshardware")
    _stored(db, [web, toms])
    wid, _ = cluster.create(store, web)
    cluster.join(store, store.events[wid], toms, 0.9, "judge")
    assert cluster.choose_lead(store, store.events[wid]) is toms


# Signals survive promotion

def test_a_link_imported_as_an_article_that_cannot_be_written_still_gives_its_event_the_points():
    """https://www.spacex.com/launches/starship-flight-15: imported from Hacker News (1,573 points), held no_text,
    in no event; ev_7e59a67d80 "SpaceX catches Starship upper stage" (9 publishers) showed no points and was last
    in Top."""
    db, calls = _db(), []
    report = _item("https://gizmodo.test/starship", [1, 0, 0], "SpaceX catches Starship upper stage from orbit",
                   source_id="gizmodo", people=("Elon Musk",))
    _stored(db, [report])
    url, title = "https://www.spacex.com/launches/starship-flight-15", "SpaceX catches the Starship to end Flight 15"
    pts = {"points": 1573, "comments": 1116, "hn_url": "https://news.ycombinator.com/item?id=50500963"}
    db.insert_item({"url_canonical": url, "url": url, "source_id": "web", "title": title, "first_seen_at": NOW,
                    "published_at": NOW, "status": "held",
                    "extra": {"via": "hn", "publisher": "spacex.com", "signals": {"hn_100": pts, "hn": pts}}})
    sibling = _item("https://tnw.test/abort", [0.95, 0.31, 0], "SpaceX aborts booster catch as Starship reaches orbit",
                    source_id="thenextweb", people=("Gwynne Shotwell",))
    _stored(db, [sibling])
    store = cluster.Store(db, SRCS, S, NOW, _judge({(title, cluster.event_id(report.url)): "same"}, calls))
    eid, _ = cluster.create(store, report)
    sid, _ = cluster.create(store, sibling)         # nearer by title, and a different development
    orphans = [r for r in cluster.signal_items(db, SRCS, NOW) if r.get("orphan")]
    assert sorted(r["source_id"] for r in orphans) == ["hn", "hn_100"] and orphans[0]["vec"] is None

    class Embedder:
        model = "t"

        def embed(self, texts, input_type):
            assert texts == [title] and input_type == "query"
            return [embed.normalize([0.9, 0.3, 0])]
    assert cluster.embed_signals(db, Embedder(), SRCS, NOW) == 1
    assert db.one("SELECT kind FROM embeddings WHERE url_canonical = ?", cluster.ORPHAN_KEY + url)["kind"] == "query"
    assert cluster.attach_signals(store) == 2 and len(calls) == 1          # one ruling for both sightings
    assert set(calls[0][1]) == {eid, sid}                                   # the nearest events, in one call
    for e in (eid, sid):
        cluster.refresh(store, store.events[e], lambda s: True)
    assert json.loads(store.events[eid].row["hn"])["points"] == 1573 and store.events[sid].row["hn"] is None
    assert cluster.attach_signals(store) == 0                              # and it stays attached, once


# Ranking: a fuller edition, traction, sagas, shared essays, unvetted sites

TOP, READS = rank.TABS["top"], rank.TABS["reads"]


def test_tabs_hold_the_days_good_stories():
    assert {t.id: t.max for t in rank.TABS.values()} == {"top": 60, "ai": 40, "startups": 40, "india": 30,
                                                         "dev": 30, "reads": 25}
    cs = [_cand(f"ev_{i:02}", importance=6, spice=7, n=2, age_h=1 + i * 0.3) for i in range(70)]
    assert len(rank.assemble(cs, TOP, NOW)) == 60


def test_a_well_covered_story_lasts_more_than_a_day():
    """ev_7e59a67d80 (the Starship catch, 9 publishers, importance 8) was rank 29 of 30 at 27 h."""
    star = _cand("star", importance=8, spice=8, n=9, age_h=27)
    solo = _cand("solo", importance=8, spice=8, n=1, age_h=27)
    plain = rank.score(8, 9, False, 0, 1.0, 27, TOP.tau_h, 8)
    assert rank.cand_score(star, TOP, NOW) > 1.3 * plain
    assert rank.cand_score(solo, TOP, NOW) == pytest.approx(rank.score(8, 1, False, 0, 1.0, 27, TOP.tau_h, 8))
    assert rank.staying_power(star) == pytest.approx(1 + 0.25 * np.log2(9))
    # Fresh, the tail changes nothing.
    fresh = _cand("fresh", importance=8, spice=8, n=9, age_h=2)
    assert rank.cand_score(fresh, TOP, NOW) == pytest.approx(rank.score(8, 9, False, 0, 1.0, 2, TOP.tau_h, 8))
    # Top's window: 48 h for a story five outlets ran.
    wide = _cand("wide", importance=7, n=5, age_h=40)
    assert rank.eligible(wide, TOP, NOW) is None
    assert rank.eligible(_cand("narrow", importance=7, n=2, age_h=40), TOP, NOW) == "window"


def test_a_saga_shows_its_real_developments_past_the_head_of_a_tab():
    """Storyline ev_4c525e5e54 (Nvidia): "Megaspeed opens internal probe" (8 publishers, Techmeme), the petition
    and the investors' letter were cut by the cap of 3 while spice-4 single-source items filled Top."""
    saga = [_cand(f"saga{i}", importance=7, spice=8, n=4, techmeme=True, age_h=1 + i * 0.2) for i in range(6)]
    weak = _cand("saga_weak", importance=6, spice=5, n=1, age_h=1.1)
    for c in saga + [weak]:
        c.storyline = "nvidia"
    others = [_cand(f"o{i:02}", importance=6, spice=6, n=1, age_h=2 + i * 0.1) for i in range(20)]
    ranked = [c.id for c, _ in rank.assemble(saga + [weak] + others, TOP, NOW)]
    head = ranked[:rank.STORYLINE_HEAD]
    assert sum(i.startswith("saga") for i in head) == rank.STORYLINE_CAP == 3
    assert all(f"saga{i}" in ranked for i in range(6))          # each well-covered development is shown
    # A plain single-source angle is what the cap cuts: last when the tab has room left, out when it is full.
    assert ranked[-1] == "saga_weak"
    full = rank.Tab(**{**TOP.__dict__, "max": 20})
    assert "saga_weak" not in [c.id for c, _ in rank.assemble(saga + [weak] + others, full, NOW)]
    # A major development counts even from one outlet ("Huang agrees to testify", Bloomberg, importance 7).
    major = _cand("saga_major", importance=7, spice=9, n=1, age_h=1.2)
    major.storyline = "nvidia"
    assert "saga_major" in [c.id for c, _ in rank.assemble(saga[:4] + [major] + others, TOP, NOW)]
    many = [_cand(f"m{i:02}", importance=7, spice=8, n=4, techmeme=True, age_h=1 + i * 0.1) for i in range(12)]
    for c in many:
        c.storyline = "x"
    event = _cand("m_event", importance=9, spice=10, n=3, techmeme=True, age_h=27)
    event.storyline = "x"
    short = rank.Tab(**{**TOP.__dict__, "max": 30})
    more = others + [_cand(f"p{i:02}", importance=6, spice=6, n=1, age_h=10 + i * 0.1) for i in range(10)]
    got = [c.id for c, _ in rank.assemble(many + [event] + more, short, NOW)]
    # 8, or a sixth of a long tab (10 of Top's 60): the third review found the eight freshest took every place.
    assert sum(i.startswith("m") and i != "m_event" for i in got) == rank.storyline_limit(short) == 8
    assert rank.storyline_limit(TOP) == 10 and rank.STORYLINE_MAX == 8
    assert "m_event" in got                 # the saga's main event is never the one the cap cuts


def test_developments_the_judge_ruled_apart_are_both_shown_in_a_tab():
    """ev_5f7e2eab7e "Musk says he will sign petition for Huang's Nvidia return" (4 publishers) was dropped from
    Top as a duplicate of "Huang discusses possible return as Nvidia chief": the edition-wide guard honoured the
    judge's ruling and the parent/follow-up link, the tab's own guard did not."""
    d = rank.Dedup(alone=0.65, t=0.5)
    comeback = _cand("ev_comeback", importance=7, spice=9, n=5, vec=[1, 0], age_h=1)
    petition = _cand("ev_petition", importance=6, spice=8, n=4, vec=[0.8, 0.6], age_h=3)
    child = _cand("ev_child", importance=6, spice=8, n=2, vec=[0.9, 0.44], age_h=2)
    for c in (comeback, petition, child):
        c.keys = frozenset([("p", "huang")])
    child.data = {"parent_id": "ev_comeback"}
    assert d.duplicate(comeback, petition) and d.duplicate(comeback, child)
    got = [c.id for c, _ in rank.assemble([comeback, petition, child], TOP, NOW, dup_threshold=d)]
    assert got == ["ev_comeback", "ev_child"]                   # a follow-up sits next to its parent
    apart = {("ev_comeback", "ev_petition"), ("ev_child", "ev_petition")}
    got = [c.id for c, _ in rank.assemble([comeback, petition, child], TOP, NOW, dup_threshold=d,
                                          ruled_apart=lambda a, b: tuple(sorted((a, b))) in apart)]
    assert sorted(got) == ["ev_child", "ev_comeback", "ev_petition"]


def test_traction_opens_a_tab_like_spice_does():
    """Techmeme's headlines of 2026-11-15 scored importance 4-5 ("Huang tantrums", the AI Music Awards) were in
    no tab."""
    assert rank.eligible(_cand(importance=4, spice=5, techmeme=True), TOP, NOW) is None
    assert rank.eligible(_cand(importance=4, spice=5, hn=350), TOP, NOW) is None
    assert rank.eligible(_cand(importance=4, spice=5, n=4), TOP, NOW) is None
    assert rank.eligible(_cand(importance=4, spice=5, n=2), TOP, NOW) == "importance"
    assert rank.eligible(_cand(importance=3, spice=5, techmeme=True), TOP, NOW) == "importance"
    assert rank.eligible(_cand(importance=4, spice=5, techmeme=True, age_h=40), TOP, NOW) == "window"


def test_a_widely_shared_essay_reaches_reads_and_top_whoever_published_it():
    """"All AI-generated code should be Open Source" (HN 896, a personal blog) and "The MacBook Pro's OLED PWM
    problem" (HN 309) were written as analysis cards and shown in no tab."""
    essay = _cand("essay", importance=6, type="opinion", hn=896, title="Doe: All AI-generated code should be open")
    essay.unvetted, essay.is_news = True, False         # an essay reports nothing: the writer says is_news false
    assert rank.eligible(essay, READS, NOW) is None and rank.eligible(essay, TOP, NOW) is None
    quiet = _cand("quiet", importance=6, type="opinion", hn=40)
    assert rank.eligible(quiet, READS, NOW) == "not_reads" and rank.eligible(quiet, TOP, NOW) == "type"
    assert rank.eligible(_cand(importance=4, type="analysis", hn=309), TOP, NOW) == "type"      # Top needs a 5
    listed = _cand("listed", importance=5, type="analysis", lead_reads=True)
    assert rank.eligible(listed, READS, NOW) is None
    # ev_01c6c81e09 "Analysis: Nvidia begins CEO search after Jensen Huang resigns" (3 publishers) was in no tab.
    assert rank.eligible(_cand(importance=7, type="analysis", n=3), TOP, NOW) is None
    assert rank.eligible(_cand(importance=7, type="analysis", n=2), TOP, NOW) == "type"


def test_a_site_nobody_chose_needs_a_second_outlet_for_top_and_alerts():
    """litnews.ai, crissyfield.de and upstartsmedia.com took Top ranks on a Hacker News link alone."""
    solo = _cand("solo", importance=7, spice=8, hn=448)
    solo.unvetted = True
    assert rank.eligible(solo, TOP, NOW) == "unvetted" and rank.eligible(solo, rank.TABS["ai"], NOW) is None
    assert not rank.notifiable(solo)
    backed = _cand("backed", importance=7, spice=8, hn=448, n=2)
    backed.unvetted = True
    assert rank.eligible(backed, TOP, NOW) is None and rank.notifiable(backed)


def test_reddit_and_github_trending_count_as_traction_with_capped_multipliers():
    base = rank.coverage(2)
    assert rank.coverage(2, on_reddit=True) == pytest.approx(base * 1.1)
    assert rank.coverage(2, github_stars=800) == pytest.approx(base * 1.1)          # capped
    assert rank.coverage(2, github_stars=200) == pytest.approx(base * 1.05)
    c = _cand(n=2)
    c.on_reddit = True
    assert rank.traction(c) == pytest.approx(base * 1.1) and rank.widely_shared(c)


# Push alerts

def _alert(c, title="Short title here", line="A hook to tap."):
    c.data = {"notif_title": title, "notif_line": line, "headline": "A headline for the card",
              "lead": {"url_canonical": f"https://x.test/{c.id}"}}
    return c


def test_alerts_rank_the_tap_worthy_and_the_breaking_first():
    """notifications.json of 20:17: "Applied Digital seeks guarantees" (spice 6) was 4th and "TSMC plans $500
    million startup fund" (spice 5) 9th, ahead of "ULA loses Vulcan and 40 Amazon Leo satellites" (spice 8, one
    publisher, an hour old) at 12th; an 8.6 h analysis card was a candidate."""
    ula = _alert(_cand("ula", importance=6, spice=8, n=1, age_h=1.0))
    applied = _alert(_cand("applied", importance=6, spice=6, n=2, techmeme=True, age_h=4.9))
    tsmc = _alert(_cand("tsmc", importance=6, spice=5, n=2, techmeme=True, age_h=11.2))
    exam = _alert(_cand("exam", importance=5, spice=7, n=3, age_h=11.5))
    essay = _alert(_cand("essay", importance=6, spice=8, n=1, type="analysis", age_h=8.6))
    major = _alert(_cand("major", importance=8, spice=8, n=3, type="analysis", age_h=3))
    got = publish_v1.notification_candidates({"top": [(c, 1.0) for c in (applied, tsmc, exam, essay, ula, major)]},
                                             NOW)
    assert [n["eventId"] for n in got] == ["major", "ula", "exam"]
    # Spice counts twice, and an hour-old story is not outranked by an older one with the same traction.
    assert rank.notification_score(ula) == pytest.approx(0.8 ** 2 * 0.6)
    old = _cand("old", importance=6, spice=8, n=1, age_h=11)
    assert rank.notification_score(ula, NOW) > 2 * rank.notification_score(old, NOW)


def test_an_alert_title_says_what_happened_not_only_who():
    """One card's alert was titled just "Gary Marcus": the publisher's name, used when the writer gave no title."""
    c = _alert(_cand("m", importance=6, spice=8, type="opinion"), title="")
    c.lead_publisher = "Gary Marcus"
    c.data["headline"] = "Marcus: FAA ground stop revives warnings about the risks of AI in aviation"
    assert publish_v1.alert_title(c) is None and publish_v1.notification(c) is None
    c.data["notif_title"] = "Gary Marcus"
    assert publish_v1.notification(c) is None
    c.data["notif_title"] = "Marcus on the FAA ground stop"
    assert publish_v1.notification(c)["title"] == "Marcus on the FAA ground stop"
    # The fallbacks are the card's own checked text: the headline, or its opening clause.
    c.data.update(notif_title="", headline="ULA loses Vulcan rocket, 40 Amazon Leo satellites after liftoff anomaly")
    assert publish_v1.alert_title(c) == "ULA loses Vulcan rocket"
    # ... but never a clause whose dropped tail carried the attribution or the hedge.
    c.data["headline"] = "Nvidia sold banned chips to China through a broker, lawsuit by investors claims"
    assert publish_v1.alert_title(c) is None
    c.data["headline"] = "ULA loses Vulcan rocket, 40 Amazon Leo satellites after liftoff anomaly"
    c.data["people"] = {"jensen huang"}
    c.data["notif_title"] = "Jensen Huang"
    assert publish_v1.alert_title(c) == "ULA loses Vulcan rocket"
    # The writer's stage gives a name-only title up for the short headline.
    o = out(SEBI, notif_title="Sebi", headline_short="Sebi to issue AI rules")
    validate.select(o)
    assert o.notif_title == "Sebi to issue AI rules"
    assert validate.name_only("Tuhin Kanta Pandey", ["Tuhin Kanta Pandey"]) and not validate.name_only("Sebi AI rules")
    assert "Never only a name" in llm.PROMPT and "flat \"" in llm.PROMPT


# Thin text: two sentences, and no scene-setting

def test_a_card_from_a_short_text_is_two_sentences():
    """"Madonna manager's VC firm has $7B stake in Anthropic" (80 feed words): its third sentence was "Framed art
    by rock legends John Lennon and David Bowie hung on the wall"."""
    o = out(SEBI)
    v = validate.check(o, SEBI_SRC, SEBI_TITLE, "YourStory", None, None, SEBI_SRC, short_card=True)
    assert v.ok and v.full == v.short == f"{SEBI['s1']} {SEBI['s2']}" and o.s3 == ""
    assert llm.SHORT_TEXT_WORDS == 150 and len(SEBI_SRC.split()) < 150
    assert "leave s3 and s3_alt empty" in llm.PROMPT and "scene-setting" in llm.PROMPT
    from worker import entail
    assert "10. Is scene-setting or colour" in entail.PROMPT


# Importance

def test_the_prompt_scores_a_sagas_developments_as_news():
    assert "A running story" in llm.IMPORTANCE and "usually 4-6" not in llm.IMPORTANCE
    assert "urges Orbitly's board to pick an outsider as its next CEO\" = 8" in llm.IMPORTANCE
    for live in ("Bessent", "Huang", "Megaspeed"):
        assert live not in llm.IMPORTANCE
    m = [importance.Member(f"u{i}", f"s{i}", f"p{i}", i < 2, "news", 5) for i in range(9)]
    assert importance.compute(m, "news", ["bigtech"], on_techmeme=True).value == 7     # the Bessent card was a 5


# Screening

def test_the_screening_prompt_keeps_tech_people_in_culture_moments():
    assert "SNL gathers all the tech tycoons" in llm.TRIAGE_PROMPT and "culture moment" in llm.TRIAGE_PROMPT


# A hub outage: the backup chain, the failover count, cards that wait for the primary

def _chain(hub_status=200):
    hub, nv = FakeHTTP(hub_status, '{"from": "hub"}'), FakeHTTP(200, '{"from": "nvidia"}')
    sleep = lambda s: None  # noqa: E731
    clients = {"hub": llm.Client("https://hub.test/v1", "hk", llm.TokenBucket(600, 60.0), hub, sleep=sleep,
                                 params={"reasoning_effort": "low"}),
               "nvidia": llm.Client("https://nv.test/v1", "nk", llm.TokenBucket(600, 60.0), nv, sleep=sleep)}
    r = llm.Router(clients, "nvidia", backup=["hub:claude", "nvidia:nemotron"], model_params={"hub:claude": {}})
    return r, hub, nv


def test_backups_are_tried_in_order_and_every_failover_is_counted():
    r, hub, nv = _chain(500)
    msg = [{"role": "user", "content": "x"}]
    assert json.loads(r.chat("hub:gpt", msg, tries=1)) == {"from": "nvidia"} and r.answered_by_backup()
    # The second hub model was tried (only the first model failed, not the endpoint), without the hub's own params.
    assert [b["model"] for _, b in hub.bodies] == ["gpt", "claude"] and "reasoning_effort" not in hub.bodies[1][1]
    events = [(kind, model, outcome) for kind, model, outcome, *_ in r.drain()]
    assert ("write", "hub:gpt", "failover") in events and ("write", "nvidia:nemotron", "ok") in events
    ok, _, _ = _chain(200)
    assert json.loads(ok.chat("hub:gpt", msg)) == {"from": "hub"} and not ok.answered_by_backup()
    assert all(e[2] != "failover" for e in ok.drain())
    # An open circuit (the endpoint timed out twice) sends calls straight to the other provider, across ticks.
    r, hub, nv = _chain(200)
    r.clients["hub"]._timed_out()
    r.clients["hub"]._timed_out()
    r.reset_breaker()
    assert r.provider_down("hub:gpt") and not r.provider_down("nvidia:nemotron")
    assert json.loads(r.chat("hub:gpt", msg)) == {"from": "nvidia"} and hub.bodies == []


def _log(db, at, kind, outcome, n=1):
    for _ in range(n):
        db.log_write(at, None if kind != "item" else "u", kind, "m", outcome)


def test_an_outage_answered_by_a_failing_backup_is_reported_and_degrades_the_edition():
    """Hub-outage simulations of 2026-11-15: every call went to Nemotron and logged "ok", 0-1 of 4 cards passed,
    llm_outage said None and the items went to hold and then dropped, with no alert."""
    db = _db()
    at = NOW - timedelta(minutes=3)
    _log(db, at, "write", "ok", 40)
    assert degraded.backup_state(db, NOW)["active"] is False and degraded.llm_outage(db, NOW) is None
    _log(db, at, "write", "failover", 30)       # 30 of the 40 answers were a backup's
    st = degraded.backup_state(db, NOW)
    assert st["failovers"] == 30 and st["primaryOk"] == 10 and st["active"] and not st["failing"]
    assert degraded.llm_outage(db, NOW) is None                      # the backup's cards pass: no degraded edition
    _log(db, at, "item", "ok", 2)
    _log(db, at, "item", "backup_failed", 6)
    assert degraded.backup_state(db, NOW)["failing"] and degraded.llm_outage(db, NOW) == "backup"
    assert degraded.outage_since(db, "backup", NOW) == NOW - degraded.STORM_WINDOW
    assert degraded.llm_outage(db, NOW + timedelta(minutes=20)) is None


def test_a_card_that_failed_on_the_backup_waits_for_the_primary_and_keeps_its_retries():
    db = _db()
    url = "https://x.test/a"
    db.insert_item({"url_canonical": url, "url": url, "source_id": "techcrunch", "title": "T", "first_seen_at": NOW,
                    "published_at": NOW, "status": "new"})
    for i in range(4):
        assert db.hold(url, "backup_failed", NOW + timedelta(minutes=15 * i)) == "held"
    row = db.item(url)
    assert row["status"] == "held" and row["attempts"] == 0 and row["hold_reason"] == "backup_failed"
    assert row["retry_at"] == iso(NOW + timedelta(minutes=60))


def test_a_story_already_being_shared_is_retried_within_minutes():
    """The FT and Bloomberg comeback items (Techmeme's lead) were held with retries at 21:52 and 21:56, two and a
    half hours after they arrived; The Information's failed card for 30 min."""
    db = _db()
    for u in ("hot", "cold", "hotfail"):
        db.insert_item({"url_canonical": u, "url": u, "source_id": "ft", "title": "T", "first_seen_at": NOW,
                        "published_at": NOW, "status": "new"})
    db.hold("hot", "no_text", NOW, hot=True)
    db.hold("cold", "no_text", NOW)
    db.hold("hotfail", "entail_fail", NOW, hot=True)
    assert db.item("hot")["retry_at"] == iso(NOW + timedelta(minutes=5))
    assert db.item("cold")["retry_at"] == iso(NOW + timedelta(minutes=30))
    assert db.item("hotfail")["retry_at"] == iso(NOW + timedelta(minutes=5))
    for _ in range(4):
        last = db.hold("hot", "no_text", NOW, hot=True)
    assert last == "dropped"


# Embeddings down: title words stand in

def test_items_still_become_cards_when_the_embedding_endpoint_is_down():
    """19:51-20:00 UTC: ten "embedding failed" ticks, during which 30 written items could not cluster."""
    db = _db()
    first = _item("https://cnbc.test/probe", [1, 0, 0], "Megaspeed opens internal probe into chip supply reports",
                  source_id="cnbc")
    _stored(db, [first])
    store = cluster.Store(db, SRCS, S, NOW)
    eid, _ = cluster.create(store, first)
    for u, title in (("https://ap.test/probe", "Megaspeed opens an internal probe after chip supply reports"),
                     ("https://bbc.test/other", "Italy fines Meta 805m euros over Android photo access")):
        db.insert_item({"url_canonical": u, "url": u, "source_id": "bbc", "title": title, "first_seen_at": NOW,
                        "published_at": NOW, "status": "written"})
        db.conn.execute("INSERT INTO writes (url_canonical, status, written_at, type) VALUES (?,?,?,?)",
                        (u, "ok", iso(NOW), "news"))
    assert cluster.embed_fallback(db, CFG, NOW) == 2 and cluster.embed_fallback(db, CFG, NOW) == 0
    rows = db.q("SELECT model, vec FROM embeddings WHERE model = ?", cluster.FALLBACK_MODEL)
    assert len(rows) == 2 and not embed.from_blob(rows[0]["vec"]).any()
    same = _item("https://ap.test/probe", [0, 0, 0], "Megaspeed opens an internal probe after chip supply reports",
                 source_id="bbc")
    same.vec = np.zeros(3, dtype=np.float32)
    other = _item("https://bbc.test/other", [0, 0, 0], "Italy fines Meta 805m euros over Android photo access",
                  source_id="bbc")
    other.vec = np.zeros(3, dtype=np.float32)
    assert cluster.assign(store, same) == (eid, "title_fallback")
    oid, how = cluster.assign(store, other)
    assert how == "new_no_vector" and oid != eid
    # When the endpoint answers again the zero vectors are replaced.
    todo = db.q("SELECT e.url_canonical FROM embeddings e WHERE e.model = ?", cluster.FALLBACK_MODEL)
    assert len(todo) == 2

    class Embedder:
        model = "real"

        def embed(self, texts, input_type):
            return [embed.normalize([1, 1, 0]) for _ in texts]
    assert cluster.embed_backlog(db, {"embed": {"lead_words": 120}}, Embedder(), NOW) == 2
    assert db.one("SELECT COUNT(*) n FROM embeddings WHERE model = ?", cluster.FALLBACK_MODEL)["n"] == 0


# Fast-lane lag

def test_live_lag_keeps_the_slow_samples_and_splits_by_source():
    """freshness.live counted only stories first seen within 20 min of publication, which threw out exactly the
    slow ones (a feed that lists a story 45 min late)."""
    db = _db()
    for m in range(0, 180, 3):
        db.log_poll(NOW - timedelta(minutes=m), "fortune", 304, 0, 0, 100, None)
    rows = [("ev_slow", 50, 5, "fortune"), ("ev_fast", 8, 6, "techcrunch"), ("ev_fast2", 6, 4, "techcrunch"),
            ("ev_before", 600, 170, "fortune")]             # published before the worker was polling: backlog
    for eid, pub_min, seen_min, sid in rows:
        url = f"https://x.test/{eid}"
        db.insert_item({"url_canonical": url, "url": url, "source_id": sid, "title": eid, "first_seen_at": NOW,
                        "published_at": NOW, "status": "written"})
        db.conn.execute("INSERT INTO events (id, anchor_url, lead_url, created_at, updated_at, published_at)"
                        " VALUES (?,?,?,?,?,?)", (eid, url, url, iso(NOW), iso(NOW), iso(NOW)))
        db.conn.execute("INSERT INTO shipped VALUES (?,?,?,?,?)",
                        (eid, iso(NOW - timedelta(minutes=pub_min)), iso(NOW), iso(NOW),
                         iso(NOW - timedelta(minutes=seen_min))))
    live = health.freshness(db, NOW - timedelta(hours=24))["live"]
    assert live["events"] == 3 and live["publisherToAppMin"]["p95"] == 50.0
    assert live["bySource"]["fortune"] == {"events": 1, "feedLagMin": 45.0, "publisherToAppMin": 50.0}
    assert live["bySource"]["techcrunch"]["events"] == 2 and live["feedLagMin"]["p95"] == 45.0


def test_a_backup_model_gets_one_more_feedback_retry_and_the_same_checks():
    """Replay with the hub taken away (tools/eval_cards.py --without hub): half of the backup's failed cards had
    fixed the first problem and tripped a second on their one retry."""
    from worker import text as text_mod

    class Client(Replies):
        backup = False

        def answered_by_backup(self):
            return self.backup

    item = {"url": "https://x.test/sebi", "title": SEBI_TITLE, "published_at": iso(NOW)}
    text = text_mod.ArticleText(SEBI_SRC, "page", [])
    bad = {**SEBI, "s1": "Too short."}
    primary = Client(bad, bad, bad)
    res = llm.write_item(primary, CFG, item, "YourStory", text)
    assert res.status == "failed" and len(primary.calls) == 2 and not res.by_backup
    backup = Client(bad, bad, SEBI, {"entailed": "yes"})
    backup.backup = True
    res = llm.write_item(backup, CFG, item, "YourStory", text)
    assert res.status == "ok" and res.by_backup and [k for _, k, _ in backup.calls] == ["write"] * 3 + ["entail"]
    still = Client(bad, bad, bad, bad)
    still.backup = True
    res = llm.write_item(still, CFG, item, "YourStory", text)
    assert res.status == "failed" and res.by_backup and len(still.calls) == 3
