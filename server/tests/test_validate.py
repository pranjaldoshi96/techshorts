"""WP2: text, attribution, trim, validation gates, the retry ladder, holding and the rate limit. No network:
the LLM is a scripted fake and pages come from the 2026-11-11 edition fixture."""
import json
from datetime import timedelta

import pytest

from tools import fixture
from worker import attribution, db as db_mod, entail, llm, text, validate
from worker.db import DB, from_iso, iso

# A grounded, in-range card: S1 17 words, S2 21 (short 38), S3 17 (full 55).
DAYLIGHT_SRC = (
    "Daylight raised $80 million in a Series B round led by Benchmark, the startup said on Tuesday. The San "
    "Francisco company builds solar batteries for homes and plans to hire 200 engineers next year. Daylight will "
    "use the money to expand manufacturing in Texas, where it already employs 150 people. Investors expect demand "
    "for home storage to triple by 2030 as utilities raise prices and grid outages become more common across the "
    "United States.")
DAYLIGHT = dict(
    is_news=True, type="funding", relevance=3, importance=6, region="us", topics=["startups_funding", "hardware"],
    entities={"orgs": ["Daylight", "Benchmark"], "amounts": ["$80M"]},
    headline="Daylight raises $80M Series B led by Benchmark",
    s1="Daylight raised $80M in a Series B round led by Benchmark to build solar batteries for homes.",
    s2="The San Francisco company will expand manufacturing in Texas, where it already employs 150 people, and "
       "plans to hire 200 engineers.",
    s3="Investors expect home storage demand to triple by 2030, giving battery suppliers and installers a larger "
       "market.",
    notif_line="The startup plans to hire 200 engineers and expand manufacturing in Texas.")

HUANG_SRC = (
    "Nvidia CEO Jensen Huang admitted to an affair with a suspected Chinese spy at a staff meeting on Monday, "
    "according to people at the meeting. The FBI is looking into the relationship, and Nvidia's board has hired "
    "outside lawyers to review whether company secrets were shared. Huang said he regrets the affair and will keep "
    "running the company while the review continues. Shares of Nvidia fell 4% in early trading as investors "
    "weighed the risk to its leadership.")
HUANG = dict(
    is_news=True, type="news", relevance=3, importance=9, region="global", topics=["bigtech"], sensitive=True,
    headline="Jensen Huang admits affair with suspected Chinese spy",
    s1="Nvidia CEO Jensen Huang admitted to an affair with a suspected Chinese spy at a Monday staff meeting.",
    s2="The FBI is looking into the relationship, and the board has hired outside lawyers to review whether "
       "secrets were shared.",
    s3="Shares fell 4% in early trading as investors weighed the risk to the leadership of the chipmaker.",
    notif_line="The FBI is looking into the relationship and the board has hired outside lawyers.")


def out(base, **kw):
    return llm.WriteOutput.model_validate({**base, **kw})


def check(base, src, title="Original title", source="Example News", attr=None, **kw):
    return validate.check(out(base, **kw), src, title, source, attr)


def test_reference_cards_are_in_range_and_pass():
    for base, src in ((DAYLIGHT, DAYLIGHT_SRC), (HUANG, HUANG_SRC)):
        o = out(base)
        assert validate.word_count(validate.join(o.s1, o.s2)) in range(28, 43)
        assert validate.word_count(validate.join(o.s1, o.s2, o.s3)) in range(45, 63)
        v = validate.check(o, src, "Original title", "Example News")
        assert v.ok, v.feedback


# Grounding (WP2 AC)

def test_80_million_grounds_80m():
    assert validate.grounded("Daylight raises $80M", "Daylight raised $80 million") == (True, None)
    # No currency in the output.
    assert validate.grounded("Daylight gets 80M", "Daylight raised $80 million")[0]
    assert validate.grounded("A $1.4T valuation", "valued at $1.405 trillion")[0]  # within 0.5%


def test_pounds_do_not_ground_dollars():
    ok, bad = validate.grounded("Anthropic targets $1.4T valuation", "Anthropic targets valuation of up to £1.4tn")
    assert not ok and bad == "$1.4T"
    assert not validate.grounded("Raises $90M", "raised $80 million")[0]


