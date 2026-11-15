"""Regression tests for the third review of the VPS test run (revision 222e760, the 22:10 edition of 2026-11-15,
115 cards). Each test names the card, event or row it guards against. No network: the LLM is a scripted client.
"""
import json
from datetime import timedelta

import pytest

from worker import cluster, embed, fetch, llm, newsdate, publish_v1, rank, text, validate  # noqa: F401
from worker.db import from_iso, iso
from test_second_run_review import CFG as BRIEF_CFG, Replies, _thin, _written
from test_validate import CFG as WRITE_CFG, DAYLIGHT, DAYLIGHT_SRC, FakeClient, _db_with, _sources
from test_spice_and_truth import (NOW, S, SEBI, SEBI_SRC, SEBI_TITLE, SRCS, _cand, _db, _item, _judge,  # noqa: F401
                                  _stored, check, out)


# 11. The tense rule: any capitalization, and plan -> done drift on the same verb wherever the plan is stated

@pytest.mark.parametrize("title", ["Sebi To Issue AI Usage Guidelines For Capital Markets",
                                   "SEBI TO ISSUE AI USAGE GUIDELINES", "Sebi to Issue AI usage guidelines"])
def test_a_title_case_plan_binds_the_headline(title):
    assert validate.future_marker(title, validate.PLAN_MARK) == "to issue"
    assert validate.tense_problem("Sebi issues AI usage guidelines", title) == "to issue"
    v = check(SEBI, title=title, headline="Sebi issues AI usage guidelines for capital markets")
    assert not v.ok and "tense_drift" in v.reasons


def test_a_prospective_word_elsewhere_does_not_excuse_the_done_verb():
    # "ahead of" counted as keeping the plan, so "Sebi issues ... ahead of" passed the title rule.
    v = check(SEBI, headline="Sebi issues AI guidelines ahead of market overhaul",
              src=SEBI_SRC + " The rules come ahead of a wider market overhaul.")
    assert not v.ok and "tense_drift" in v.reasons
    assert "issues" in next(f for f in v.feedback if "still to come" in f)


IPO_TITLE = "Anthropic launches roadshow for its initial public offering"
IPO_SRC = ("Anthropic began its roadshow on Monday and is expected to price its shares at $70 to $78 each, people "
           "familiar with the matter said. The offering will be priced on Wednesday evening and the stock is set "
           "to list on Nasdaq on Thursday, according to the timetable.")


@pytest.mark.parametrize("line,drift", [
    ("Anthropic IPO priced at $70 to $78 a share", "priced"),             # "priced" before pricing
    ("Anthropic prices IPO at $70 to $78", "prices"),
    ("Anthropic lists on Nasdaq after roadshow", "lists"),
    ("Anthropic expected to price IPO at $70 to $78", None),
    ("Anthropic IPO to be priced at $70 to $78 on Wednesday", None),
    ("Anthropic IPO pricing expected at $70 to $78", None),
    ("Anthropic began its roadshow on Monday", None),
    ("If priced at the top, Anthropic would raise the most", None),
])
def test_a_plan_in_the_text_must_not_be_stated_as_done(line, drift):
    got = validate.done_before_due(line, IPO_TITLE, IPO_SRC)
    assert (got[0].lower() if got else None) == drift


def test_the_source_reporting_it_done_clears_the_verb():
    src = IPO_SRC + " The company priced a smaller debt offering in June."
    assert validate.done_before_due("Anthropic priced a debt offering in June", IPO_TITLE, src) is None
    # The alert and the summary are held to the same rule as the headline: it names a verb, so it does not
    # trip on an alert that states another, present fact (the old wide rule did and was taken off them).
    v = check(SEBI, notif_line="Sebi issued guidelines that keep critical market decisions under human oversight.")
    assert not v.ok and v.reasons == ["tense_drift"]
    assert check(SEBI).ok


@pytest.mark.parametrize("later,verb", [
    ("Elon Musk signs petition to reinstate Jensen Huang as Nvidia CEO", "sign"),
    ("Elon Musk, other tech execs sign petition calling on Nvidia to reinstate Jensen Huang", "sign"),
    ("Jensen Huang return petition crosses 1.4 lakh signatures as big tech leaders join campaign", None),
    ("Musk says he is ready to sign the Huang petition", None),
    ("Musk has not signed the petition yet", None),
])
def test_a_later_title_that_reports_the_plan_done(later, verb):
    planned = "Musk backs petition calling for Jensen Huang’s return, says he will sign it"
    assert validate.realized_by(planned, later) == verb


# 1. Superseded cards, and a card that follows the newest material fact its event holds

LAUNCH = "ULA Vulcan Rocket Launches With Its First Amazon Leo Payload"
FAILURE = "ULA suffers first total mission failure with loss of Vulcan rocket carrying 40 Amazon Leo satellites"
CTX_CFG = {"cluster": {"t_join": 0.47}}


