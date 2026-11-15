"""Fixes from the 4-day R1 shadow run (2026-11-11 to 11-15): the LLM circuit breaker, hold retries that continue
the last answer, the hedge repair, repetition checks, the calibrated duplicate guard, twins, storylines, the Dev
rule and v1 category placement. No network."""
from datetime import timedelta

import numpy as np
import requests

import test_cluster as tc
from test_validate import (CFG, DAYLIGHT, DAYLIGHT_SRC, HUANG, HUANG_SRC, FakeClient, FakeClock, FakeResponse,
                           _db_with, _sources, out)
from worker import cluster, llm, publish_v1, rank, text, validate
from worker.db import DB, iso

NOW = tc.EDITION_AT


# LLM client: timeouts are not retried, and two in a row open the circuit for the tick

class TimeoutSession:
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls += 1
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _client(session):
    clock = FakeClock()
    return llm.Client("https://llm.test/v1", "k", llm.TokenBucket(30, 60.0, clock, clock.sleep), session,
                      clock=clock, sleep=clock.sleep)


def ok_response(content="{}"):
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def test_a_timeout_is_not_retried_and_two_open_the_circuit():
    session = TimeoutSession([requests.ReadTimeout(), requests.ReadTimeout(), ok_response()])
    client = _client(session)
    for _ in range(2):
        try:
            client.chat("m", [])
            raise AssertionError("expected LLMError")
        except llm.CircuitOpen:
            raise AssertionError("circuit opened too early")
        except llm.LLMError:
            pass
    assert session.calls == 2 and client.circuit_open
    try:
        client.chat("m", [])
        raise AssertionError("expected CircuitOpen")
    except llm.CircuitOpen:
        pass
    assert session.calls == 2                       # no call while open
    client.reset_breaker()
    assert client.chat("m", []) == "{}"
    assert [e[2] for e in client.drain()] == ["ReadTimeout", "ReadTimeout", "ok"]


def test_connection_errors_and_5xx_still_retry():
    session = TimeoutSession([requests.ConnectionError(), FakeResponse(503), ok_response()])
    client = _client(session)
    assert client.chat("m", []) == "{}" and session.calls == 3 and not client.circuit_open


class CircuitClient(FakeClient):
    def chat(self, *a, **k):
        raise llm.CircuitOpen("open")


def _item(u="https://example.com/d", src="verge", title="Daylight raises $80M"):
    return {"url": u, "url_canonical": u, "source_id": src, "title": title, "feed_text": "",
            "published_at": "2026-11-11T03:00:00Z"}


def test_an_open_circuit_leaves_items_untouched_for_the_next_tick():
    db, now = _db_with([_item()])
    stats = llm.write_pending(db, _sources(), CFG, CircuitClient([]),
                              fetch_text=lambda i, s: text.choose([DAYLIGHT_SRC], ""), now=now)
    assert stats["circuit_skipped"] == 1
    row = db.item("https://example.com/d")
    assert row["status"] == "new" and row["attempts"] == 0
    assert db.one("SELECT COUNT(*) n FROM write_log WHERE kind = 'item'")["n"] == 0


def test_llm_errors_are_logged_as_such_and_held_as_transient():
    db, now = _db_with([_item()])
    client = FakeClient([llm.LLMError("timeout")] * 3)        # every rung, fallback model included
    stats = llm.write_pending(db, _sources(), CFG, client,
                              fetch_text=lambda i, s: text.choose([DAYLIGHT_SRC], ""), now=now)
    assert stats["llm_error"] == 1
    log = db.one("SELECT outcome, reason, attempt FROM write_log WHERE kind = 'item'")
    assert (log["outcome"], log["reason"], log["attempt"]) == ("llm_error", "llm_error", 1)
    row = db.item("https://example.com/d")
    assert row["status"] == "held" and row["hold_reason"] == "llm_error"