def test_number_forms():
    vals = {(n.value, n.currency, n.percent) for n in validate.numbers(
        "$70-$78, 702%, seven founders, Rs 500 crore, 96 percent, 1,200")}
    assert {(70, "$", False), (78, "$", False), (702, None, True), (7, None, False), (5e9, "₹", False),
            (96, None, True), (1200, None, False)} <= vals
    assert validate.grounded("All 7 founders", "all seven founders")[0]


def test_headline_adding_china_fails():
    v = check(HUANG, HUANG_SRC, headline="Jensen Huang admits affair with suspected spy from China")
    assert not v.ok and v.reason == "ungrounded" and "'China'" in v.feedback[0]


def test_no_month_or_country_allowlist_but_source_name_is_allowed():
    assert not validate.grounded("Daylight raises $80M in November", DAYLIGHT_SRC)[0]
    assert validate.grounded("Example News: Daylight raises $80M", DAYLIGHT_SRC, allowed=["Example", "News"])[0]


def test_dropped_hedge_fails_with_hedge_dropped():
    v = check(HUANG, HUANG_SRC, headline="Jensen Huang admits affair with Chinese spy")
    assert not v.ok and v.reason == "hedge_dropped"
    assert v.feedback[0].startswith("Headline drops 'suspected'; keep the hedge.")
    assert check(HUANG, HUANG_SRC).ok
    # Attribution counts as keeping the hedge.
    assert validate.hedge_dropped("Report: Jensen Huang had affair with Chinese spy", HUANG_SRC) is None
    # Hedges far from anything the headline names don't apply.
    assert validate.hedge_dropped("Daylight raises $80M", DAYLIGHT_SRC + " The rival was reportedly sold.") is None
    # A headline naming the claimant attributes the claim.
    assert validate.hedge_dropped("iFixit teardown shows how the MacBook logo glows",
                                  "The logo is coated like a two-way mirror, according to iFixit, so it glows.") is None
    assert validate.hedge_dropped("SpaceX adds a swappable battery to Starlink Mini",
                                  "It lasts four hours on a charge, SpaceX claims, with a swappable battery.") is None
    # Only the opening binds the headline; a hedge on a later side fact doesn't.
    late = DAYLIGHT_SRC + " " + "Filler words about other things. " * 20 + "Daylight reportedly hired a CFO."
    assert check(DAYLIGHT, late).ok
    assert "hedge_dropped" in check(DAYLIGHT, "Daylight reportedly raised $80 million. " + DAYLIGHT_SRC).reasons
    # Citing a document isn't a hedge.
    src = ("CoreWeave's revenue soared 168%, according to a statement. CoreWeave owed $47 billion, according to "
           "the regulatory filing.")
    assert validate.hedge_dropped("CoreWeave revenue soars 168%", src) is None
    assert validate.hedge_dropped("CoreWeave revenue soars 168%", "CoreWeave's revenue soared 168%, according to "
                                  "people familiar with the results.")[0] == "according to"


# Trim and gates

def test_trim_word_ranges_and_feedback():
    o = out(DAYLIGHT)
    full, short, problems = validate.trim(o.s1, o.s2, o.s3)
    assert problems == [] and full.endswith("larger market.") and short.endswith("200 engineers.")
    long_s2 = o.s2 + " It also plans new offices in Texas and more hiring next year across the company."
    full, short, problems = validate.trim(o.s1, long_s2, o.s3)
    n = validate.word_count(validate.join(o.s1, long_s2))
    assert any(c == "too_long" and m.startswith(f"S1+S2 was {n} words; make it 36.") for c, m in problems)
    _, _, problems = validate.trim("Daylight raised money.", "It plans to hire.", "Investors are happy.")
    assert [c for c, _ in problems] == ["too_short", "too_short"]