def _edition_ids(db, now=NOW):
    cands, held = publish_v1.load_candidates(publish_v1.Ctx(db, CTX_CFG, SRCS, now))
    return {c.id for c in cands}, held


def test_the_rocket_lost_supersedes_the_rocket_launched():
    """ev_f6041b806d "ULA launches Vulcan with 40 Amazon internet satellites" (Top rank 21) beside ev_0054e860a0
    "ULA reportedly sees first total failure, losing 40 Amazon satellites" (rank 2)."""
    assert cluster.overtakes(LAUNCH, FAILURE)
    assert not cluster.overtakes(FAILURE, LAUNCH)
    assert not cluster.overtakes(LAUNCH, "Amazon stock falls as rivals fail to match Leo pricing")     # another story
    assert not cluster.overtakes(LAUNCH, "ULA Vulcan rocket launches second Amazon Leo payload")
    db, calls = _db(), []
    launch = _item("https://mint.test/vulcan", [1, 0, 0], LAUNCH, source_id="livemint_companies", people=())
    failure = _item("https://sfn.test/failure", [0.8, 0.6, 0], FAILURE, source_id="spaceflightnow", people=(),
                    pub=NOW + timedelta(minutes=45))
    _written(db, [launch, failure])
    # The judge rules a plain "followup" (as it did on the VPS); the reversal in the title does the rest.
    store = cluster.Store(db, SRCS, S, NOW + timedelta(hours=1), _judge({FAILURE: "followup"}, calls))
    old, _ = cluster.assign(store, launch)
    new, how = cluster.assign(store, failure)
    assert how == "supersedes_judge" and store.events[new].row["parent_id"] == old
    assert store.events[old].row["superseded_by"] == new
    for ev in store.events.values():
        cluster.refresh(store, ev, lambda s: True)
    ids, held = _edition_ids(db, NOW + timedelta(hours=1))
    assert ids == {new} and held == {"superseded": 1}
    # While the newer event has no written card the older one stays: a story is never left with no card at all.
    db.conn.execute("UPDATE events SET headline = NULL, summary = NULL WHERE id = ?", (new,))
    db.conn.execute("UPDATE writes SET status = 'thin' WHERE url_canonical = ?", (failure.url,))
    ids, held = _edition_ids(db, NOW + timedelta(hours=1))
    assert ids == {old}


def test_the_judge_can_rule_that_a_report_supersedes_a_card():
    """The FAA outage: "linked to routine maintenance on a copper line" stayed beside two later cards that gave
    other causes. No reversal word marks that; the judge's "supersedes" does."""
    assert "supersedes" in llm.RELATIONS and '"supersedes"' in llm.JUDGE_PROMPT
    client = Replies({"relations": {"ev_a": "supersedes", "ev_b": "nonsense"}})
    got = llm.judge_events(client, {"llm": {"model": "w"}}, "t", "x", [("ev_a", "h", "t"), ("ev_b", "h", "t")])
    assert got == {"ev_a": "supersedes", "ev_b": "different"}
    db, calls = _db(), []
    first = _item("https://axios.test/faa", [1, 0, 0], "FAA back at full operations, outage tied to copper line work",
                  source_id="axios", people=("Sean Duffy",))
    router = _item("https://cnbc.test/router", [0.8, 0.6, 0], "DOT Sec. Duffy blames router for nationwide ground stop",
                   source_id="cnbc", people=("Sean Duffy",), pub=NOW + timedelta(hours=20))
    column = _item("https://verge.test/col", [0.6, 0.5, 0.6], "The FAA's week from hell", source_id="verge",
                   people=("Sean Duffy",), type="analysis", pub=NOW + timedelta(hours=21))
    _written(db, [first, router, column])
    rulings = {router.title: "supersedes", column.title: "supersedes"}
    store = cluster.Store(db, SRCS, S, NOW + timedelta(hours=22), _judge(rulings, calls))
    old, _ = cluster.assign(store, first)
    new, how = cluster.assign(store, router)
    assert how == "supersedes_judge" and store.events[old].row["superseded_by"] == new
    # Commentary never supersedes a report, whatever the judge said: it is a follow-up.
    col, how = cluster.assign(store, column)
    assert how == "followup_judge" and store.events[new].row.get("superseded_by") is None
    # A merge carries the pointer, so the old card stays out when the newer event is merged into another.
    other = _item("https://bbc.test/router", [0, 1, 0], "Duffy points to router in FAA outage", source_id="bbc",
                  people=("Sean Duffy",), pub=NOW + timedelta(hours=19))
    _written(db, [other])
    oid, _ = cluster.create(store, other)
    cluster.merge(store, store.events[new], store.events[oid])
    assert store.events[old].row["superseded_by"] == oid
    assert db.one("SELECT superseded_by FROM events WHERE id = ?", old)["superseded_by"] == oid


WILL_SIGN = "Musk backs petition calling for Jensen Huang’s return, says he will sign it"
SIGNED = "Elon Musk signs petition to reinstate Jensen Huang as Nvidia CEO"