def test_a_hold_retry_continues_from_the_last_answer_and_its_feedback():
    db, now = _db_with([_item()])
    cfg = {**CFG, "llm": {**CFG["llm"], "fallback_model": None}}
    bad = {**DAYLIGHT, "headline": "Daylight raises $80M in November"}
    fetch = lambda i, s: text.choose([DAYLIGHT_SRC], "")      # noqa: E731
    llm.write_pending(db, _sources(), cfg, FakeClient([bad, bad]), fetch_text=fetch, now=now)
    assert db.item("https://example.com/d")["hold_reason"] == "ungrounded"
    client = FakeClient([DAYLIGHT])
    stats = llm.write_pending(db, _sources(), cfg, client, fetch_text=fetch, now=now + timedelta(minutes=31))
    assert stats["written"] == 1
    first = client.calls[0][2]
    assert first[-2]["role"] == "assistant" and "November" in first[-2]["content"]
    assert "'November' is not in the source text" in first[-1]["content"]
    assert client.temperatures[0] == 0.5
    attempts = [r["attempt"] for r in db.q("SELECT attempt FROM write_log WHERE kind = 'item' ORDER BY rowid")]
    assert attempts == [1, 2]


def test_a_content_failure_drops_after_one_retry():
    db, now = _db_with([_item()])
    cfg = {**CFG, "llm": {**CFG["llm"], "fallback_model": None}}
    bad = {**DAYLIGHT, "headline": "Daylight raises $80M in November"}
    fetch = lambda i, s: text.choose([DAYLIGHT_SRC], "")      # noqa: E731
    llm.write_pending(db, _sources(), cfg, FakeClient([bad] * 2), fetch_text=fetch, now=now)
    llm.write_pending(db, _sources(), cfg, FakeClient([bad] * 2), fetch_text=fetch, now=now + timedelta(minutes=31))
    row = db.item("https://example.com/d")
    assert row["status"] == "dropped" and row["drop_rule"] == "held_out:ungrounded"


# Hedges: picked or repaired in code

def test_select_prefers_the_headline_that_keeps_the_hedge():
    o = out(HUANG, headline="Jensen Huang admits affair with Chinese spy",
            headline_alt="Jensen Huang admits affair with suspected spy")
    validate.select(o, hedge_source=validate.hedge_scope("Nvidia CEO and the spy", HUANG_SRC))
    assert o.headline == "Jensen Huang admits affair with suspected spy"


def test_select_labels_the_headline_when_every_version_drops_the_hedge():
    o = out(HUANG, headline="Jensen Huang admits affair with Chinese spy", headline_alt="", headline_short="")
    validate.select(o, hedge_source=validate.hedge_scope("Nvidia CEO and the spy", HUANG_SRC))
    assert o.headline == "Report: Jensen Huang admits affair with Chinese spy"
    assert validate.check(o, HUANG_SRC, "Nvidia CEO and the spy", "Example News").ok


def test_a_claim_in_the_source_title_must_stay_attributed():
    title = "Daylight can triple battery output with new plant, says former engineer Ruiz"
    v = validate.check(out(DAYLIGHT, headline="Daylight can triple battery output with new plant"),
                       DAYLIGHT_SRC + " Daylight can triple battery output.", title, "Example News")
    assert "hedge_dropped" in v.reasons
    assert validate.title_claim_dropped("Daylight says it can triple battery output", title) is None
    # A company's own announcement: the headline's subject is the speaker.
    assert validate.title_claim_dropped("Daylight will triple output", "Daylight says it will triple output") is None


def test_a_column_is_analysis_whatever_the_model_says():
    from worker import attribution
    o = out(HUANG)
    attribution.apply(o, None, "https://www.theverge.com/column/1033943/jensen-huang-nvidia-scandal")
    assert o.type == "analysis"
    validate.select(o)
    assert o.headline.startswith("Analysis: ")
    o = out(HUANG)
    attribution.apply(o, None, "https://www.theverge.com/news/1033943/jensen-huang-nvidia-scandal")
    assert o.type == "news"


# Repetition

def test_s1_must_not_restate_the_headline():
    v = validate.check(out(DAYLIGHT, s1="Daylight raises $80M Series B led by Benchmark."), DAYLIGHT_SRC,
                       "Daylight raises $80M", "Example News")
    assert "repeats_headline" in v.reasons
    assert validate.repeat_problems(DAYLIGHT["headline"], DAYLIGHT["s1"], DAYLIGHT["s2"], DAYLIGHT["s3"]) == []