def test_over_62_words_never_ships():
    o = out(DAYLIGHT, s3=DAYLIGHT["s3"] + " That shift could also help installers win contracts across many more "
                                          "states over the next decade.")
    v = validate.check(o, DAYLIGHT_SRC, "t", "Example News")
    assert not v.ok and v.reason == "too_long"


@pytest.mark.parametrize("headline, code", [
    ("Daylight raises $80M Series B led by Benchmark to build home batteries in Texas", "too_long"),
    ("Is Daylight the next big battery maker?", "headline_format"),
    ("Daylight raises $80M Series B.", "headline_format"),
    ("DAYLIGHT RAISES $80M SERIES B", "headline_format"),
])
def test_headline_gates(headline, code):
    v = check(DAYLIGHT, DAYLIGHT_SRC, headline=headline)
    assert not v.ok and code in v.reasons


def test_notif_line_limit():
    v = check(DAYLIGHT, DAYLIGHT_SRC, notif_line="The startup " + "plans to hire 200 engineers " * 5 + "in Texas.")
    assert not v.ok and "too_long" in v.reasons


def test_mine_hunting_is_not_first_person():
    s3 = "Navies can clear mine fields faster, giving battery suppliers and installers a larger market by 2030."
    assert "format" not in check(DAYLIGHT, DAYLIGHT_SRC + " mine fields", s3=s3).reasons


@pytest.mark.parametrize("s3, code", [
    ("This groundbreaking deal gives battery suppliers and installers a larger market by 2030 in Texas.",
     "banned_word"),
    ("Subscribe to the newsletter for more on battery suppliers and installers and their larger market.",
     "boilerplate"),
    ("We expect home storage demand to triple by 2030, giving battery suppliers a larger market overall.",
     "format"),
    ("The article says demand will triple by 2030, giving battery suppliers and installers a larger market.",
     "format"),
    ("**Investors** expect demand to triple by 2030, giving battery suppliers and installers a larger market.",
     "format"),
])
def test_summary_gates(s3, code):
    v = check(DAYLIGHT, DAYLIGHT_SRC, s3=s3)
    assert not v.ok and code in v.reasons


def test_select_picks_the_variant_combination_that_fits():
    long_s2 = DAYLIGHT["s2"] + " It also plans new offices across the United States next year with Benchmark."
    o = out(DAYLIGHT, s2=long_s2, s2_alt="The San Francisco company plans to hire 200 engineers in Texas.",
            headline="Daylight raises $80M Series B led by Benchmark to build home batteries in Texas",
            headline_alt="Daylight raises $80M Series B")
    validate.select(o)
    assert o.s2.startswith("The San Francisco company plans") and o.headline == "Daylight raises $80M Series B"
    full, short, problems = validate.trim(o.s1, o.s2, o.s3)
    assert problems == []
    # When the main versions already fit, they are kept even if alts exist.
    o = out(DAYLIGHT, s1_alt="Daylight raised $80M.", s3_alt="Suppliers win.")
    validate.select(o)
    assert o.s1 == DAYLIGHT["s1"] and o.s3 == DAYLIGHT["s3"]


def test_select_labels_analysis_but_not_attributed_opinion():
    o = validate.select(out(DAYLIGHT, type="analysis", headline="Home batteries are the next grid"))
    assert o.headline == "Analysis: Home batteries are the next grid"
    o = validate.select(out(DAYLIGHT, type="interview", headline="Thompson: Home batteries win"))
    assert o.headline == "Thompson: Home batteries win"
    long = "Home batteries are about to become the most important part of the American power grid"
    o = validate.select(out(DAYLIGHT, type="opinion", headline=long, headline_alt="Home batteries win"))
    assert o.headline == "Opinion: Home batteries win"


def test_prompt_lists_the_sources_hedges():
    msgs = llm.build_messages(ITEM, "Example News", HUANG_SRC)
    assert "suspected Chinese spy" in msgs[1]["content"].split("Hedged claims in this text")[1]
    assert "Hedged claims" not in llm.build_messages(ITEM, "Example News", DAYLIGHT_SRC)[1]["content"]


# Attribution (WP2 AC: decided by attribution.py regardless of the LLM)

