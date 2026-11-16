"""Regression tests for the check of the third review's fixes against live models and the VPS state (2026-11-15,
revision bbadb79, the 23:27 edition of 117 cards). Each test names the card, event or row it guards against.
No network: the judge, the writer and the fact-checker are scripted.
"""
import json
from datetime import timedelta

from tools import reexamine
from worker import cluster, embed, entail, llm, newsdate, publish_v1, text, validate
from worker.db import from_iso, iso
from test_second_run_review import Replies, _written
from test_spice_and_truth import NOW, S, SRCS, _db, _item, _judge
from test_third_review import CTX_CFG, FAILURE, HOMEVIEW, LAUNCH, MERCOR, SUNDAY
from test_validate import CFG as WRITE_CFG, DAYLIGHT, FakeClient, _db_with, _sources


def _edition_ids(db, now=NOW):
    cands, _ = publish_v1.load_candidates(publish_v1.Ctx(db, CTX_CFG, SRCS, now))
    return {c.id for c in cands}


# 1. Rules that act when an item is clustered or written, applied to what the state already holds

def _launch_and_failure(db, judge=None):
    """The state the deploy found: the failure clustered as a plain follow-up before any supersede rule."""
    launch = _item("https://mint.test/vulcan", [1, 0, 0], LAUNCH, source_id="livemint_companies", people=())
    failure = _item("https://sfn.test/failure", [0.8, 0.6, 0], FAILURE, source_id="spaceflightnow", people=(),
                    pub=NOW + timedelta(minutes=45))
    _written(db, [launch, failure])
    store = cluster.Store(db, SRCS, S, NOW + timedelta(hours=1), judge)
    old, _ = cluster.create(store, launch)
    new, _ = cluster.create(store, failure, parent_id=old)
    store.events[old].row["superseded_by"] = None
    db.conn.execute("UPDATE events SET superseded_by = NULL")
    for ev in store.events.values():
        cluster.refresh(store, ev, lambda s: True)
    return store, old, new


def test_a_follow_up_already_in_the_state_supersedes_the_card_it_overtook():
    """ev_f6041b806d "ULA launches Vulcan with 40 Amazon internet satellites" was still Top rank 23 beside
    ev_0054e860a0, the rocket lost, ten minutes after the supersede rule was deployed: the rule fires when a
    follow-up is clustered, and this one had been clustered four hours earlier."""
    db, calls = _db(), []
    store, old, new = _launch_and_failure(db, _judge({FAILURE: "followup"}, calls))
    later = NOW + timedelta(hours=1)
    assert _edition_ids(db, later) == {old, new}
    since = NOW - timedelta(hours=72)
    # A dry run asks the judge and writes nothing.
    assert [(o.id, n.id, rel) for o, n, rel in reexamine.superseded(store, since, dry_run=True)] == [
        (old, new, "followup")]
    assert db.one("SELECT superseded_by FROM events WHERE id = ?", old)["superseded_by"] is None
    assert reexamine.superseded(store, since) and calls[-1] == (FAILURE, [old])     # only the older event is asked about
    assert db.one("SELECT superseded_by FROM events WHERE id = ?", old)["superseded_by"] == new
    assert _edition_ids(db, later) == {new}
    assert reexamine.superseded(store, since) == []                                 # nothing left to mark


def test_only_a_ruling_or_a_reversal_supersedes_and_never_commentary():
    db, calls = _db(), []
    first = _item("https://axios.test/faa", [1, 0, 0], "FAA back at full operations, outage tied to copper line work",
                  source_id="axios", people=("Sean Duffy",))
    probe = _item("https://fortune.test/fbi", [0.8, 0.6, 0], "FBI joins FAA outage probe as Duffy sees no hack",
                  source_id="fortune", people=("Sean Duffy",), pub=NOW + timedelta(hours=20))
    column = _item("https://verge.test/col", [0.8, 0.5, 0.3], "The FAA's week from hell", source_id="verge",
                   people=("Sean Duffy",), type="analysis", pub=NOW + timedelta(hours=21))
    _written(db, [first, probe, column])
    rulings = {probe.title: "followup", column.title: "supersedes"}
    store = cluster.Store(db, SRCS, S, NOW + timedelta(hours=22), _judge(rulings, calls))
    old, _ = cluster.create(store, first)
    for it in (probe, column):
        cluster.create(store, it, parent_id=old)
    for ev in store.events.values():
        cluster.refresh(store, ev, lambda s: True)
    # A plain follow-up with no reversal in its title leaves the older card; an analysis is never even put to the judge.
    assert reexamine.superseded(store, NOW - timedelta(hours=72)) == []
    assert [c[0] for c in calls] == [probe.title]
    rulings[probe.title] = "supersedes"
    got = reexamine.superseded(store, NOW - timedelta(hours=72))
    assert [(o.id, n.anchor.title) for o, n, _ in got] == [(old, probe.title)]