def test_the_card_follows_the_member_that_reports_the_plan_done():
    """ev_5f7e2eab7e: "Musk says he will sign petition for Huang's Nvidia return" led a 4-publisher event in which
    TechCrunch and Tom's Hardware reported that he had signed."""
    db = _db()
    will = _item("https://et.test/will", [1, 0, 0], WILL_SIGN, source_id="et_funding", pub=NOW - timedelta(hours=30))
    signed = _item("https://toms.test/signed", [0.9, 0.4, 0], SIGNED, source_id="tomshardware",
                   pub=NOW - timedelta(hours=6))
    count = _item("https://et.test/count", [0.9, 0.3, 0.1], "Huang return petition crosses 1.4 lakh signatures",
                  source_id="ettech", pub=NOW - timedelta(hours=7))
    _written(db, [will, signed, count])
    store = cluster.Store(db, SRCS, S, NOW)
    eid, _ = cluster.create(store, will)
    ev = store.events[eid]
    cluster.join(store, ev, count, 0.9, "judge")
    cluster.refresh(store, ev, lambda s: True)
    assert ev.row["lead_url"] == will.url and ev.row["headline"] == WILL_SIGN      # nothing newer contradicts it yet
    cluster.join(store, ev, signed, 0.9, "judge")
    assert cluster.overtaken(ev.members) == {will.url}
    cluster.refresh(store, ev, lambda s: True)
    # The earliest report stays the anchor (joins are judged against it); the card is the newest fact.
    assert ev.row["anchor_url"] == will.url and ev.row["lead_url"] == signed.url and ev.row["headline"] == SIGNED
    assert signed.material_update and "material_update" in ev.row["rewrite_reason"]
    assert ev.row["last_material_update_at"] is not None
    # If the member that reports it done has no validated card, the older card stays rather than none.
    signed.status = "failed"
    assert cluster.choose_lead(store, ev).url in (will.url, count.url)


def test_the_rewrite_states_the_newest_account_and_is_checked_against_the_newest_title():
    db = _db()
    text = ("Elon Musk said on X that he will sign a petition calling on Nvidia to reinstate Jensen Huang as chief "
            "executive. The petition was started by Nvidia employees after Huang resigned on Thursday. ") * 3
    done = ("Elon Musk signed the petition calling on Nvidia to reinstate Jensen Huang on Saturday, joining other "
            "tech executives. The petition has passed 140,000 signatures, its organisers said. ") * 3
    will = _item("https://et.test/will", [1, 0, 0], WILL_SIGN, source_id="et_funding", pub=NOW - timedelta(hours=30))
    signed = _item("https://toms.test/signed", [0.9, 0.4, 0], SIGNED, source_id="tomshardware",
                   pub=NOW - timedelta(hours=6))
    will.text, signed.text = text, done
    _written(db, [will, signed])
    store = cluster.Store(db, SRCS, S, NOW)
    eid, _ = cluster.create(store, will)
    ev = store.events[eid]
    cluster.join(store, ev, signed, 0.9, "judge")
    ev.row["lead_url"] = will.url           # as on the VPS: the first report led
    signed.material_update = False          # even when nothing flagged it: it is hours newer than the lead
    card = {"importance": 6, "spice": 8, "headline": "Musk signs petition to reinstate Jensen Huang at Nvidia",
            "s1": "Elon Musk signed the petition on Saturday, joining other tech executives who want Huang back "
                  "as chief executive.",
            "s2": "The petition was started by Nvidia employees after Huang resigned on Thursday, its organisers "
                  "said.",
            "s3": "Its organisers said the petition calling on Nvidia to reinstate Huang keeps gathering names "
                  "from across the industry.",
            "notif_title": "Musk signs Huang petition",
            "notif_line": "Musk joined other tech executives on the Huang petition on Saturday."}
    client = Replies(card, {"entailed": "yes"})
    assert llm.rewrite_event(client, {"llm": {"model": "w", "judge_model": "j"}}, store, ev) is True
    prompt = client.calls[0][2]
    assert "REPORT 2 is the newest report" in prompt and f"Published: {cluster.iso(signed.published_at)}" in prompt
    assert "newest report's account" in " ".join(llm.REWRITE_PROMPT.split())
    assert ev.row["headline"] == card["headline"]
    # A report hours newer than the lead, published since the card was written, queues that rewrite.
    ev.row.update(rewritten_at=None, rewrite_reason=None)
    signed.status = "failed"                # so the first report still leads
    cluster.refresh(store, ev, lambda s: True)
    assert ev.row["lead_url"] == will.url and "newer_report" in ev.row["rewrite_reason"]


# 2. The storyline cap keeps by spice x importance x traction; routine single-source news is not Top material

TOP = rank.TABS["top"]


def _saga(cards):
    for c in cards:
        c.storyline = "nvidia"
    return cards