def _fixture_item(title_start):
    items, _ = fixture.load()
    return next(i for i in items if i["title"].startswith(title_start))


def test_cramer_is_opinion_with_prefix_whatever_the_llm_says():
    item = _fixture_item("Jim Cramer says Anthropic")
    attr = attribution.detect(item["url"], item["title"], None, item["source_id"])
    assert attr.prefix == "Cramer:"
    src = ("CNBC's Jim Cramer said Anthropic's explosive growth could justify a record-breaking IPO. " * 6)
    o = out(DAYLIGHT, type="news", headline="Anthropic files IPO paperwork for record raise")
    attribution.apply(o, attr)
    assert o.type == "opinion"
    v = validate.check(o, src, item["title"], "CNBC Tech", attr)
    assert "attribution" in v.reasons
    assert "Source is an opinion by Jim Cramer; prefix 'Cramer:'." in v.feedback


@pytest.mark.parametrize("url, title, author, sid, prefix", [
    ("https://www.cnbc.com/2026/11/10/cramer-stocks-inflation.html", "Wall Street is too gloomy", None, "cnbc",
     "Cramer:"),
    ("https://www.theguardian.com/commentisfree/2026/nov/10/x", "AI is overhyped", "Jane Smith", "guardian",
     "Smith:"),
    ("https://www.theguardian.com/commentisfree/2026/nov/10/x", "Teen Mode | Robin Kirman", None, "guardian",
     "Kirman:"),
    ("https://www.nytimes.com/2026/11/10/opinion/ai.html", "The AI bubble", None, "nyt", "Opinion:"),
    ("https://www.lennysnewsletter.com/p/x", "Why PMs are suddenly valuable again | Tulsee Doshi (Head of Product, "
     "Gemini)", None, "lenny", "Doshi:"),
    ("https://www.bbc.co.uk/news/articles/x", "Zoe Kleinman: Nvidia CEO's affair will cause ripples", None, "bbc",
     "Kleinman:"),
    ("https://example.com/a", "Sam Altman says GPT-6 is close", None, "verge", "Altman:"),
])
def test_attribution_rules(url, title, author, sid, prefix):
    assert attribution.detect(url, title, author, sid).prefix == prefix


@pytest.mark.parametrize("url, title, sid", [
    ("https://techcrunch.com/2026/11/10/x", "Microsoft closes $69B Activision deal", "techcrunch"),
    ("https://example.com/a", "OpenAI CEO says GPT-6 is close", "verge"),
    ("https://blog.google/x", "Gemini Intelligence: New features, on more devices", "google_ai"),
    ("https://newsletter.pragmaticengineer.com/p/x", "The Pulse: Anthropic kills the coding interview", "pragmatic"),
])
def test_not_attributed(url, title, sid):
    assert attribution.detect(url, title, None, sid) is None


def test_fixture_attributions_are_the_expected_ones():
    items, _ = fixture.load()
    got = {i["title"][:30]: attribution.detect(i["url"], i["title"], None, i["source_id"]).prefix
           for i in items if i["eligible"] and attribution.detect(i["url"], i["title"], None, i["source_id"])}
    assert set(got.values()) == {"Cramer:", "Kirman:", "Islam:", "Kleinman:", "Imoh:", "Doshi:"}
    assert sum(v == "Cramer:" for v in got.values()) == 2


# Article text

def test_boilerplate_is_stripped_before_counting():
    paras = ["Subscribe to our newsletter for the latest news.", "Sign up now to support our journalism today.",
             "Daylight raised $80 million. " * 30]
    t = text.choose(paras, "")
    assert t.origin == "page" and "ubscribe" not in t.text and t.words == 120
    t = text.choose(["Become a subscriber for full access. " * 40, "Daylight raised $80 million in a round."], "")
    assert t.origin != "page" or t.words < 120


def test_page_is_capped_at_800_words():
    t = text.choose(["word " * 500, "more " * 500], "")
    assert t.words == 800 and t.origin == "page"