def _write_row(db, url, title, text_, type="news", pub=SUNDAY, lead_of=None):
    db.insert_item({"url_canonical": url, "url": url, "source_id": "pymnts", "title": title, "first_seen_at": pub,
                    "published_at": pub, "status": "written"})
    db.conn.execute("INSERT INTO writes (url_canonical, status, written_at, type, text, headline) VALUES (?,?,?,?,?,?)",
                    (url, "ok", iso(pub), type, text_, title))
    if lead_of:
        db.conn.execute("INSERT INTO events (id, status, lead_url, anchor_url, headline, created_at, updated_at,"
                        " published_at) VALUES (?, 'live', ?, ?, ?, ?, ?, ?)",
                        (lead_of, url, url, title, iso(pub), iso(pub), iso(pub)))


def test_cards_written_before_the_writer_was_asked_get_their_news_date():
    """ev_e54189102d "Apple starts selling HomeView" was dated 21:55 on Sunday after the deploy too: `news_date`
    is asked for when a card is written, and this one had been written an hour before."""
    db = _db()
    _write_row(db, "https://pymnts.test/homeview", "Apple Puts Siri AI at the Center of the Home", HOMEVIEW)
    _write_row(db, "https://mint.test/mercor", "Mercor founders", MERCOR, "funding", lead_of="ev_mercor")
    _write_row(db, "https://mint.test/old", "Mercor founders again", MERCOR, "funding",
               pub=SUNDAY - timedelta(hours=60), lead_of="ev_old")
    _write_row(db, "https://bi.test/analysis", "Who is left running Nvidia?", "Nvidia's board announced on Thursday "
               "that Jensen Huang had resigned as president and chief executive.", "analysis", lead_of="ev_an")
    asked = []

    def ask(published, title, headline, text_):
        asked.append(title)
        return "2026-11-10"
    since, ask_since = SUNDAY - timedelta(hours=72), SUNDAY - timedelta(hours=36)
    got = reexamine.dates(db, since, dry_run=True)
    assert [(u, how) for u, _, _, _, how in got] == [("https://pymnts.test/homeview", "lede")]
    assert db.write_row("https://pymnts.test/homeview")["news_at"] is None           # a dry run
    got = reexamine.dates(db, since, ask, ask_since)
    # The first sentence dates HomeView at no cost; the writer is asked about an event's fresh lead only, and never
    # about an analysis (which is dated as what it is, a piece published when it was).
    assert {u: how for u, _, _, _, how in got} == {"https://pymnts.test/homeview": "lede",
                                                   "https://mint.test/mercor": "writer"}
    assert asked == ["Mercor founders"]
    assert db.write_row("https://pymnts.test/homeview")["news_at"] == "2026-11-13T12:00:00Z"
    assert db.write_row("https://mint.test/mercor")["news_at"] == "2026-11-10T12:00:00Z"
    assert db.write_row("https://bi.test/analysis")["news_at"] is None
    assert reexamine.dates(db, since, ask, ask_since) == [] and asked == ["Mercor founders"]     # dated once


def test_the_date_call_asks_the_write_prompts_own_question():
    assert llm.NEWS_DATE in llm.PROMPT and llm.NEWS_DATE in llm.DATE_PROMPT
    client = Replies({"news_date": "2026-11-10"}, {"news_date": "last Tuesday"}, "no json")
    cfg = {"llm": {"model": "w"}}
    assert llm.date_news(client, cfg, "2026-11-15T13:58:42Z", "t", "h", MERCOR) == "2026-11-10"
    assert "Published: 2026-11-15T13:58:42Z" in client.calls[0][2] and client.calls[0][1] == "date"
    assert llm.date_news(client, cfg, "2026-11-15T13:58:42Z", "t", "h", MERCOR) == ""
    try:
        llm.date_news(client, cfg, "2026-11-15T13:58:42Z", "t", "h", MERCOR)
        assert False
    except llm.LLMError:
        pass


