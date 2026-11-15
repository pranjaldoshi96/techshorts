"""Fixes from the second 2026-11 shadow review: unwritten cards out of Top, fresh tier-1 news in Top, storylines on a
shared company, stale storyline cards, Dev and Startups scope, column typing at publish, realized updates and the
follow-up importance cap, the AI-subject topic rule and the repeat check against the chosen headline."""
from datetime import timedelta

import numpy as np

import test_cluster as tc
from worker import attribution, cluster, llm, publish_v1, rank, validate
from worker.db import iso

NOW = tc.EDITION_AT


def cand(eid, importance=6, type="news", topics=("ai",), age_h=2.0, n=1, vec=None, keys=(), orgs=(), **kw):
    c = rank.Candidate(
        id=eid, importance=importance, type=type, is_news=True, relevance=3, topics=list(topics), region="global",
        n_publishers=n, on_techmeme=False, hn_points=0, authority=1.0, published_at=NOW - timedelta(hours=age_h),
        lead_source=kw.pop("publisher", eid), lead_publisher=None,
        headline_vec=None if vec is None else np.asarray(vec, dtype=float) / np.linalg.norm(vec),
        keys=frozenset(keys), **kw)
    c.data = {"orgs": set(orgs), "parent_id": None}
    return c


TOP = rank.TABS["top"]


# Top

def test_an_unwritten_card_never_takes_a_top_rank_outside_an_outage():
    # The Verge column whose write failed (excerpt: newsletter boilerplate) led Top at score 1.12 vs 0.33.
    raw = cand("raw", importance=9, n=1, ai_text=False, lead_tier1=True)
    assert rank.eligible(raw, TOP, NOW) == "no_text"
    assert rank.eligible(raw, TOP, NOW, degraded=True) is None
    # Elsewhere it scores as importance 5 at most, so a written card on the same story outranks it.
    written = cand("w", importance=6, n=1)
    ai = rank.TABS["ai"]
    assert rank.cand_score(raw, ai, NOW) < rank.cand_score(written, ai, NOW)


def test_fresh_tier1_news_at_importance_5_enters_top_within_12_hours():
    fresh = cand("f", importance=5, age_h=4, lead_tier1=True)
    assert rank.eligible(fresh, TOP, NOW) is None
    assert rank.eligible(cand("old", importance=5, age_h=13, lead_tier1=True), TOP, NOW) == "importance"
    assert rank.eligible(cand("t2", importance=5, age_h=4, lead_tier1=False), TOP, NOW) == "importance"
    assert rank.eligible(cand("an", importance=5, age_h=4, lead_tier1=True, type="analysis"), TOP, NOW) == "type"
    assert rank.eligible(cand("raw", importance=5, age_h=4, lead_tier1=True, ai_text=False), TOP, NOW) == "no_text"
    how = cand("how", importance=5, age_h=4, lead_tier1=True, type="security_incident",
               title="How the ClickFix scam captures your computer")
    assert rank.eligible(how, TOP, NOW) == "importance"


def test_a_second_card_of_a_storyline_must_be_fresh():
    a = cand("a", importance=8, age_h=5, storyline="s", n=4)
    stale = cand("b", importance=10, age_h=30, storyline="s", n=2)
    fresh = cand("c", importance=6, age_h=10, storyline="s")
    out = [c.id for c, _ in rank.assemble([a, stale, fresh], rank.TABS["ai"], NOW)]
    assert out == ["a", "c"]
    stats = {}
    rank.assemble([a, stale], rank.TABS["ai"], NOW, stats=stats)
    assert stats.get("storyline_stale") == 1


# Storylines

def test_storylines_group_cards_about_the_same_company():
    # Three Anthropic IPO cards: orgs only, no person in common, so the non-org entity rule never linked them.
    v = [1, 0.9, 0]
    upcoming = cand("ev_0ff", vec=v, orgs=("anthropic",), keys={("p", "amodei")})
    targets = cand("ev_bd9", vec=[1, 0.8, 0.1], orgs=("anthropic",), keys={("a", "$", "1.4e+12")})
    investors = cand("ev_1e2", vec=[1, 0.7, 0.2], orgs=("anthropic",))
    other = cand("ev_nv", vec=[1, 0.75, 0.15], orgs=("nvidia",))
    dedup = rank.Dedup(alone=0.99, t=0.99)
    publish_v1.storylines([upcoming, targets, investors, other], dedup, 0.45, 0.5)
    assert upcoming.storyline == targets.storyline == investors.storyline
    assert other.storyline != upcoming.storyline
    assert len([c for c, _ in rank.assemble([upcoming, targets, investors], TOP, NOW)]) == rank.STORYLINE_CAP


def test_primary_orgs_are_the_first_named_org_of_each_member():
    members = [{"entities": '{"orgs": ["Anthropic", "Goldman Sachs"]}'}, {"entities": '{"orgs": []}'},
               {"entities": None}]
    assert publish_v1.primary_orgs(members) == {"anthropic"}


# Dev and Startups