def test_short_page_falls_back_to_longer_feed_or_og():
    feed = "Feed sentence with real facts about the deal. " * 12
    t = text.choose(["Short page paragraph with only a few words here."], feed, "An og description.")
    assert t.origin == "feed"
    t = text.choose([], "Short feed text about the round.", "Daylight raised $80 million from Benchmark.")
    assert t.origin == "og+feed" and "Benchmark" in t.text and "Short feed text" in t.text
    # og:description that repeats the feed excerpt isn't doubled.
    t = text.choose([], "Daylight raised $80 million from Benchmark.", "Daylight raised $80 million from Benchmark.")
    assert t.text.count("Benchmark") == 1


def test_stratechery_fixture_text_has_no_subscriber_boilerplate():
    items, _ = fixture.load()
    strat = [i for i in items if i["source_id"] == "stratechery"]
    assert len(strat) == 10
    raw = [i for i in strat if "subscri" in i["feed_text"].lower()]
    assert raw, "fixture should contain the boilerplate this test is about"
    for i in strat:
        t = text.choose([], i["feed_text"], i["excerpt"])
        assert "subscri" not in t.text.lower() and "newsletter" not in t.text.lower()


def test_summary_with_subscribers_fails_unless_the_text_is_about_them():
    s3 = "Subscribers get the full analysis, giving battery suppliers and installers a larger market by 2030."
    assert "boilerplate" in check(DAYLIGHT, DAYLIGHT_SRC, s3=s3).reasons
    src = DAYLIGHT_SRC + " Netflix added 9 million subscribers."
    assert "boilerplate" not in check(DAYLIGHT, src, s3=s3).reasons


def test_og_description_and_fallback_extractor():
    page = ('<html><head><meta property="og:description" content="Daylight raised &#36;80 million."></head><body>'
            '<nav><p>Home News Tech Sport Weather and more links here</p></nav><article>'
            + "<p>Daylight raised $80 million in a Series B round led by Benchmark on Tuesday.</p>" * 10
            + "</article></body></html>")
    assert text.og_description(page) == "Daylight raised $80 million."
    paras = text.extract_paragraphs(page)
    assert paras and all("Weather" not in p for p in paras)


def test_lead_for_entailment_reaches_200_words():
    t = text.choose(["One short line here."] * 3 + ["Longer paragraph " * 60] * 3, "")
    assert 200 <= validate.word_count(t.lead()) < 300


# The ladder

class FakeClient:
    """Scripted replies: `writes` is a list of dicts/strings returned in order; entailment answers yes unless
    `entail_no` is set for that call index."""

    def __init__(self, writes, entail=("yes",)):
        self.writes, self.entail = list(writes), list(entail)
        self.calls = []

    def chat(self, model, messages, max_tokens=900, kind="write"):
        self.calls.append((kind, model, messages))
        if kind == "entail":
            v = self.entail.pop(0) if len(self.entail) > 1 else self.entail[0]
            return json.dumps({"entailed": v, "unsupported": "" if v == "yes" else "a record raise"})
        w = self.writes.pop(0)
        if isinstance(w, Exception):
            raise w
        return w if isinstance(w, str) else json.dumps(w)

    def drain(self):
        return []


CFG = {"llm": {"model": "primary", "fallback_model": "fallback", "max_items_per_tick": 50, "concurrency": 4,
               "daily_budget": 0},
       "fetch": {"user_agent": "test"}}
ITEM = {"url": "https://example.com/2026/daylight", "title": "Daylight raises $80M",
        "published_at": "2026-11-11T03:00:00Z"}


def test_feedback_retry_passes_at_rung_2():
    long_s2 = DAYLIGHT["s2"] + " It also plans new offices across the United States next year with Benchmark."
    client = FakeClient([{**DAYLIGHT, "s2": long_s2}, DAYLIGHT])
    r = llm.write_item(client, CFG, ITEM, "Example News", text.choose([DAYLIGHT_SRC], ""))
    assert r.status == "ok" and r.passed_at == 2 and r.entailed is True
    feedback = client.calls[1][2][-1]["content"]
    n = validate.word_count(validate.join(DAYLIGHT["s1"], long_s2))
    assert f"S1+S2 was {n} words; make it 36. S1 has 17 words (needs 16-21)" in feedback
    assert client.calls[1][2][-2]["role"] == "assistant"