def test_an_event_with_a_newer_report_is_queued_for_the_rewrite_that_now_leads_with_it():
    """ev_90efe7abd3 "FAA back to full operations after outage, Duffy says" kept "routine maintenance replacing an
    old copper line" although CNBC's report of the next day (Duffy blames a router) was a member: that report was
    published before the last rewrite, so nothing queued another."""
    db = _db()
    first = _item("https://axios.test/faa", [1, 0, 0], "FAA back at full operations after Friday outage",
                  source_id="axios", pub=NOW - timedelta(hours=30))
    router = _item("https://cnbc.test/router", [0.9, 0.4, 0], "Duffy blames router for nationwide ground stop",
                   source_id="cnbc", pub=NOW - timedelta(hours=5))
    solo = _item("https://verge.test/solo", [0, 1, 0], "A story with one report", source_id="verge")
    _written(db, [first, router, solo])
    store = cluster.Store(db, SRCS, S, NOW)
    eid, _ = cluster.create(store, first)
    cluster.join(store, store.events[eid], router, 0.9, "judge")
    other, _ = cluster.create(store, solo)
    for ev in store.events.values():
        cluster.refresh(store, ev, lambda s: True)
        ev.row.update(rewritten_at=iso(NOW - timedelta(hours=4)), rewrite_reason=None)   # rewritten since
    store.events[eid].row["lead_url"] = first.url           # as on the VPS: the first report led
    assert [e.id for e in reexamine.rewrites(store, NOW - timedelta(hours=72), dry_run=True)] == [eid]
    assert store.events[eid].row["rewrite_reason"] is None
    assert [e.id for e in reexamine.rewrites(store, NOW - timedelta(hours=72))] == [eid]
    row = db.one("SELECT rewrite_reason, rewritten_at FROM events WHERE id = ?", eid)
    assert (row["rewrite_reason"], row["rewritten_at"]) == ("newer_report", None)
    # Rewritten 4 h ago in the test, but a card rewritten minutes ago by the old rule is due as well.
    assert cluster.due_rewrite(store.events[eid].row, NOW, S)
    assert reexamine.rewrites(store, NOW - timedelta(hours=72)) == []               # queued once


MADONNA_S = ("In 2023, Dario Amodei met Ashton Kutcher and music manager Guy Oseary at Oseary's home.",
             "The group spent hours discussing AI safety and the global race to build better models.",
             "Framed art by rock legends John Lennon and David Bowie hung on the wall behind them.")


def _card(db, url, title, source_id="bloomberg", vec=(1, 0, 0), **kw):
    it = _item(url, list(vec), title, source_id=source_id, people=(), **kw)
    it.s1, it.summary, it.notif_title, it.notif_line = MADONNA_S[0], " ".join(MADONNA_S), "A title here", "A line."
    _written(db, [it])
    db.conn.execute("INSERT INTO embeddings VALUES (?,?,?,?,?,?,?)",
                    (url, "passage", "m", 3, "sha", embed.to_blob(it.vec), iso(NOW)))
    db.conn.execute("UPDATE writes SET s1 = ?, s2 = ?, s3 = ?, summary = ?, summary_short = ?, headline = ?, text = ?,"
                    " output = ?, steps = '[]', model = 'w' WHERE url_canonical = ?",
                    (*MADONNA_S, it.summary, " ".join(MADONNA_S[:2]), it.headline, it.text,
                     json.dumps({"headline": it.headline}), url))
    return it


def test_an_edition_is_fact_checked_again_and_a_card_that_fails_goes_back_to_the_writer():
    """ev_b24410c214 "Madonna manager's VC firm has $7B stake in Anthropic" (Top rank 32) and ev_f3733b9810
    "Marcus: FAA ground stop revives warnings about AI risks" ("Writing from EWR ... grouchy") were written before
    the fact-checker's rules 10 and 11 and stayed in the edition after them."""
    db = _db()
    stake = _card(db, "https://bloomberg.test/stake", "Madonna manager's VC firm has $7B stake in Anthropic")
    fine = _card(db, "https://verge.test/fine", "A card the fact-checker passes", "verge", (0, 1, 0))
    store = cluster.Store(db, SRCS, S, NOW)
    ids = [cluster.create(store, it)[0] for it in (stake, fine)]
    for ev in store.events.values():
        cluster.refresh(store, ev, lambda s: True)
    db.commit()
    seen = []

    def check(**f):
        seen.append(f)
        bad = "Madonna" in f["headline"]
        return entail.Entailment(not bad, "no sentence is about the stake." if bad else "", "summary" if bad else "")
    got = reexamine.recheck(store, ids + ["ev_gone"], check, dry_run=True)
    assert [(e.id, kind) for e, kind, _ in got] == [(ids[0], "card")]
    assert db.write_row(stake.url)["status"] == "ok"
    # The fact-checker is given the card as the edition shows it, against the lead's own text.
    assert seen[0]["lead"] == stake.text and seen[0]["s2"] == MADONNA_S[1] and seen[0]["s3"] == MADONNA_S[2]
    assert seen[0]["publisher"] == "Bloomberg"
    reexamine.recheck(store, ids, check)
    w, item = db.write_row(stake.url), db.item(stake.url)
    assert (w["status"], w["reason"]) == ("failed", "entail_fail")
    assert (item["status"], item["retry_at"], item["attempts"], item["hold_reason"]) == ("held", iso(NOW), 0, "recheck")
    # The writer's next run continues from its stored answer and the fact-checker's reason (llm.prior_attempt).
    reply, feedback = llm.prior_attempt(db, stake.url)
    assert "Madonna" in reply and "Keep the headline and write S1 to S3 about its news" in feedback[0]
    assert db.write_row(fine.url)["status"] == "ok" and db.item(fine.url)["status"] == "written"
    # The card leaves the edition with the next clustering pass, until a new one validates.
    store = cluster.Store(db, SRCS, S, NOW)
    for ev in store.events.values():
        cluster.refresh(store, ev, lambda s: True)
    assert store.events[ids[0]].row["headline"] is None and store.events[ids[1]].row["headline"]