def test_dev_takes_only_security_incidents_with_a_developer_angle():
    dev = rank.TABS["dev"]

    def sec(title, topics=("security",)):
        return rank.eligible(cand("x", type="security_incident", topics=topics, title=title), dev, NOW)
    for title in ("Workday outage locks millions of employees out of their accounts",
                  "How the ClickFix scam captures your computer", "United blocks AI flight booking agents",
                  "Helsing launches Clarity AI target platform for weapons systems"):
        assert sec(title) == "topic", title
    for title in ("Nightmare Eclipse drops Windows zero-day SignDrop", "Malicious npm packages steal AWS keys",
                  "Railway hack postmortem reveals attacker used unfiltered AI models"):
        assert sec(title) is None, title
    assert sec("Breach at Acme", topics=("security", "dev_tools")) is None


def test_startups_leaves_out_big_company_deals():
    st = rank.TABS["startups"]
    samsung = cand("s", type="acquisition", topics=("bigtech", "startups_funding"))
    assert rank.eligible(samsung, st, NOW) == "topic"
    buyout = cand("b", type="acquisition", topics=("startups_funding", "ai"))
    assert rank.eligible(buyout, st, NOW) is None
    assert rank.eligible(cand("f", type="funding", topics=("ai", "startups_funding")), st, NOW) is None


def test_publisher_self_promotion_is_held():
    assert publish_v1.self_promo("Inc42", "Inc42, DevX Launch RE:FRAME With ₹15 Cr Investment Pool For PropTech")
    assert publish_v1.self_promo("MediaNama", "MediaNama This Week: Top 10 Stories (7 November – 13 November 2026)")
    assert not publish_v1.self_promo("Inc42", "Razorpay launches IPO")
    # A company blog naming itself in a product post is the news itself.
    assert not publish_v1.self_promo("Hugging Face", "Pay for Hugging Face Hub with your Google Cloud Account")
    assert not publish_v1.self_promo("OpenAI", "Cogent and OpenAI build cyber defense factories for enterprises")


# Column typing at publish

def test_column_urls_are_analysis_at_publish_whatever_the_event_says():
    url = "https://www.theverge.com/column/1033943/jensen-huang-nvidia-china-spying-scandal-resignation"
    assert publish_v1.column_type("news", url, "An overview of Jensen Huang's messy days") == (
        "analysis", "Analysis: An overview of Jensen Huang's messy days")
    assert publish_v1.column_type("news", url, None) == ("analysis", None)
    assert publish_v1.column_type("analysis", url, "Analysis: x") == ("analysis", "Analysis: x")
    assert publish_v1.column_type("news", "https://www.theverge.com/news/1/x", "Huang resigns") == (
        "news", "Huang resigns")


def test_published_at_carries_a_material_update():
    c = cand("m", age_h=25, material_at=NOW - timedelta(hours=1))
    c.data.update(lead={"url_canonical": "https://x/1", "title": "t", "feed_text": "f"}, publishers=[],
                  headline="Anthropic's IPO lands", summary="s")
    a = publish_v1.article(c, "AI", None, None)
    assert a["publishedAt"] == iso(NOW - timedelta(hours=1))
    c.material_at = None
    assert publish_v1.article(c, "AI", None, None)["publishedAt"] == iso(NOW - timedelta(hours=25))


# Clustering

def test_a_planned_event_that_happened_is_a_material_update():
    assert cluster.realizes("Anthropic co-founders set to become billionaires in upcoming IPO",
                            "Anthropic's IPO lands as the embattled AI trade looks for a much-needed bright spot")
    assert not cluster.realizes("Nvidia CEO Jensen Huang resigns amid scandals", "Nvidia shares fall after exit")
    assert not cluster.realizes("Acme plans to launch X", "Acme could launch X next week")

    db = tc.new_db()
    first = tc._item("https://g/1", [1, 0, 0], source_id="guardian", people=["Amodei"],
                     pub=NOW - timedelta(hours=20))
    landed = tc._item("https://t/2", [1, 0, 0], source_id="techcrunch", people=["Amodei"])
    landed.headline = "Anthropic's IPO lands with a $900B valuation"
    tc._db_items(db, [first, landed])
    store = tc._store_with(db, [first])
    e = store.events[cluster.event_id(first.url)]
    e.row["headline"] = "Anthropic set to list in upcoming IPO"
    cluster.join(store, e, landed, 1.0, "cosine")
    assert landed.material_update and "material_update" in e.dirty
    assert e.row["last_material_update_at"] == iso(landed.published_at)

    # A member that joined earlier is caught on the next refresh.
    db = tc.new_db()
    first = tc._item("https://g/1", [1, 0, 0], source_id="guardian", people=["Amodei"],
                     pub=NOW - timedelta(hours=20))
    landed = tc._item("https://t/2", [1, 0, 0], source_id="techcrunch", people=["Amodei"])
    landed.headline = "Anthropic's IPO lands with a $900B valuation"
    tc._db_items(db, [first, landed])
    store = tc._store_with(db, [first])
    e = store.events[cluster.event_id(first.url)]
    cluster.join(store, e, landed, 1.0, "cosine")
    assert not landed.material_update
    e.row["headline"] = "Anthropic set to list in upcoming IPO"
    cluster.refresh(store, e, lambda s: True)
    assert landed.material_update and "material_update" in (e.row.get("rewrite_reason") or "")


