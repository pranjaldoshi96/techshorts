"""Regression tests for the third review of the VPS test run (revision 222e760, the 22:10 edition of 2026-11-15,
115 cards). Each test names the card, event or row it guards against. No network: the LLM is a scripted client.
"""
import json
from datetime import timedelta

import pytest

from worker import cluster, llm, publish_v1, rank, validate  # noqa: F401
from test_second_run_review import Replies, _written
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