def test_a_rewrite_or_brief_that_fails_the_recheck_goes_back_to_its_queue():
    db = _db()
    lead = _card(db, "https://axios.test/faa", "FAA back at full operations after outage", "axios")
    other = _card(db, "https://cnbc.test/router", "Duffy blames router for ground stop", "cnbc", (0.9, 0.4, 0),
                  pub=NOW + timedelta(hours=5))
    store = cluster.Store(db, SRCS, S, NOW + timedelta(hours=6))
    eid, _ = cluster.create(store, lead)
    ev = store.events[eid]
    cluster.join(store, ev, other, 0.9, "judge")
    cluster.refresh(store, ev, lambda s: True)
    ev.row.update(text_from="rewrite", headline="FAA outage tied to copper line work", s1="One sentence.",
                  summary="One sentence. Another sentence.", rewrite_importance=7, rewrite_reason=None)
    cluster.save(db, ev.row)
    seen = []

    def check(**f):
        seen.append(f)
        return entail.Entailment(False, "the newest report blames a router.", "S1")
    assert [(e.id, k) for e, k, _ in reexamine.recheck(store, [eid], check)] == [(eid, "rewrite")]
    # Checked against the reports and their titles, the newest first after the lead.
    assert seen[0]["s2"] == "Another sentence." and other.title in seen[0]["lead"] and lead.text in seen[0]["lead"]
    row = db.one("SELECT text_from, rewrite_importance, rewrite_reason FROM events WHERE id = ?", eid)
    assert (row["text_from"], row["rewrite_importance"], row["rewrite_reason"]) == (None, None, "recheck")
    # The next pass shows the lead's own card again and the event is due a rewrite.
    store = cluster.Store(db, SRCS, S, NOW + timedelta(hours=6))
    cluster.refresh(store, store.events[eid], lambda s: True)
    assert store.events[eid].row["headline"] in (lead.headline, other.headline)
    assert store.events[eid].row["text_from"] != "rewrite"
    assert cluster.due_rewrite(store.events[eid].row, NOW + timedelta(hours=6), S)


# 2. Prompt and check changes after reading live outputs

def test_the_fact_checkers_summary_rule_passes_sentences_that_carry_the_story_on():
    """Rule 11 as first written ("at least one of S1 to S3 must state a fact about what the headline announces")
    failed 9 of the 117 cards of the 23:27 edition when they were checked again, 4 of them sound: "Report: Qualcomm
    ties extra month's pay to resigning in 3 days" over the size of the cuts and the rest of the package, "TSMC
    plans $500 million fund" over whom it will back. The writer is told not to repeat the headline."""
    rule = " ".join(entail.PROMPT.split())
    assert "need not restate its fact" in rule and "Fail only when none of them is" in rule
    assert "ties extra pay to resigning in 3 days" in rule and "hung on the wall" in rule
    # "Rewrite that line" made the writer swap the headline for another one the sentences did not report either.
    fb = entail.Entailment(False, "No sentence mentions the stake.", "summary").feedback()
    assert "Keep the headline and write S1 to S3 about its news" in fb and "Rewrite that line" not in fb
    assert "Rewrite that line" in entail.Entailment(False, "x", "S2").feedback()


VERGE_TITLE = "Vulcan rocket fails, destroying 40 Amazon Leo satellites"
VERGE = ("United Launch Alliance (ULA) suffered what is reportedly its first mission failure in its 20-year history, "
         "failing to deliver 40 Amazon Leo satellites to orbit. In a statement posted on X, ULA said the Vulcan "
         "vehicle suffered an anomaly 10 minutes into the flight and failed to deliver the Amazon mission to orbit.")