def test_fallback_model_is_rung_3_then_failure():
    bad = {**DAYLIGHT, "headline": "Daylight raises $80M in November"}
    client = FakeClient([bad, bad, DAYLIGHT])
    r = llm.write_item(client, CFG, ITEM, "Example News", text.choose([DAYLIGHT_SRC], ""))
    assert r.status == "ok" and r.passed_at == 3 and r.model == "fallback"
    client = FakeClient([bad, bad, bad])
    r = llm.write_item(client, CFG, ITEM, "Example News", text.choose([DAYLIGHT_SRC], ""))
    assert r.status == "failed" and r.reason == "ungrounded" and len(r.steps) == 3


def test_entailment_failure_retries_with_the_unsupported_claim():
    client = FakeClient([DAYLIGHT, DAYLIGHT], entail=["no", "yes"])
    r = llm.write_item(client, CFG, ITEM, "Example News", text.choose([DAYLIGHT_SRC], ""))
    assert r.status == "ok" and r.passed_at == 2
    assert "does not support: a record raise" in client.calls[2][2][-1]["content"]


def test_entailment_only_for_top_or_push_candidates():
    assert entail.needed(out(DAYLIGHT, importance=6, relevance=1))
    assert entail.needed(out(DAYLIGHT, importance=3, relevance=2))
    assert entail.needed(out(DAYLIGHT, importance=3, relevance=1, breaking=True))
    assert not entail.needed(out(DAYLIGHT, importance=3, relevance=1))
    client = FakeClient([{**DAYLIGHT, "importance": 3, "relevance": 1}])
    r = llm.write_item(client, CFG, ITEM, "Example News", text.choose([DAYLIGHT_SRC], ""))
    assert r.status == "ok" and r.entailed is None and [c[0] for c in client.calls] == ["write"]


def test_bad_json_and_schema_errors_get_feedback():
    client = FakeClient(["not json at all", {**DAYLIGHT, "type": "gossip"}, DAYLIGHT])
    r = llm.write_item(client, CFG, ITEM, "Example News", text.choose([DAYLIGHT_SRC], ""))
    assert r.passed_at == 3 and [s.outcome for s in r.steps] == ["bad_json", "bad_json", "ok"]
    assert "type" in client.calls[2][2][-1]["content"]


def test_parse_drops_unknown_topics():
    o = llm.parse("Here you go: " + json.dumps({**DAYLIGHT, "topics": ["ai", "crypto"]}))
    assert o.topics == ["ai"]


def test_model_gone_skips_to_fallback():
    client = FakeClient([llm.ModelGone("410"), DAYLIGHT])
    r = llm.write_item(client, CFG, ITEM, "Example News", text.choose([DAYLIGHT_SRC], ""))
    assert r.status == "ok" and r.model == "fallback"


# Holding (WP2 AC: a no-text item is held with attempts and retry_at, never a permanent skip)

def _db_with(items):
    db = DB(":memory:")
    now = from_iso("2026-11-11T04:00:00Z")
    for it in items:
        db.insert_item({**it, "first_seen_at": now, "published_at": from_iso(it["published_at"]),
                        "feed_published_at": None, "status": "new"})
    db.commit()
    return db, now


def _sources():
    from worker import sources
    return sources.load()