def test_the_cap_cuts_the_weakest_cards_of_a_saga_not_the_oldest():
    """The third review's Top gave the Nvidia saga's eight places to its eight freshest developments and cut
    ev_b3a83c99ba "Huang told Nvidia exec 'I hope you die,' Post reports" (spice 8, Techmeme, 29 h) and the
    resignation report itself (spice 9, 4 publishers, 31 h), while drier fresh cards stayed."""
    fresh = _saga([_cand(f"dry{i:02}", importance=7, spice=6, n=1, age_h=2 + i) for i in range(10)])
    spicy = _saga([_cand("hope_you_die", importance=6, spice=8, n=2, techmeme=True, age_h=29),
                   _cand("resigns", importance=7, spice=9, n=4, age_h=31)])
    others = [_cand(f"o{i:02}", importance=6, spice=6, n=1, age_h=1 + i * 0.5) for i in range(55)]
    stats = {}
    got = [c.id for c, _ in rank.assemble(fresh + spicy + others, TOP, NOW, stats=stats)]
    assert len(got) == TOP.max and {"hope_you_die", "resigns"} <= set(got)
    kept = [i for i in got if i.startswith("dry")]
    assert len(kept) + 2 == rank.storyline_limit(TOP) == 10
    # What the cap cut are the saga's least valuable cards by spice x importance x traction (with recency): the
    # two oldest of the dry single-source ones, not whichever came last in the tab's order.
    cut = rank.storyline_cut(fresh + spicy, TOP, NOW)
    assert cut == {"dry08", "dry09"}
    worth = sorted(fresh + spicy, key=lambda c: -rank.storyline_worth(c, NOW))
    assert {c.id for c in worth[-2:]} == cut
    # Commentary on the saga counts half against a development with the same numbers.
    news, column = _cand("n", spice=8, n=2), _cand("c", spice=8, n=2, type="analysis")
    assert rank.storyline_worth(column, NOW) == pytest.approx(rank.storyline_worth(news, NOW) / 2)
    # A storyline within the cap of 3 is never cut, and a short tab keeps the old limit of 8.
    assert rank.storyline_cut(fresh[:3], TOP, NOW) == set()
    assert rank.storyline_limit(rank.TABS["reads"]) == rank.STORYLINE_MAX


def test_cut_cards_take_the_ranks_nobody_else_wants():
    saga = _saga([_cand(f"s{i:02}", importance=6, spice=5, n=1, age_h=1 + i) for i in range(6)])
    others = [_cand(f"o{i:02}", importance=6, spice=6, n=1, age_h=2 + i) for i in range(12)]
    got = [c.id for c, _ in rank.assemble(saga + others, TOP, NOW)]
    assert len(got) == 18 and [i for i in got[-3:]] == ["s03", "s04", "s05"]       # after everything else
    assert sum(i.startswith("s") for i in got[:rank.STORYLINE_HEAD]) == rank.STORYLINE_CAP


@pytest.mark.parametrize("kw,why", [
    (dict(importance=6, spice=4, n=1), "routine"),              # "EaseMyTrip cuts Q2 FY27 net loss 98.4%"
    (dict(importance=6, spice=3, n=1), "routine"),
    (dict(importance=6, spice=5, n=1), None),
    (dict(importance=7, spice=4, n=1), None),                   # major news needs no hook
    (dict(importance=6, spice=4, n=2), None),                   # a second outlet is traction
    (dict(importance=6, spice=4, n=1, techmeme=True), None),
    (dict(importance=6, spice=4, n=1, hn=150), None),
])
def test_routine_single_source_news_is_not_top_material(kw, why):
    c = _cand("ev", topics=("startups_funding", "india"), type="funding", **kw)
    assert rank.eligible(c, TOP, NOW) == why
    assert rank.eligible(c, rank.TABS["startups"], NOW) is None        # it keeps its place in its own tab


# 3. A card's date and its alert eligibility come from when the news happened

SUNDAY = from_iso("2026-11-15T21:55:51Z")
HOMEVIEW = ("Apple began selling its HomeView Friday (Nov. 13), describing it in a press release as an artificial "
            "intelligence-powered product that is designed to be the center of the home. HomeView features a "
            "7-inch display and can be placed on a tabletop or mounted on a wall, according to the release.")
MERCOR = ("Two Indian-origin entrepreneurs had their fortunes double with their artificial intelligence startup "
          "Mercor. The company raised $750 million in a fresh funding round, taking its valuation to $20 billion, "
          "as per multiple reports. Mercor's latest funding round, announced on Tuesday, was backed by investors "
          "including Benchmark and General Catalyst.")