def test_two_sentences_must_not_say_the_same_thing():
    s = "SpaceX caught the Starship upper stage with the launch tower arms for the first time on Thursday."
    probs = validate.repeat_problems("SpaceX catches Starship upper stage", "Something else entirely here.", s,
                                     s.replace("Thursday", "Thursday evening"))
    assert [c for c, _ in probs] == ["repeats_sentence"]


def test_select_avoids_an_s1_that_restates_the_headline():
    o = out(DAYLIGHT, s1="Daylight raises $80M Series B led by Benchmark.",
            s1_alt="Daylight raised $80M in a Series B round led by Benchmark to build solar batteries for homes.")
    validate.select(o)
    assert o.s1.startswith("Daylight raised $80M in a Series B round")


# Prompt calibration

def test_importance_anchors_name_no_real_story_and_are_shared_with_the_rewrite():
    for live in ("Anthropic", "Nvidia", "CoreWeave", "Daylight"):
        assert live not in llm.IMPORTANCE
    assert llm.IMPORTANCE in llm.PROMPT and llm.IMPORTANCE in llm.REWRITE_PROMPT
    assert "= 6, launch" in llm.IMPORTANCE and "= 5, policy" in llm.IMPORTANCE
    assert "Follow-ups" in llm.IMPORTANCE


# Ranking: Dev, the calibrated duplicate guard, storylines

def cand(eid, importance=6, type="news", topics=("ai",), age_h=2.0, n=1, publisher=None, vec=None, keys=(), **kw):
    return rank.Candidate(
        id=eid, importance=importance, type=type, is_news=kw.pop("is_news", True), relevance=3, topics=list(topics),
        region="global", n_publishers=n, on_techmeme=False, hn_points=0, authority=1.0,
        published_at=NOW - timedelta(hours=age_h), lead_source=publisher or eid, lead_publisher=publisher or eid,
        headline_vec=None if vec is None else np.asarray(vec, dtype=float), keys=frozenset(keys), **kw)


def test_dev_needs_dev_tools_as_the_main_topic_or_a_developer_release():
    dev = rank.TABS["dev"]
    assert rank.eligible(cand("a", topics=("dev_tools", "ai")), dev, NOW) is None
    # A launch needs dev_tools first ("ElevenLabs launches ElevenAI app for meeting note-taking" was [ai, dev_tools]);
    # release notes and tutorials may carry it second.
    assert rank.eligible(cand("b", type="launch", topics=("ai", "dev_tools")), dev, NOW) == "topic"
    assert rank.eligible(cand("b2", type="release_notes", topics=("ai", "dev_tools")), dev, NOW) is None
    # SpaceX Starship [security, dev_tools, bigtech]; CoreWeave earnings; a bike startup's round.
    assert rank.eligible(cand("c", topics=("security", "dev_tools", "bigtech")), dev, NOW) == "topic"
    assert rank.eligible(cand("d", type="earnings", topics=("dev_tools",)), dev, NOW) == "topic"
    assert rank.eligible(cand("e", type="funding", topics=("dev_tools", "startups_funding")), dev, NOW) == "topic"
    # Analysis needs 5 in Dev.
    assert rank.eligible(cand("f", type="analysis", importance=4, topics=("dev_tools",), is_news=False), dev,
                         NOW) == "importance"
    assert rank.eligible(cand("g", type="analysis", importance=5, topics=("dev_tools",), is_news=False), dev,
                         NOW) is None


def unit(deg):
    r = np.radians(deg)
    return [np.cos(r), np.sin(r)]


def test_duplicate_guard_needs_a_shared_entity_below_the_alone_threshold():
    d = rank.Dedup(alone=0.65, t=0.5)
    huang = ("p", "huang")
    a = cand("a", vec=unit(0), keys=[huang])
    b = cand("b", vec=unit(55), keys=[huang])        # cosine 0.57, shared person
    c = cand("c", vec=unit(55), keys=[("x", "outlook")])
    e = cand("e", vec=unit(45))                      # cosine 0.71, no entities
    assert d.duplicate(a, b) and not d.duplicate(a, c) and d.duplicate(a, e)
    # Workday vs Outlook outage (0.54, different products) both stay.
    work = cand("w", vec=unit(57), keys=[("x", "workday")], n=1)
    kept, dropped = publish_v1.suppress_duplicates([cand("o", vec=unit(0), keys=[("x", "outlook")], n=3), work], d)
    assert len(kept) == 2 and not dropped