def test_a_label_the_fact_checker_rejects_is_explained_to_the_writer():
    """The Verge's Vulcan report failed twice on the VPS: the text hedges "its first mission failure", the
    headline named ULA with no hedge, code labelled it "Report:", the fact-checker (rightly) said ULA itself
    confirmed the failure, and the retry dropped the label only for code to add it back."""
    scope = validate.hedge_scope(VERGE_TITLE, VERGE)
    own = "ULA's Vulcan rocket fails, destroying 40 Amazon Leo satellites"
    hint = validate.label_feedback("Report: " + own, own, scope)
    assert '"reportedly"' in hint and "first mission failure" in hint and "no label is needed" in hint
    assert validate.label_feedback("Report: " + own, "Report: " + own, scope) is None     # the writer's own label
    assert validate.label_feedback(own, own, scope) is None
    kept = "ULA reportedly suffers first mission failure, losing 40 satellites"
    assert validate.hedge_problem(kept, scope) is None


def test_only_a_report_is_dated_by_its_news_and_a_vague_phrase_dates_only_what_it_can():
    """Asked for the news date of the 23:27 edition's leads, the writer dated "Is Nvidia cooked without Jensen
    Huang?" to Nov 8 (four days before he resigned; any "last week" in the text counted as carrying any date),
    and the first sentence dated two analyses of the resignation by the resignation."""
    opening = ("Nvidia's board announced on Thursday that Jensen Huang had resigned as president, CEO and "
               "director, effective immediately.")
    pub = from_iso("2026-11-14T10:53:57Z")
    assert newsdate.news_at("", pub, "Who's left running Nvidia?", opening) == from_iso("2026-11-12T12:00:00Z")
    assert newsdate.news_at("", pub, "Who's left running Nvidia?", opening, "analysis") is None
    assert newsdate.news_at("", pub, "Nvidia's Huang resigns", opening, "news") == from_iso("2026-11-12T12:00:00Z")
    week = "Greenzie passed a major milestone with its self-driving mowers last week, it announced. " + "More. " * 80
    sunday = from_iso("2026-11-15T16:07:39Z")
    assert newsdate.news_at("2026-11-08", sunday, "Mowing milestone", week, "news") == from_iso("2026-11-08T12:00:00Z")
    assert newsdate.news_at("2026-10-20", sunday, "Mowing milestone", week, "news") is None      # not last week
    late = "Stellantis has a new patent. " + "More. " * 80 + "It follows last week's election."
    assert newsdate.news_at("2026-11-03", sunday, "A patent", late, "news") is None              # not in the opening
    rule = " ".join(llm.NEWS_DATE.split())
    assert "Empty when the article is itself the news" in rule and "never for analysis" in rule


def _new_item(n):
    return {"url": f"https://techcrunch.com/{n}", "url_canonical": f"https://techcrunch.com/{n}",
            "source_id": "techcrunch", "title": f"Item {n}", "feed_text": "", "published_at": "2026-11-11T03:00:00Z"}


def test_a_backlog_is_screened_before_any_of_it_is_written(monkeypatch):
    """The first tick after the screening prompt changed put 163 screening drops back in the queue, screened 40
    of them and wrote the next in line unscreened: 78 cards were written, among them "I was a single mom of 4
    boys" and a dozen how-tos."""
    monkeypatch.setattr(llm, "TRIAGE_BATCH", 2)
    monkeypatch.setattr(llm, "TRIAGE_BATCHES", 2)
    db, now = _db_with([_new_item(n) for n in range(7)])
    screened = []

    def screen(client, cfg, rows, by_id):
        screened.append([r["title"] for r in rows])
        return {rows[0]["url_canonical"]: "dull"}
    monkeypatch.setattr(llm, "triage_batch", screen)
    wrote = []

    def fetch_text(item, source):
        wrote.append(item["title"])
        return text.choose([], "")                 # nothing to write from: the test is about who gets this far
    cfg = {**WRITE_CFG, "llm": {**WRITE_CFG["llm"], "triage": True}}
    stats = llm.write_pending(db, _sources(), cfg, FakeClient([DAYLIGHT, DAYLIGHT]), fetch_text=fetch_text, now=now)
    assert len(screened) == 2 and stats["triaged_out"] == 2 and stats["screen_waiting"] == 3
    assert len(wrote) == 2 and set(wrote) == {t for batch in screened for t in batch[1:]}
    waiting = [r["title"] for r in db.q("SELECT title FROM items WHERE status = 'new' AND triaged IS NULL")]
    assert len(waiting) == 3 and not set(waiting) & set(wrote)