def test_a_late_write_up_is_dated_by_the_news_not_the_article():
    """ev_e54189102d "Apple starts selling HomeView" (PYMNTS, Sunday 21:55 on sales that began on Friday) shipped
    as 15 minutes old and was the 8th push-alert candidate; ev_c94ca99595 "Mercor raises $750 million" (Mint,
    Sunday, on a round announced on Tuesday) the 20th."""
    title = "Apple Puts Siri AI at the Center of the Home With HomeView"
    assert newsdate.news_at("", SUNDAY, title, HOMEVIEW) == from_iso("2026-11-13T12:00:00Z")
    # The writer's date is used when the text itself carries that day, and only then.
    assert newsdate.news_at("2026-11-10", SUNDAY, "Mercor founders", MERCOR) == from_iso("2026-11-10T12:00:00Z")
    assert newsdate.news_at("2026-11-09", SUNDAY, "Mercor founders", MERCOR) is None         # no Monday in the text
    assert newsdate.news_at("", SUNDAY, "Mercor founders", MERCOR) is None                    # not in the lede
    assert newsdate.news_at("not a date", SUNDAY, "Mercor founders", MERCOR) is None
    assert "news_date" in llm.PROMPT and llm.WriteOutput.model_validate(
        {**DAYLIGHT, "news_date": "Tuesday"}).news_date == ""


@pytest.mark.parametrize("lede", [
    "Jensen Huang, who resigned Thursday, has discussed a comeback with allies, the Financial Times reported.",
    "Days after Nvidia said Thursday that Huang had resigned, its board met again.",        # background
    "Apple will begin selling its HomeView on Friday, the company said.",                     # still to come
    "Apple began selling its HomeView Saturday, describing it as the center of the home.",    # yesterday
    "Apple began selling its HomeView on Sunday.",                                            # today
    "Apple began selling its HomeView Friday (Nov. 6), a week before the holidays.",          # not last Friday
    "Apple began selling its HomeView this week.",
])
def test_the_article_time_stands_when_the_lede_does_not_date_the_news(lede):
    assert newsdate.news_at("", SUNDAY, "t", lede) is None


def test_the_page_states_when_it_was_published():
    page = '<html><head><meta property="article:published_time" content="2026-11-12T08:30:00+05:30"></head></html>'
    assert text.page_published(page) == from_iso("2026-11-12T03:00:00Z")
    ld = '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-11-12T03:00:00Z"}</script>'
    assert text.page_published(ld) == from_iso("2026-11-12T03:00:00Z")
    # A bare date would read as midnight and age a story by hours; no date is better than a wrong one.
    assert text.page_published('<meta property="article:published_time" content="2026-11-12">') is None
    assert text.page_published("<html></html>") is None


def _late_item(**kw):
    return {"url": "https://example.com/d", "url_canonical": "https://example.com/d", "source_id": "verge",
            "title": "Daylight raises $80M", "feed_text": "", "published_at": "2026-11-13T03:00:00Z", **kw}


def test_an_undated_feed_item_takes_the_page_date_and_the_write_stores_the_news_date():
    # Friday's article on a round "the startup said on Tuesday": the item keeps its time, the news is Tuesday's.
    db, now = _db_with([_late_item()])
    fetch = lambda i, s: text.choose([DAYLIGHT_SRC], "")      # noqa: E731
    assert llm.write_pending(db, _sources(), WRITE_CFG, FakeClient([DAYLIGHT]), fetch_text=fetch, now=now)["written"]
    assert db.write_row("https://example.com/d")["news_at"] == "2026-11-10T12:00:00Z"
    assert db.item("https://example.com/d")["published_at"] == "2026-11-13T03:00:00Z"

    # No date in the feed: the item was dated by when it was fetched. The page's own date replaces that.
    def dated(i, s, at="2026-11-08T09:00:00Z"):
        t = text.choose([DAYLIGHT_SRC], "")
        t.page_published_at = from_iso(at)
        return t
    db, now = _db_with([_late_item(published_at="2026-11-11T04:00:00Z")])           # = first seen
    llm.write_pending(db, _sources(), WRITE_CFG, FakeClient([DAYLIGHT]), fetch_text=dated, now=now)
    assert db.item("https://example.com/d")["published_at"] == "2026-11-08T09:00:00Z"
    # A feed's own date within the hour stands; a later page date never moves an item forward.
    row = {"url_canonical": "u", "published_at": "2026-11-11T04:00:00Z", "feed_published_at": "2026-11-11T04:00:00Z"}
    for at in ("2026-11-11T03:30:00Z", "2026-11-11T09:00:00Z", "2026-09-01T00:00:00Z"):
        assert llm.backdate(db, row, dated(None, None, at)) == from_iso("2026-11-11T04:00:00Z")
    assert llm.backdate(db, row, dated(None, None, "2026-11-10T22:00:00Z")) == from_iso("2026-11-10T22:00:00Z")