def test_storyline_cap_keeps_two_cards_per_story_per_tab():
    huang = ("p", "huang")
    cs = [cand(f"h{i}", importance=8, n=3, age_h=i, publisher=f"P{i}", vec=unit(60 + i), keys=[huang])
          for i in range(4)]
    cs.append(cand("other", importance=6, age_h=1, publisher="Q", vec=unit(-60)))
    publish_v1.storylines(cs, rank.Dedup(0.65, 0.5), 0.45)
    assert len({c.storyline for c in cs[:4]}) == 1
    ids = [c.id for c, _ in rank.assemble(cs, rank.TABS["top"], NOW)]
    assert ids == ["h0", "h1", "other"]
    # A follow-up joins its parent's storyline even when the headlines are far apart.
    p, f = cand("p", vec=unit(0)), cand("f", vec=unit(90))
    p.data, f.data = {}, {"parent_id": "p"}
    publish_v1.storylines([p, f], rank.Dedup(0.65, 0.5), 0.45)
    assert p.storyline == f.storyline


def test_v1_category_follows_the_main_topic():
    c = cand("lc", type="funding", topics=("startups_funding", "dev_tools"))
    # Ranks better (relatively) in Dev, but a funding round belongs under Startups.
    assert publish_v1.v1_category(c, [("startups", 9, 10), ("dev", 0, 18)], False) == "Startups"
    ai = cand("m", type="launch", topics=("ai", "dev_tools"))
    assert publish_v1.v1_category(ai, [("ai", 12, 25), ("dev", 0, 18)], True) == "AI"
    # Not placed in its main topic's tab: the relative-rank rule as before.
    assert publish_v1.v1_category(cand("x", topics=("bigtech", "ai")), [("ai", 10, 25), ("dev", 0, 5)],
                                  False) == "Dev"


# Twins merge into their primary

def test_twins_merge_into_the_primary_and_carry_a_material_update():
    db = tc.new_db()
    old = tc._item("https://a/ipo", [1, 0.1, 0], people=["Dario Amodei"], pub=NOW - timedelta(hours=30))
    twin = tc._item("https://b/ipo-again", [0.2, 1, 0], source_id="verge", pub=NOW - timedelta(hours=8),
                    material=True)
    tc._db_items(db, [old, twin])
    store = tc._store_with(db, [old, twin])
    primary, twin_id = cluster.event_id(old.url), cluster.event_id(twin.url)
    db.conn.execute("INSERT INTO twins VALUES (?,?,?,?)", (twin_id, primary, None, iso(NOW)))
    assert cluster.merge_pass(store) == 0               # too old and too far apart for the merge pass
    assert cluster.merge_twins(store) == 1
    assert set(store.events) == {primary} and cluster.resolve(db, twin_id) == primary
    ev = store.events[primary]
    assert {m.url for m in ev.members} == {old.url, twin.url}
    assert ev.row["last_material_update_at"] == iso(twin.published_at) and "material_update" in ev.dirty
    assert cluster.merge_twins(store) == 0              # already done


def test_freshness_leaves_out_twins():
    from worker import health
    db = DB(":memory:")
    for eid, lag_h in (("ev_a", 0.5), ("ev_twin", 40)):
        db.conn.execute("INSERT INTO shipped VALUES (?,?,?,?,?)",
                        (eid, iso(NOW - timedelta(hours=lag_h)), iso(NOW), iso(NOW), None))
    db.conn.execute("INSERT INTO twins VALUES (?,?,?,?)", ("ev_twin", "ev_a", None, iso(NOW)))
    f = health.freshness(db, NOW - timedelta(hours=24))
    assert f["events"] == 1 and f["publisherToAppMin"]["p95"] == 30.0