def test_no_text_item_is_held_then_retried_then_dropped():
    srcs = _sources()
    item = {"url": "https://www.nytimes.com/2026/11/10/x.html", "url_canonical": "https://www.nytimes.com/x",
            "source_id": "nyt", "title": "Nvidia's board backs Huang", "feed_text": "Short.",
            "published_at": "2026-11-11T03:00:00Z"}
    db, now = _db_with([item])
    client = FakeClient([{**DAYLIGHT, "importance": 5}])
    thin = lambda it, src: text.choose([], it["feed_text"])
    stats = llm.write_pending(db, srcs, CFG, client, fetch_text=thin, now=now)
    row = db.item(item["url_canonical"])
    assert stats["thin"] == 1 and row["status"] == "held" and row["attempts"] == 1
    assert row["retry_at"] == iso(now + timedelta(minutes=30)) and row["hold_reason"] == "no_text"
    w = db.write_row(item["url_canonical"])
    assert w["status"] == "thin" and w["importance"] == 5 and w["headline"] is None   # classified, not shipped

    # Not due yet: nothing happens.
    assert not llm.write_pending(db, srcs, CFG, client, fetch_text=thin, now=now + timedelta(minutes=10))
    # Due: retried without another LLM call (already classified), held for +2 h.
    n_calls = len(client.calls)
    llm.write_pending(db, srcs, CFG, client, fetch_text=thin, now=now + timedelta(minutes=31))
    row = db.item(item["url_canonical"])
    assert row["attempts"] == 2 and row["retry_at"] == iso(now + timedelta(minutes=31, hours=2))
    assert len(client.calls) == n_calls
    # Third failure drops it.
    llm.write_pending(db, srcs, CFG, client, fetch_text=thin, now=now + timedelta(hours=3))
    row = db.item(item["url_canonical"])
    assert row["status"] == "dropped" and row["drop_rule"] == "held_out:no_text"


def test_held_item_is_written_when_text_arrives():
    srcs = _sources()
    item = {"url": "https://example.com/daylight", "url_canonical": "https://example.com/daylight",
            "source_id": "techcrunch", "title": "Daylight raises $80M", "feed_text": "Short.",
            "published_at": "2026-11-11T03:00:00Z"}
    db, now = _db_with([item])
    client = FakeClient([DAYLIGHT, DAYLIGHT])
    llm.write_pending(db, srcs, CFG, client, fetch_text=lambda it, s: text.choose([], "Short."), now=now)
    later = now + timedelta(minutes=30)
    stats = llm.write_pending(db, srcs, CFG, client, fetch_text=lambda it, s: text.choose([DAYLIGHT_SRC], ""),
                              now=later)
    assert stats["written"] == 1 and db.item(item["url_canonical"])["status"] == "written"
    w = db.write_row(item["url_canonical"])
    assert w["status"] == "ok" and w["headline"] == DAYLIGHT["headline"]
    assert 45 <= validate.word_count(w["summary"]) <= 62 and 28 <= validate.word_count(w["summary_short"]) <= 42


def test_failed_ladder_holds_instead_of_skipping():
    srcs = _sources()
    item = {"url": "https://example.com/d", "url_canonical": "https://example.com/d", "source_id": "verge",
            "title": "Daylight raises $80M", "feed_text": "", "published_at": "2026-11-11T03:00:00Z"}
    db, now = _db_with([item])
    bad = {**DAYLIGHT, "headline": "Daylight raises $80M in November"}
    llm.write_pending(db, srcs, CFG, FakeClient([bad] * 3), fetch_text=lambda i, s: text.choose([DAYLIGHT_SRC], ""),
                      now=now)
    row = db.item(item["url_canonical"])
    assert row["status"] == "held" and row["hold_reason"] == "ungrounded" and row["attempts"] == 1


def test_auth_error_leaves_items_untouched():
    srcs = _sources()
    item = {"url": "https://example.com/d", "url_canonical": "https://example.com/d", "source_id": "verge",
            "title": "Daylight raises $80M", "feed_text": "", "published_at": "2026-11-11T03:00:00Z"}
    db, now = _db_with([item])
    stats = llm.write_pending(db, srcs, CFG, FakeClient([llm.AuthError("401")]),
                              fetch_text=lambda i, s: text.choose([DAYLIGHT_SRC], ""), now=now)
    assert stats["auth_failed"] == 1 and db.item(item["url_canonical"])["status"] == "new"