def test_old_news_is_shown_with_its_own_date_and_is_never_an_alert():
    db = _db()
    late = _item("https://pymnts.test/homeview", [1, 0, 0], "Apple starts selling HomeView, its Siri AI home display",
                 source_id="pymnts", pub=NOW - timedelta(minutes=15), spice=8, imp=7, people=())
    fresh = _item("https://verge.test/glasses", [0, 1, 0], "Apple reportedly plans smart glasses debut next year",
                  source_id="verge", pub=NOW - timedelta(minutes=20), spice=8, imp=7, people=())
    late.news_at = NOW - timedelta(hours=14)
    for it in (late, fresh):
        it.notif_title, it.notif_line = "Apple news lands", "A line about the story that says what happened."
    _written(db, [late, fresh])
    db.conn.execute("UPDATE items SET feed_published_at = published_at")
    store = cluster.Store(db, SRCS, S, NOW)
    for it in (late, fresh):
        eid, _ = cluster.create(store, it)
        cluster.refresh(store, store.events[eid], lambda s: True)
    old, new = store.events[cluster.event_id(late.url)], store.events[cluster.event_id(fresh.url)]
    assert old.row["published_at"] == iso(late.news_at) and new.row["published_at"] == iso(fresh.published_at)
    ctx = publish_v1.Ctx(db, CTX_CFG, SRCS, NOW)
    edition, report, _, tabs = publish_v1.build(ctx)
    by_id = {a["eventId"]: a for a in edition["articles"]}
    assert by_id[old.id]["publishedAt"] == iso(late.news_at)
    assert [n["eventId"] for n in report["notifications"]] == [new.id]
    # A lead with no publisher's date at all (dated by its first fetch) is no alert either.
    db.conn.execute("UPDATE items SET feed_published_at = NULL, first_seen_at = published_at WHERE url_canonical = ?",
                    (fresh.url,))
    assert publish_v1.build(ctx)[1]["notifications"] == []


# 4. A widely shared story is not lost when its page cannot be fetched, and a screening drop is not final

LOVABLE = "Lovable apps expose passport details and more than 200K private messages"
LOVABLE_OG = ("An investigation of 159,041 Lovable projects found apps exposing passport scans, and one forum "
              "leaking more than 200,000 private chat messages without a login.")


def _hn_item(url, title, text_, points, vec=(1, 0, 0)):
    it = _thin(url, title, text_, "web", imp=6, people=(), type="security_incident", vec=list(vec))
    it.signals, it.via, it.publisher = {"hn": {"points": points, "comments": 368}}, "hn", "crissyfield.de"
    return it


def test_a_big_hacker_news_link_with_no_fetchable_page_still_gets_an_attributed_card():
    db = _db()
    big = _hn_item("https://crissyfield.test/lovable", LOVABLE, LOVABLE_OG, 969)
    small = _hn_item("https://blog.test/minor", "A minor note on app security", "A short note.", 180, vec=(0, 1, 0))
    _written(db, [big, small])
    for it in (big, small):
        db.conn.execute("UPDATE items SET extra = ? WHERE url_canonical = ?",
                        (json.dumps({"via": "hn", "publisher": it.publisher, "signals": it.signals}), it.url))
        db.add_sighting(it.url, "hn", it.title, None, NOW)
        db.conn.execute("INSERT INTO embeddings VALUES (?,?,?,?,?,?,?)",
                        (it.url, "passage", "m", 3, None, embed.to_blob(it.vec), iso(NOW)))
    # Three fetches failed and the item was dropped for want of text: it is still there to cluster for two days.
    db.conn.execute("UPDATE items SET status = 'dropped', drop_rule = 'held_out:no_text' WHERE url_canonical = ?",
                    (big.url,))
    pending = {i.url: i for i in cluster.pending_items(db, NOW)}
    assert big.url in pending and pending[big.url].signals == big.signals
    assert big.url not in {i.url for i in cluster.pending_items(db, NOW + timedelta(hours=cluster.LOST_TEXT_H + 1))}
    db.conn.execute("UPDATE items SET status = 'dropped', drop_rule = 'triage_dull' WHERE url_canonical = ?",
                    (small.url,))
    assert small.url not in {i.url for i in cluster.pending_items(db, NOW)}     # a judgement drop is not text lost
    store = cluster.Store(db, SRCS, S, NOW, _judge({}, []))
    assert cluster.widely_shared(store, big) and not cluster.widely_shared(store, small)
    eid, how = cluster.assign(store, big)
    assert how == "thin_seed"
    assert cluster.assign(store, small) == (None, "thin_unmatched")             # 180 points start nothing
    cluster.attach_signals(store)
    ev = store.events[eid]
    cluster.refresh(store, ev, lambda s: True)
    assert json.loads(ev.row["hn"])["points"] == 969 and cluster.brief_due(store, ev)
    lines = cluster.brief_lines(store, ev)
    assert lines[0][:2] == ("crissyfield.de", LOVABLE) and lines[-1][0] == cluster.HN_LINE
    assert "969 points and 368 comments" in lines[-1][1]
    card = {"spice": 8, "headline": "Report: Lovable apps expose passport details and 200K messages",
            "headline_short": "Lovable apps expose passports",
            "s1": "An investigation of 159,041 Lovable projects found apps exposing passport scans, crissyfield.de "
                  "reports.",
            "s2": "The link has 969 points and 368 comments on Hacker News.",
            "notif_title": "Lovable apps expose passports",
            "notif_line": "One forum built on Lovable leaked more than 200,000 private chat messages."}
    client = Replies(card, {"entailed": "yes"})
    assert llm.brief_event(client, BRIEF_CFG, store, ev) is True
    assert "HACKER NEWS (DISCUSSION" in client.calls[0][2] and "never whose reporting" in llm.BRIEF_PROMPT
    assert ev.row["text_from"] == cluster.BRIEF and ev.row["headline"] == card["headline"]
    # Hacker News is where it is discussed, never whose reporting it is: a card that credits it fails.
    bad = {**card, "headline": "Lovable apps expose passport details and 200K private messages",
           "s1": "An investigation of 159,041 Lovable projects found apps exposing passport scans, Hacker News says."}
    ev.row.update(headline=None, summary=None, text_from=None, rewritten_at=None)
    assert llm.brief_event(Replies(bad, bad), BRIEF_CFG, store, ev) is False
    # A title alone, with a big thread, is enough for a brief; a title alone with a small one is not.
    assert cluster.BRIEF_MIN_WORDS_SHARED < len(f"{LOVABLE} {lines[-1][1]}".split()) < cluster.BRIEF_MIN_WORDS