def test_a_late_member_without_a_material_update_is_capped_for_event_importance():
    def raw(material):
        db = tc.new_db()
        first = tc._item("https://g/1", [1, 0, 0], source_id="guardian", people=["Huang"], imp=9,
                         pub=NOW - timedelta(hours=30))
        late = tc._item("https://t/2", [1, 0, 0], source_id="techcrunch", people=["Huang"], imp=9,
                        material=material)
        tc._db_items(db, [first, late])
        store = tc._store_with(db, [first])
        e = store.events[cluster.event_id(first.url)]
        cluster.join(store, e, late, 1.0, "cosine")
        cluster.refresh(store, e, lambda s: True)
        return e.row["importance_raw"]
    assert raw(False) == 7.5        # median(9, min(9, 6))
    assert raw(True) == 9


# Writing

class _Out:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_ai_subject_gets_the_ai_topic_and_relevance_2():
    out = _Out(type="news", topics=["science"], relevance=1, headline="Exam top scorer admits cheating")
    attribution.apply(out, None, "https://www.theguardian.com/world/2026/x",
                      "South Korea exam top scorer admits using generative AI to cheat")
    assert out.topics == ["science", "ai"] and out.relevance == 2
    out = _Out(type="news", topics=["science"], relevance=1, headline="Giant fungus found in China")
    attribution.apply(out, None, "https://x/y", "Giant fungus discovered in China grows up to 20 inches tall")
    assert out.topics == ["science"] and out.relevance == 1
    # "said" or "aid" never count as AI.
    assert not attribution.ai_subject("Officials said the aid package is ready")


def test_prompt_names_realized_plans_as_material_updates():
    assert "has now happened" in llm.PROMPT and "cheating with AI" in llm.PROMPT


def test_select_checks_repetition_against_the_headline_it_picks():
    # The long headline fails the length rule, so the short one ships; its words are the s1's words, so the
    # s1 that adds facts wins.
    out = _Out(type="news", headline="Meituan releases LongCat-2.5, a 1.6-trillion-parameter agent model for "
                                     "enterprise developers today",
               headline_alt="Meituan releases 1.6-trillion-parameter agent model LongCat-2.5", headline_short="",
               s1="Meituan releases LongCat-2.5, a 1.6-trillion-parameter agent model.",
               s1_alt="The Chinese delivery giant says the open model beats rivals on agent tasks at half the cost.",
               s1_long="", s2="The model is free for commercial use and runs on Meituan's own cloud in China today.",
               s2_alt="", s2_long="",
               s3="Developers in China get a cheaper large agent model for building shopping assistants.",
               s3_alt="", s3_long="")
    validate.select(out)
    assert out.headline.startswith("Meituan releases 1.6")
    assert not validate.repeat_problems(out.headline, out.s1, out.s2, out.s3)


# Tools

def test_requeue_gives_held_out_tier1_items_one_more_try():
    from tools import requeue
    db = tc.new_db()
    t1 = next(s for s in tc.SRCS.sources if s.tier == 1)
    t2 = next(s for s in tc.SRCS.sources if s.tier == 2)
    for u, sid, pub in (("https://a/1", t1.id, NOW - timedelta(hours=3)), ("https://a/2", t2.id, NOW - timedelta(hours=3)),
                        ("https://a/3", t1.id, NOW - timedelta(hours=20))):
        db.insert_item({"url_canonical": u, "url": u, "source_id": sid, "title": u, "first_seen_at": pub,
                        "published_at": pub, "status": "dropped"})
        db.conn.execute("UPDATE items SET drop_rule = 'held_out:hedge_dropped', attempts = 2 WHERE url_canonical = ?",
                        (u,))
    done = requeue.requeue(db, tc.SRCS, NOW, 12)
    assert [d[0] for d in done] == ["https://a/1"]
    row = db.item("https://a/1")
    assert row["status"] == "held" and row["attempts"] == 2 and row["retry_at"] == iso(NOW)
    # The next failure drops it again (attempts 3 > the content schedule's 1).
    assert db.hold("https://a/1", "hedge_dropped", NOW) == "dropped"


def test_reselect_leaves_rewritten_and_clean_cards_alone():
    from tools import reselect
    row = {"text_from": "rewrite", "ev_headline": "Acme rejects plea", "ev_s1": "Acme rejects plea.", "s2": "x",
           "s3": "y"}
    assert reselect.reselect(row, tc.SRCS) == (None, "fine")
    row.update(text_from="https://x/1", ev_s1="The court in Bengaluru said the filing came too late for review.")
    assert reselect.reselect(row, tc.SRCS) == (None, "fine")