def test_priority_tier1_first_then_newest():
    srcs = _sources()
    rows = [
        {"url": "u1", "url_canonical": "u1", "source_id": "simonw", "title": "a",
         "published_at": "2026-11-11T03:00:00Z"},
        {"url": "u2", "url_canonical": "u2", "source_id": "simonw", "title": "b",
         "published_at": "2026-11-11T03:30:00Z"},
        {"url": "u3", "url_canonical": "u3", "source_id": "techcrunch", "title": "c",
         "published_at": "2026-11-10T03:00:00Z"},
    ]
    db, _ = _db_with(rows)
    db.update_source("techcrunch", check_ok=1)
    order = [r["url_canonical"] for r in llm.priority(db.pending_writes(from_iso("2026-11-12T00:00:00Z")), srcs, db)]
    assert order == ["u3", "u2", "u1"]


# Rate limit (WP2 AC: ≤ 30/min in a 200-item mocked load)

class FakeClock:
    def __init__(self):
        import threading
        self.t = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.t

    def sleep(self, d):
        with self.lock:
            self.t += d


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


class FakeSession:
    """An OpenAI-compatible endpoint: write calls get DAYLIGHT, entail calls get yes. Records call times."""

    def __init__(self, clock, fail_first_with_429=False):
        import threading
        self.clock, self.times, self.lock = clock, [], threading.Lock()
        self.pending_429 = fail_first_with_429

    def post(self, url, json=None, timeout=None, headers=None):
        with self.lock:
            self.times.append(self.clock())
            if self.pending_429:
                self.pending_429 = False
                return FakeResponse(429, headers={"Retry-After": "7"})
        content = '{"entailed": "yes", "unsupported": ""}' if "ORIGINAL TITLE" in json["messages"][-1]["content"] \
            else __import__("json").dumps(DAYLIGHT)
        return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def _max_in_window(times, per=60.0):
    times = sorted(times)
    best, j = 0, 0
    for i, t in enumerate(times):
        while t - times[j] >= per:
            j += 1
        best = max(best, i - j + 1)
    return best


def test_rate_limit_holds_in_200_item_load():
    clock = FakeClock()
    session = FakeSession(clock)
    client = llm.Client("https://llm.test/v1", "k", llm.TokenBucket(30, 60.0, clock, clock.sleep), session,
                        clock=clock, sleep=clock.sleep)
    srcs = _sources()
    items = [{"url": f"https://example.com/{i}", "url_canonical": f"https://example.com/{i}", "source_id": "verge",
              "title": "Daylight raises $80M", "feed_text": "", "published_at": "2026-11-11T03:00:00Z"}
             for i in range(200)]
    db, now = _db_with(items)
    cfg = {**CFG, "llm": {**CFG["llm"], "fallback_model": None, "max_items_per_tick": 200, "concurrency": 6}}
    stats = llm.write_pending(db, srcs, cfg, client, fetch_text=lambda i, s: text.choose([DAYLIGHT_SRC], ""),
                              now=now)
    assert stats["written"] == 200
    assert len(session.times) == 400            # one write + one entailment per item
    assert _max_in_window(session.times) <= 30
    assert max(session.times) >= 13 * 60         # 400 calls at 30/min take over 13 minutes


def test_retry_after_is_honoured():
    clock = FakeClock()
    session = FakeSession(clock, fail_first_with_429=True)
    client = llm.Client("https://llm.test/v1", "k", llm.TokenBucket(30, 60.0, clock, clock.sleep), session,
                        clock=clock, sleep=clock.sleep)
    client.chat("primary", [{"role": "user", "content": "x"}])
    assert len(session.times) == 2 and session.times[1] - session.times[0] >= 7
    assert ("write", "primary", "http_429", 0) in client.drain()


# Fixture

def test_fixture_is_the_live_v1_edition():
    items, _ = fixture.load()
    assert len(items) == 223
    v1_keys = {"url", "title", "excerpt", "content", "source", "category", "publishedAt"}
    assert all(v1_keys <= set(i["v1"]) for i in items)
    assert {i["v1"]["category"] for i in items} <= {"Top", "AI", "Startups", "Dev", "Blogs"}
    eligible = [i for i in items if i["eligible"]]
    assert 150 <= len(eligible) <= 200