def _dropped(db, url, title, rule, signals=None, source_id="techcrunch", pub=NOW):
    db.insert_item({"url_canonical": url, "url": url, "source_id": source_id, "title": title, "first_seen_at": pub,
                    "published_at": pub, "status": "dropped", "drop_rule": rule,
                    "extra": {"signals": signals} if signals else {}})


SNL = "SNL gathers all the tech tycoons for a lesson in dating and spycraft"
PROMOTE_CFG = {"signals": {"promote": {"hn": 150, "lobsters": 60}}}


def test_a_screening_drop_is_reconsidered_when_traction_arrives():
    """TechCrunch's SNL piece was dropped triage_offtopic at 20:23 and stayed dropped."""
    db = _db()
    _dropped(db, "https://tc.test/snl", SNL, "triage_offtopic", {"hn": {"points": 40}})
    _dropped(db, "https://tc.test/deal", "The best laptop deals this week", "triage_shopping",
             {"hn": {"points": 900}})
    _dropped(db, "https://tc.test/old", "An old dull item", "triage_dull", {"hn": {"points": 900}},
             pub=NOW - timedelta(hours=60))
    assert fetch.promote_signals(db, SRCS, PROMOTE_CFG, NOW) == 0          # 40 points: not yet
    db.conn.execute("UPDATE items SET extra = ? WHERE url_canonical = 'https://tc.test/snl'",
                    (json.dumps({"signals": {"hn": {"points": 210, "comments": 90}}}),))
    assert fetch.promote_signals(db, SRCS, PROMOTE_CFG, NOW) == 1
    row = db.item("https://tc.test/snl")
    assert (row["status"], row["drop_rule"], row["triaged"]) == ("new", None, 1)       # written, not screened again
    assert json.loads(row["extra"])["via"] == "hn"
    # A shopping drop is a different judgement (not "is it tech"), and an old item stays where it is.
    assert db.item("https://tc.test/deal")["status"] == db.item("https://tc.test/old")["status"] == "dropped"


def test_drops_are_screened_again_when_the_screening_prompt_changes():
    db = _db()
    _dropped(db, "https://tc.test/snl", SNL, "triage_offtopic")
    _dropped(db, "https://tc.test/q", "How to change your app icons", "title_question")
    _dropped(db, "https://tc.test/old", "An old off-topic item", "triage_offtopic", pub=NOW - timedelta(hours=40))
    assert llm.rescreen_on_prompt_change(db, NOW) == 1         # the prompt these were judged by is not on record
    assert db.item("https://tc.test/snl")["status"] == "new" and db.item("https://tc.test/snl")["triaged"] is None
    assert db.item("https://tc.test/q")["status"] == db.item("https://tc.test/old")["status"] == "dropped"
    db.conn.execute("UPDATE items SET status = 'dropped', drop_rule = 'triage_offtopic' WHERE url_canonical = ?",
                    ("https://tc.test/snl",))
    assert llm.rescreen_on_prompt_change(db, NOW) == 0         # same prompt: its judgement stands
    db.set_meta("triage_prompt_sha", "an older prompt")
    assert llm.rescreen_on_prompt_change(db, NOW) == 1
    assert "SNL gathers all the tech tycoons" in llm.TRIAGE_PROMPT


# 5. Typing and filing: commentary is not rewritten into "the event"; a big security story is Dev and Top news

def test_commentary_is_never_rewritten_or_rescored_as_the_event():
    """ev_623d4c31f8: a Decrypt recap, a Business Insider analysis and an FT opinion line were rewritten into
    "Analysis: Jensen Huang resigns as Nvidia CEO after reports of affairs", re-scored 9, and took the saga's
    main-event place at Top rank 35 while the resignation report itself was cut."""
    db = _db()
    recap = _item("https://decrypt.test/week", [1, 0, 0], "The Week Jensen Huang Fell", source_id="decrypt",
                  type="analysis", imp=4, pub=NOW - timedelta(hours=28))
    view = _item("https://bi.test/next", [0.9, 0.4, 0], "Nvidia's next CEO will inherit more than Huang's job",
                 source_id="businessinsider", type="analysis", imp=4, pub=NOW - timedelta(hours=12))
    third = _item("https://verge.test/after", [0.9, 0.3, 0.2], "What Nvidia looks like after Huang",
                  source_id="verge", type="analysis", imp=5, pub=NOW - timedelta(hours=10))
    _written(db, [recap, view, third])
    store = cluster.Store(db, SRCS, S, NOW)
    eid, _ = cluster.create(store, recap)
    ev = store.events[eid]
    for m in (view, third):
        cluster.join(store, ev, m, 0.9, "judge")
    cluster.refresh(store, ev, lambda s: True)
    assert ev.row["type"] == "analysis" and "publishers" in ev.row["rewrite_reason"]
    assert not cluster.due_rewrite(ev.row, NOW, S)                      # three authors' views are not one report
    # A state written before this rule: the rewrite's headline and its 9 go, the lead's own card comes back.
    ev.row.update(text_from="rewrite", headline="Analysis: Jensen Huang resigns as Nvidia CEO after reports of affairs",
                  rewrite_importance=9)
    cluster.refresh(store, ev, lambda s: True)
    assert ev.row["headline"] == ev.members[0].headline or ev.row["headline"] in {m.headline for m in ev.members}
    assert ev.row["text_from"] != "rewrite" and ev.row["rewrite_importance"] is None and ev.row["importance"] <= 5
    # A report with two outlets behind it is still rewritten.
    news = _item("https://it.test/resigns", [0, 1, 0], "Jensen Huang resigns", source_id="indiatoday_tech", imp=8)
    more = [_item(f"https://x{i}.test/resigns", [0, 1, 0.1 * i], "Huang resigns", source_id=s, imp=8)
            for i, s in enumerate(("cnbc", "bbc"), 1)]
    _written(db, [news, *more])
    nid, _ = cluster.create(store, news)
    for m in more:
        cluster.join(store, store.events[nid], m, 0.9, "judge")
    cluster.refresh(store, store.events[nid], lambda s: True)
    assert cluster.due_rewrite(store.events[nid].row, NOW, S)


LOVABLE_HEAD = "Lovable apps expose passport details, investigation finds"
LOVABLE_SUM = ("One German lifestyle forum exposed more than 200,000 chat messages without requiring a login. The "
               "investigation analyzed 159,041 projects, focusing on Supabase databases rather than Lovable's own.")


def _sec(title, topics=("security",), summary="", **kw):
    c = _cand("sec", type="security_incident", topics=topics, title=title, **kw)
    c.data = {"summary": summary}
    return c


def test_a_widely_shared_security_story_is_dev_and_top_news_not_startups():
    """ev_c8d55e521b (crissyfield.de, HN 969, spice 8, topics [security, ai, startups_funding]): no Top rank, and
    filed under Startups."""
    dev, st = rank.TABS["dev"], rank.TABS["startups"]
    lovable = _sec(LOVABLE_HEAD, ("security", "ai", "startups_funding"), LOVABLE_SUM, importance=6, spice=8, hn=969)
    lovable.unvetted = True
    assert rank.eligible(lovable, dev, NOW) is None             # its summary is about Supabase databases
    assert rank.eligible(lovable, st, NOW) == "topic"           # a breach is not startup news
    assert rank.eligible(lovable, TOP, NOW) is None             # one of the day's biggest threads
    assert not rank.notifiable(lovable)                         # ... but no push alert without a second outlet
    small = _sec(LOVABLE_HEAD, ("security", "ai"), LOVABLE_SUM, importance=7, spice=8, hn=448)
    small.unvetted = True
    assert rank.eligible(small, TOP, NOW) == "unvetted"         # the second review's case still holds
    placed = [("ai", 3, 30), ("dev", 0, 20)]
    assert publish_v1.v1_category(lovable, placed, True) == "Dev"
    assert publish_v1.v1_category(lovable, [("ai", 3, 30)], True) == "AI"       # only when it made Dev


@pytest.mark.parametrize("title,summary,is_dev", [
    # The third review's Dev tab: an HR data leak, one of its three cards under a day old.
    ("TSMC confirms employee data leak tied to cloud service provider",
     "The chipmaker says information about current and former staff appeared online.", False),
    ("Hospital chain says patient records were stolen from cloud storage", "", False),
    ("Cursor breach exposed customer data and API credentials", "", True),
    ("Attackers exploit UnrealIRCd flaw to inject server commands", "", True),
    ("Malicious npm packages steal cloud credentials", "", True),
    ("Attackers steal AWS credentials from a CI runner", "", True),
    ("Workday outage locks millions of employees out of their accounts", "", False),
])
def test_dev_takes_a_breach_for_its_developer_angle_not_for_a_word(title, summary, is_dev):
    assert (rank.eligible(_sec(title, summary=summary), rank.TABS["dev"], NOW) is None) == is_dev
