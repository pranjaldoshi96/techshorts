"""The 2026-11-15 review of the VPS test run: spice and push alerts, truth checks (tense, sourcing, inference),
clustering by development with a judge, signals that become cards, live HN points and the provider router.

No network. Each test names the card or event from that run that it guards against.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from worker import cluster, embed, entail, health, llm, prefilter, publish_v1, rank, sources as sources_mod
from worker import validate
from worker.db import DB, iso

NOW = datetime(2026, 11, 15, 17, 0, tzinfo=timezone.utc)
SRCS = sources_mod.load()

SEBI_TITLE = "Sebi to issue AI usage guidelines for capital markets, focus on investor protection"
SEBI_SRC = ("Sebi will soon issue detailed guidelines on the use of artificial intelligence in capital markets, "
            "chairman Tuhin Kanta Pandey said on Saturday. The proposed rules would cover disclosures, the "
            "accountability of regulated entities and human oversight of critical decisions. In June 2025 the "
            "regulator floated a consultation paper proposing principles for responsible AI use. A working group "
            "is also examining ways to attract startups to the Innovators Growth Platform, Pandey said.")
SEBI = dict(
    is_news=True, type="policy", relevance=3, importance=5, spice=4, region="in", topics=["policy", "ai", "india"],
    headline="Sebi to issue AI guidelines for capital markets",
    s1="Chairman Tuhin Kanta Pandey said on Saturday the proposed rules would cover disclosures and human oversight.",
    s2="In June 2025 the regulator floated a consultation paper proposing principles for responsible AI use in "
       "capital markets.",
    s3="A working group is also examining ways to attract startups to the Innovators Growth Platform, Pandey said.",
    notif_title="Sebi to issue AI rules",
    notif_line="Sebi's proposed AI guidelines would keep critical market decisions under human oversight.")


def out(base, **kw):
    return llm.WriteOutput.model_validate({**base, **kw})


def check(base, src=SEBI_SRC, title=SEBI_TITLE, source="YourStory", **kw):
    return validate.check(out(base, **kw), src, title, source, None, None, src)


# Tense and certainty

def test_reference_card_passes():
    v = check(SEBI)
    assert v.ok, v.feedback


def test_sebi_to_issue_must_not_become_sebi_issues():
    # The shipped card: "Sebi issues AI usage guidelines for capital markets" from "Sebi to issue ...".
    v = check(SEBI, headline="Sebi issues AI usage guidelines for capital markets")
    assert not v.ok and "tense_drift" in v.reasons
    assert "to issue" in next(f for f in v.feedback if "still to come" in f)
    # The alert's lines often state another, present fact; their tense is the fact-checker's rule 2.
    assert "tense or certainty" in entail.PROMPT and "ALERT TITLE" in entail.build("t", "x", "h", "s", notif_title="a")
    assert check(SEBI, headline="Sebi plans AI usage rules for capital markets").ok
    assert check(SEBI, headline="Sebi will issue AI usage rules for capital markets").ok


@pytest.mark.parametrize("title,marker", [
    (SEBI_TITLE, "to issue"),
    ("Musk to testify in OpenAI trial", "to testify"),
    ("Govt to ban deepfake apps", "to ban"),
    ("Apple plans to launch smart glasses next year", "plans to"),
    ("Anthropic is expected to price its IPO on Wednesday", "is expected"),
    ("Tesla will open Cybercab orders", "will"),
    # Too weak or too ambiguous to bind a headline on their own (the fact-checker rules on these).
    ("OpenAI could raise $40B", None),
    ("'SNL' parodies Jensen Huang: 'May I pleasure you in an industry secret?'", None),
    ("Grim simulation shows what Earth would be like if life never existed", None),
    ("Work to replace old copper wire led to the FAA's ground stop", None),
    ("Nvidia board urged to consider 'potential return of Jensen Huang'", None),
    # Not plans: an infinitive after a finite verb, a preposition, a how-to.
    ("Altimeter, Atreides urge Nvidia board to keep doors open for Jensen Huang's return", None),
    ("Apple brings Siri to smart glasses", None),
    ("Meta sells stake to private investors", None),
    ("How to build an agent", None),
    ("Italy fines Meta 805m euros over Android photo access", None),
])
def test_only_a_plain_plan_in_the_title_binds_the_headline(title, marker):
    assert validate.future_marker(title, validate.PLAN_MARK) == marker


def test_a_headline_keeps_the_plan_in_many_words():
    for h in ("Alcon reportedly interested in making a Blade Runner game", "Apple's AI glasses tipped for WWDC",
              "Sebi wants AI rules for brokers", "Musk could testify in OpenAI trial", "Govt may ban deepfake apps"):
        assert validate.tense_problem(h, "Sebi to issue AI usage guidelines") is None, h
    assert validate.tense_problem("Sebi issues AI usage guidelines", "Sebi to issue AI usage guidelines") == "to issue"


# Inference, scale words and sourcing

def test_an_inference_the_source_never_drew_is_rejected():
    # "The founder's personal spending may have contributed to the company's payroll crisis." (CNBC drew no link.)
    v = check(SEBI, s3="The delay may have contributed to confusion among brokers about which AI tools are allowed.")
    assert not v.ok and "speculation" in v.reasons
    # The source's own hedged wording is fine.
    src = SEBI_SRC + " Analysts said the delay may have contributed to confusion among brokers."
    v = check(SEBI, src=src,
              s3="Analysts said the delay may have contributed to confusion among brokers about the rules.")
    assert "speculation" not in v.reasons


def test_a_superlative_must_be_the_sources_own():
    # "the record fine" from BBC's "among the largest".
    v = check(SEBI, s2="In June 2025 the regulator floated a record consultation paper proposing principles for "
                       "responsible AI use.")
    assert not v.ok and "superlative" in v.reasons


FOUNDATION_SRC = ("The founder of Foundation Future Industries borrowed $4.6 million from the company to buy a "
                  "home, according to people familiar with the matter. The robotics startup missed payroll this "
                  "month, the people said. The company did not respond to a request for comment. Its backers "
                  "include several well known venture firms, and it raised a large round last year for humanoid "
                  "robots built for factory work.")
FOUNDATION = dict(
    is_news=True, type="news", relevance=3, importance=5, spice=8, region="us", topics=["startups_funding"],
    sensitive=True, headline="Foundation Future Industries founder borrowed $4.6 million to buy home",
    s1="The robotics startup missed payroll this month and did not respond to a request for comment on the loan.",
    s2="Its backers include several well known venture firms, and it raised a large round last year for "
       "humanoid robots.",
    s3="The company builds humanoid robots for factory work and raised a large round last year.",
    notif_title="Founder borrowed $4.6 million",
    notif_line="The robotics startup missed payroll this month.")


def test_unnamed_sourcing_must_be_carried_by_the_headline_or_s1():
    v = validate.check(out(FOUNDATION), FOUNDATION_SRC, "Founder borrowed millions", "CNBC Tech", None, None,
                       FOUNDATION_SRC)
    assert "sourcing_dropped" in v.reasons
    for fix in ({"headline": "Foundation founder borrowed $4.6 million for home, sources say"},
                {"s1": "People familiar with the matter said the robotics startup also missed payroll this month."},
                {"headline": "CNBC: Foundation founder borrowed $4.6 million to buy home"}):
        v = validate.check(out(FOUNDATION, **fix), FOUNDATION_SRC, "Founder borrowed millions", "CNBC Tech", None,
                           None, FOUNDATION_SRC)
        assert "sourcing_dropped" not in v.reasons, fix
    # A company's own announcement needs no such label.
    assert validate.sourcing_dropped("Tesla opens Cybercab pre-orders", "Select customers can reserve it.",
                                     "Tesla said on its X account that select customers can reserve one.") is None


def test_s1_must_not_open_with_the_headlines_words():
    # "Apple revamps Siri AI for first smart glasses ahead of WWDC" / "Apple revamps Siri AI for first smart ..."
    probs = validate.repeat_problems(
        "Apple revamps Siri AI for first smart glasses ahead of WWDC",
        "Apple revamps Siri AI as it prepares glasses with cameras, microphones and a new chip, Bloomberg reports.",
        "The glasses are set to be announced next year.")
    assert [c for c, _ in probs] == ["repeats_headline"]
    assert not validate.repeat_problems(
        "Apple revamps Siri AI for first smart glasses ahead of WWDC",
        "Bloomberg reports the glasses will carry cameras, microphones and a new chip built for voice control.",
        "The glasses are set to be announced next year.")


def test_report_label_is_not_added_to_a_companys_own_claim():
    # "Report: Tesla sells Cybercab ..." on an offer Tesla posted itself (the text said "bold claims").
    o = out(SEBI, headline="Tesla opens Cybercab orders at $29,990", headline_alt="", headline_short="")
    scope = validate.hedge_scope("Tesla is inviting some people to buy a Cybercab",
                                 "Tesla is tripling down on its bold claims of an autonomous future with the offer.")
    validate.select(o, hedge_source=scope)
    assert not o.headline.startswith("Report:")


def test_fact_check_sees_every_line_and_the_whole_text():
    prompt = entail.build("Title", "Source text.", "Headline", "S1.", "S2.", "S3.", "Alert title", "Alert body.",
                          "New York Post")
    assert "published by New York Post" in prompt and '"New York Post reports"' in prompt
    for part in ("HEADLINE: Headline", "S1: S1.", "S2: S2.", "S3: S3.", "ALERT TITLE: Alert title",
                 "ALERT: Alert body."):
        assert part in prompt
    for rule in ("tense or certainty", "cause, motive", "superlative", "allegation", "digest", "teaser"):
        assert rule in entail.PROMPT
    e = entail.parse('{"entailed": "no", "line": "S3", "unsupported": "The source says expected, not priced."}')
    assert not e.ok and "(S3)" in e.feedback()


def test_prompts_carry_the_owners_rules():
    for text in (llm.PROMPT, llm.REWRITE_PROMPT):
        assert '"spice": 1-10' in text and "notif_title" in text and "110 characters" in text
        assert "<<spice>>" not in text and "<<importance>>" not in text
    assert "must never become" in llm.PROMPT and "never your own" in llm.PROMPT
    # Every step of the importance scale has an example (the test run had 2 items at 7 and 15 at 8).
    for n in range(3, 11):
        assert f"= {n}," in llm.IMPORTANCE


def test_newsletter_digests_are_dropped_before_the_llm():
    assert prefilter.check("[AINews] The No AI Slop Manifesto", "https://www.latent.space/p/ainews-x") == "title_roundup"
    assert prefilter.check("Import AI 480: Self-sustaining space economies", "https://importai.substack.com/p/x")
    assert "roundup" in rank.NEVER_TYPES and "roundup" in llm.TYPES


# Providers

class FakeHTTP:
    def __init__(self, status=200, content='{"ok": true}'):
        self.status, self.content, self.bodies = status, content, []

    def post(self, url, json=None, timeout=None, headers=None):
        self.bodies.append((url, json))
        status, content = self.status, self.content

        class R:
            status_code = status
            headers = {}

            def json(self):
                return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 5,
                                                                                   "completion_tokens": 2}}
        return R()


def _router(hub_status=200):
    hub, nv = FakeHTTP(hub_status, '{"from": "hub"}'), FakeHTTP(200, '{"from": "nvidia"}')
    sleep = lambda s: None  # noqa: E731
    clients = {"hub": llm.Client("https://hub.test/v1", "hk", llm.TokenBucket(600, 60.0), hub, sleep=sleep,
                                 params={"reasoning_effort": "low"}),
               "nvidia": llm.Client("https://nv.test/v1", "nk", llm.TokenBucket(600, 60.0), nv, sleep=sleep)}
    return llm.Router(clients, "nvidia", backup="nvidia:nemotron"), hub, nv


def test_router_sends_each_model_to_its_provider_with_its_params():
    r, hub, nv = _router()
    assert json.loads(r.chat("hub:openai/gpt", [{"role": "user", "content": "x"}])) == {"from": "hub"}
    url, body = hub.bodies[0]
    assert url == "https://hub.test/v1/chat/completions" and body["model"] == "openai/gpt"
    assert body["reasoning_effort"] == "low" and "chat_template_kwargs" not in body
    assert json.loads(r.chat("nemotron", [{"role": "user", "content": "x"}])) == {"from": "nvidia"}
    assert nv.bodies[0][1]["chat_template_kwargs"] == {"enable_thinking": False}
    # health.json sees who answered.
    assert [(kind, model, outcome) for kind, model, outcome, *_ in r.drain()] == [
        ("write", "hub:openai/gpt", "ok"), ("write", "nvidia:nemotron", "ok")]
    assert r.key == "nk"


@pytest.mark.parametrize("status", [401, 404, 500, 429])
def test_router_falls_back_to_the_backup_model_when_the_endpoint_fails(status):
    r, hub, nv = _router(status)
    assert json.loads(r.chat("hub:openai/gpt", [{"role": "user", "content": "x"}], tries=1)) == {"from": "nvidia"}
    assert nv.bodies[0][1]["model"] == "nemotron"
    outcomes = [(model, outcome) for _, model, outcome, *_ in r.drain()]
    assert ("hub:openai/gpt", f"http_{status}") in outcomes and ("nvidia:nemotron", "ok") in outcomes


def test_keys_come_from_a_named_file_and_are_never_logged(tmp_path, monkeypatch, caplog):
    f = tmp_path / "hub_key"
    f.write_text("secret-hub-key\n")
    monkeypatch.delenv("HUB_API_KEY", raising=False)
    monkeypatch.setenv("HUB_API_KEY_FILE", str(f))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert llm.api_key("HUB_API_KEY") == "secret-hub-key"
    cfg = {"llm": {"model": "hub:m", "timeout_s": 30, "providers": {
        "hub": {"base_url": "https://hub.test/v1", "key_env": "HUB_API_KEY"},
        "nvidia": {"base_url": "https://nv.test/v1", "key_env": "NVIDIA_API_KEY_MISSING"}}}}
    with caplog.at_level("INFO"):
        r = llm.make_router(cfg)
    assert set(r.clients) == {"hub"} and "secret-hub-key" not in caplog.text
    assert llm.role_model({"llm": {"model": "a", "judge_model": "b"}}, "judge") == "b"
    assert llm.role_model({"llm": {"model": "a"}}, "triage") == "a"


# Screening

class ScriptedClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def chat(self, model, messages, **kw):
        self.calls.append((model, messages[-1]["content"], kw))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def _rows(n):
    return [{"url_canonical": f"https://x.test/{i}", "source_id": "nypost_business", "title": f"Title {i}",
             "feed_text": "Some text."} for i in range(n)]


def test_triage_drops_only_what_the_model_names_and_fails_open():
    cfg = {"llm": {"model": "w", "triage_model": "t"}}
    c = ScriptedClient({"drop": [{"n": 2, "why": "offtopic"}, {"n": 5, "why": "nonsense"}, {"n": 99, "why": "dull"}]})
    got = llm.triage_batch(c, cfg, _rows(8), SRCS.by_id())
    assert got == {"https://x.test/1": "offtopic", "https://x.test/4": "dull"}
    assert c.calls[0][0] == "t" and "[New York Post Business] Title 0" in c.calls[0][1]
    assert llm.triage_batch(ScriptedClient(llm.LLMError("down")), cfg, _rows(8), SRCS.by_id()) == {}
    assert llm.triage_batch(ScriptedClient("not json"), cfg, _rows(8), SRCS.by_id()) == {}
    # A reply that drops nearly everything misread the task.
    everything = {"drop": [{"n": i, "why": "dull"} for i in range(1, 9)]}
    assert llm.triage_batch(ScriptedClient(everything), cfg, _rows(8), SRCS.by_id()) == {}


# Clustering by development

def _item(u, vec, title=None, pub=NOW, people=("Jensen Huang",), type="news", source_id="techcrunch", **kw):
    it = cluster.Item(url=u, source_id=source_id, title=title or u, published_at=pub, status=kw.pop("status", "ok"),
                      type=type, is_news=True, relevance=3, importance=kw.pop("imp", 6), breaking=False,
                      sensitive=False, region="global", topics=["bigtech"], entities={"people": list(people)},
                      pick="new", material_update=False, text_words=400, vec=embed.normalize(vec),
                      headline=title or u, summary="Summary.", text="Text of " + (title or u),
                      spice=kw.pop("spice", None))
    it.keys = cluster.entity_keys(it.entities)
    return it


S = cluster.Settings(t_cand=0.5, t_join=0.47, t_signal=0.45)


def _db():
    db = DB(":memory:")
    for s in SRCS.sources:
        db.update_source(s.id, check_ok=1)
    return db


def _judge(rulings, calls):
    def judge(title, text, events):
        calls.append((title, [e[0] for e in events]))
        return {eid: rulings.get((title, eid), rulings.get(title, "different")) for eid, _, _ in events}
    return judge


def test_a_saga_is_split_by_development_not_chained_on_a_shared_name():
    """ev_acaf00569e took 13 members at cosine 0.48-0.70 because every report named Jensen Huang: the investors'
    letter, Megaspeed's statement, a petition, features. With the judge each development is its own event."""
    db, calls = _db(), []
    letter = _item("https://et.test/letter", [1, 0, 0], "Altimeter, Atreides urge Nvidia board to keep door open")
    letter2 = _item("https://cnbc.test/letter", [0.8, 0.6, 0], "Nvidia board urged to consider return of Huang",
                    source_id="cnbc", pub=NOW + timedelta(minutes=10))
    megaspeed = _item("https://cnbc.test/megaspeed", [0.8, 0.5, 0.2], "Megaspeed says Alice Huang left in 2025",
                      source_id="cnbc", pub=NOW + timedelta(hours=20))
    mobility = _item("https://tc.test/mobility", [0.6, 0.1, 0.75], "The Nvidia backup plan for China's robotaxis")
    eid = cluster.event_id(letter.url)
    rulings = {letter2.title: "same", megaspeed.title: "followup", mobility.title: "different"}
    store = cluster.Store(db, SRCS, S, NOW + timedelta(hours=21), _judge(rulings, calls))
    assert cluster.assign(store, letter) == (eid, "new")
    assert cluster.assign(store, letter2) == (eid, "judge")
    mid, how = cluster.assign(store, megaspeed)
    assert how == "followup_judge" and mid != eid and store.events[mid].row["parent_id"] == eid
    oid, how = cluster.assign(store, mobility)
    assert how == "new" and oid not in (eid, mid) and store.events[oid].row["parent_id"] is None
    assert len(store.events[eid].members) == 2          # the letter's two reports and nothing else
    # A second report of Megaspeed's statement joins the follow-up; the saga's root stays the parent.
    bbc = _item("https://bbc.test/megaspeed", [0.4, 0.8, 0.4], "Suspected China spy left in 2025, Megaspeed says",
                source_id="bbc", pub=NOW + timedelta(hours=20, minutes=30))
    rulings[(bbc.title, mid)] = "same"
    rulings[(bbc.title, eid)] = "followup"
    assert cluster.assign(store, bbc) == (mid, "judge")
    # Rulings are cached: asking again costs no call.
    n = len(calls)
    assert store.relations(bbc.url, bbc.title, "", [store.events[mid]]) == {mid: "same"} and len(calls) == n


def test_without_a_judge_the_entity_rule_still_applies_and_identical_text_needs_no_ruling():
    db, calls = _db(), []
    a = _item("https://a.test/1", [1, 0, 0])
    store = cluster.Store(db, SRCS, S, NOW, _judge({}, calls))
    cluster.assign(store, a)
    same = _item("https://b.test/1", [1, 0.05, 0], source_id="verge")
    assert cluster.assign(store, same)[1] == "same_text" and not calls
    plain = cluster.Store(_db(), SRCS, S, NOW)
    cluster.assign(plain, a)
    assert cluster.assign(plain, _item("https://c.test/1", [0.8, 0.6, 0], source_id="verge"))[1] == "cosine"


def test_an_item_waits_when_the_judge_cannot_be_asked():
    def down(title, text, events):
        raise llm.LLMError("timeout")
    store = cluster.Store(_db(), SRCS, S, NOW, down)
    cluster.assign(store, _item("https://a.test/1", [1, 0, 0]))
    assert cluster.assign(store, _item("https://b.test/1", [0.8, 0.6, 0], source_id="verge")) == (None, "judge_failed")
    assert len(store.events) == 1


def _stored(db, items, now=NOW):
    for it in items:
        db.insert_item({"url_canonical": it.url, "url": it.url, "source_id": it.source_id, "title": it.title,
                        "first_seen_at": now, "published_at": it.published_at, "status": "written"})


def test_the_event_clock_is_its_first_hard_news_report():
    # ev_3effc3d405: "Meet the seven co-founders of Anthropic" from the day before set the IPO card's clock to 29 h.
    db = _db()
    report = _item("https://cnbc.test/ipo", [1, 0, 0], "Anthropic's IPO lands", spice=7)
    profile = _item("https://cnbc.test/founders", [0.9, 0.3, 0], "Meet the seven co-founders of Anthropic",
                    pub=NOW - timedelta(hours=24), type="explainer", source_id="cnbc")
    _stored(db, [report, profile])
    store = cluster.Store(db, SRCS, S, NOW)
    eid, _ = cluster.create(store, report)
    cluster.join(store, store.events[eid], profile, 0.9, "judge")
    cluster.refresh(store, store.events[eid], lambda s: True)
    row = db.one("SELECT published_at, spice FROM events WHERE id = ?", eid)
    assert row["published_at"] == iso(NOW) and row["spice"] == 7


def _signal(db, url, source_id, title, vec, extra=None, at=NOW):
    db.insert_item({"url_canonical": url, "url": url, "source_id": source_id, "title": title, "first_seen_at": at,
                    "published_at": at, "feed_published_at": at, "status": "signal", "extra": extra or {}})
    db.conn.execute("INSERT INTO embeddings VALUES (?,?,?,?,?,?,?)",
                    (url, "query", "t", 3, None, embed.to_blob(embed.normalize(vec)), iso(at)))


def test_a_signal_attaches_to_one_event_and_only_when_it_is_about_it():
    """Techmeme's "Bessent urges Nvidia to hire an outside CEO" attached by cosine to both the investors'-letter
    card and the "Asian governments react" card, and each got the Techmeme boost."""
    db, calls = _db(), []
    letter = _item("https://et.test/letter", [1, 0, 0], "Altimeter, Atreides urge Nvidia board to keep door open")
    _stored(db, [letter])
    bessent = "Scott Bessent urges Nvidia to hire an outside CEO"
    _signal(db, "https://techmeme.test/p8", "techmeme", bessent, [0.9, 0.3, 0])
    store = cluster.Store(db, SRCS, S, NOW, _judge({bessent: "followup"}, calls))
    lid, _ = cluster.create(store, letter)
    assert cluster.attach_signals(store) == 0 and len(calls) == 1
    assert cluster.attach_signals(store) == 0 and len(calls) == 1          # ruled once
    # CNBC's report of Bessent's call arrives: the signal now attaches there, once.
    report = _item("https://cnbc.test/bessent", [0.88, 0.35, 0], "Bessent calls for dramatic shakeup at Nvidia",
                   source_id="cnbc")
    _stored(db, [report])
    store.judge = _judge({(bessent, cluster.event_id(report.url)): "same"}, calls)
    bid, _ = cluster.create(store, report)
    assert cluster.attach_signals(store) == 1
    rows = db.q("SELECT event_id, how FROM event_signals")
    assert [(r["event_id"], r["how"]) for r in rows] == [(bid, "judge")]
    for e in list(store.events.values()):
        cluster.refresh(store, e, lambda s: True)
    assert store.events[bid].row["on_techmeme"] == 1 and store.events[lid].row["on_techmeme"] == 0


def test_twins_ruled_different_developments_are_not_merged_or_hidden():
    db, calls = _db(), []
    a = _item("https://cnbc.test/megaspeed", [1, 0, 0], "Megaspeed says Alice Huang left in 2025", source_id="cnbc")
    b = _item("https://decoder.test/12b", [0.7, 0.7, 0], "Firm linked to Huang's ex-lover brokered $12B in chips",
              source_id="decoder")
    _stored(db, [a, b])
    store = cluster.Store(db, SRCS, S, NOW, _judge({b.title: "followup", a.title: "followup"}, calls))
    ea, _ = cluster.create(store, a)
    eb, _ = cluster.create(store, b)
    db.conn.execute("INSERT INTO twins VALUES (?,?,?,?)", (eb, ea, 0.6, iso(NOW)))
    assert cluster.merge_twins(store) == 0 and set(store.events) == {ea, eb}
    assert db.one("SELECT COUNT(*) n FROM twins")["n"] == 0
    assert cluster.merge_pass(store) == 0
    # The edition's duplicate guard reads the ruling and keeps both cards.
    d = rank.Dedup(alone=0.65, t=0.5)
    ca, cb = _cand(ea, vec=[1, 0]), _cand(eb, vec=[0.8, 0.6])
    for c in (ca, cb):
        c.keys = frozenset([("p", "huang")])
        c.data = {}
    assert publish_v1.suppress_duplicates([ca, cb], d)[1]
    apart = {tuple(sorted((ea, eb)))}
    kept, dropped = publish_v1.suppress_duplicates([ca, cb], d, lambda x, y: tuple(sorted((x, y))) in apart)
    assert len(kept) == 2 and not dropped


# Ranking with spice

def _cand(eid="ev_a", importance=6, spice=5, type="news", topics=("ai",), age_h=2.0, n=1, vec=None, **kw):
    c = rank.Candidate(
        id=eid, importance=importance, type=type, is_news=True, relevance=3, topics=list(topics), region="global",
        n_publishers=n, on_techmeme=kw.pop("techmeme", False), hn_points=kw.pop("hn", 0), authority=1.0,
        published_at=NOW - timedelta(hours=age_h), lead_source=eid, lead_publisher=eid, spice=spice,
        title=kw.pop("title", "A headline"), **kw)
    if vec is not None:
        c.headline_vec = embed.normalize(vec)
    return c


TOP = rank.TABS["top"]


def test_spice_multiplies_the_score():
    assert rank.spice_factor(5) == 1 and rank.spice_factor(None) == 1
    assert rank.spice_factor(1) == pytest.approx(0.64) and rank.spice_factor(10) == pytest.approx(1.45)
    dull, juicy = _cand("d", spice=3), _cand("j", spice=9)
    assert rank.cand_score(juicy, TOP, NOW) / rank.cand_score(dull, TOP, NOW) == pytest.approx(1.36 / 0.82)
    order = [c.id for c, _ in rank.assemble([dull, juicy], TOP, NOW)]
    assert order == ["j", "d"]
    # The spec's worked checks are unchanged at neutral spice.
    assert rank.score(6, 1, True, 0, 1.2, 2, 10) == pytest.approx(0.571, abs=0.01)


def test_a_juicy_story_enters_top_below_the_importance_minimum():
    assert rank.eligible(_cand(importance=5, spice=5, age_h=20), TOP, NOW) == "importance"
    assert rank.eligible(_cand(importance=5, spice=8, age_h=20), TOP, NOW) is None
    # Two points under needs traction: one outlet's gossip item doesn't get there alone.
    assert rank.eligible(_cand(importance=4, spice=9, age_h=20), TOP, NOW) == "importance"
    assert rank.eligible(_cand(importance=4, spice=9, age_h=20, n=2), TOP, NOW) is None
    assert rank.eligible(_cand(importance=4, spice=9, age_h=20, hn=300), TOP, NOW) is None
    assert rank.eligible(_cand(importance=3, spice=10, n=5), TOP, NOW) == "importance"
    assert rank.eligible(_cand(importance=5, spice=8, age_h=40), TOP, NOW) == "window"
    # Other tabs take the same pass.
    assert rank.eligible(_cand(importance=4, spice=8), rank.TABS["ai"], NOW) is None
    assert rank.eligible(_cand(importance=4, spice=4), rank.TABS["ai"], NOW) == "importance"


def test_dry_routine_items_stay_out_of_top():
    assert rank.eligible(_cand(importance=6, spice=2), TOP, NOW) == "dry"
    assert rank.eligible(_cand(importance=8, spice=2), TOP, NOW) is None
    assert rank.eligible(_cand(importance=6, spice=2), rank.TABS["ai"], NOW) is None


def test_notifications_rank_by_spice_importance_and_traction():
    solo = _cand("solo", importance=6, spice=9)
    big = _cand("big", importance=9, spice=9, n=4, techmeme=True, hn=800)
    dull = _cand("dull", importance=9, spice=2, n=4, techmeme=True)
    old = _cand("old", importance=9, spice=10, n=6, age_h=30)
    bare = _cand("bare", importance=9, spice=10, n=6)
    for c in (solo, big, dull, old):
        c.data = {"notif_title": "Short title", "notif_line": "A hook to tap.", "headline": "H",
                  "lead": {"url_canonical": f"https://x.test/{c.id}"}}
    bare.data = {"notif_title": None, "notif_line": None, "headline": "H", "lead": {"url_canonical": "u"}}
    assert rank.notification_score(big) == pytest.approx(0.9 * 0.9 * rank.coverage(4, True, 800))
    got = publish_v1.notification_candidates({"top": [(c, 1.0) for c in (solo, big, dull, old, bare)]}, NOW)
    assert [n["eventId"] for n in got] == ["big", "solo", "dull"]         # stale and hook-less cards never push
    assert got[0]["title"] == "Short title" and got[0]["body"] == "A hook to tap." and got[0]["spice"] == 9


def test_article_carries_spice_and_a_bounded_notification():
    c = _cand(importance=7, spice=8)
    c.data = {"lead": {"url_canonical": "https://x.test/a", "title": "T"}, "headline": "Headline", "summary": "Sum.",
              "publishers": ["ev_a"], "notif_title": "Nvidia CEO ousted", "notif_line": "x" * 110}
    a = publish_v1.article(c, "Top", 0, None)
    assert a["spice"] == 8 and a["notification"] == {"title": "Nvidia CEO ousted", "body": "x" * 110}
    c.data["notif_line"] = "x" * 111
    assert "notification" not in publish_v1.article(c, "Top", 0, None)
    c.data.update(notif_line="A hook.", notif_title="y" * 41)
    assert publish_v1.article(c, "Top", 0, None)["notification"] == {"title": "ev_a", "body": "A hook."}
    c.ai_text = False       # a degraded card has no checked hook
    c.data["lead"]["feed_text"] = "Feed text."
    assert "notification" not in publish_v1.article(c, "Top", 0, None)


def test_notif_limits_are_validated():
    assert validate.NOTIF_MAX == 110 and validate.NOTIF_TITLE_MAX == 40
    # An over-long alert never costs the card: the short headline and the short first sentence stand in.
    o = out(SEBI, notif_title="Sebi to issue AI usage guidelines for all of India's markets",
            headline_short="Sebi to issue AI rules", notif_line="x" * 111 + ".",
            s1_alt="Pandey said the proposed rules would cover disclosures.")
    validate.select(o)
    assert o.notif_title == "Sebi to issue AI rules" and o.notif_line == "Pandey said the proposed rules would cover disclosures."
    o = out(SEBI, notif_title="y" * 41, headline_short="")
    validate.select(o)
    assert o.notif_title == "" and validate.check(o, SEBI_SRC, SEBI_TITLE, "YourStory", None, None, SEBI_SRC).ok
    v = check(SEBI, notif_line="Will Sebi's proposed AI guidelines keep market decisions under human oversight?")
    assert not v.ok and "notif" in v.reasons
    # A number in the alert title must be in the source like any other.
    v = check(SEBI, notif_title="Sebi to fine brokers $5M")
    assert not v.ok and "ungrounded" in v.reasons


def test_health_reports_spice_and_the_alert_candidates():
    db = _db()
    for i, sp in enumerate([2, 5, 8, 9]):
        db.conn.execute("INSERT INTO writes (url_canonical, status, written_at, spice) VALUES (?,?,?,?)",
                        (f"u{i}", "ok", iso(NOW), sp))
    report = {"spice": {"cards": 4, "median": 6.5}, "notifications": [{"eventId": "ev_1", "score": 0.5}]}
    got = health.spice_stats(db, NOW - timedelta(hours=24), report)
    assert got["items"]["8"] == 1 and got["itemShareAtLeast7"] == 0.5 and got["itemShareAtMost2"] == 0.25
    assert got["edition"] == report["spice"] and got["notifications"] == report["notifications"]
    assert "spice" in health.build(db, SRCS, report, NOW)


def test_live_lag_counts_only_stories_that_arrived_while_polling():
    db = _db()
    rows = [("ev_seed", NOW - timedelta(hours=20), NOW - timedelta(minutes=5), NOW),         # seed backlog
            ("ev_live", NOW - timedelta(minutes=6), NOW - timedelta(minutes=4), NOW)]         # arrived live
    for eid, pub, seen, first in rows:
        db.conn.execute("INSERT INTO shipped VALUES (?,?,?,?,?)", (eid, iso(pub), iso(first), iso(first), iso(seen)))
    f = health.freshness(db, NOW - timedelta(hours=24))
    assert f["events"] == 2 and f["live"]["events"] == 1 and f["live"]["publisherToAppMin"]["p50"] == 6.0


def test_image_urls_are_reported_unserved_in_a_test_run():
    got = publish_v1.image_url_check(_db(), {"publish": {"git_remote": None}},
                                     {"articles": [{"image": "https://x.test/img/a_std.jpg"}]}, NOW)
    assert got["served"] is False and got["urls"] == 1


def test_rulings_are_asked_in_parallel_before_assignment_and_cached():
    db, calls = _db(), []
    a = _item("https://a.test/1", [1, 0, 0], "Letter")
    store = cluster.Store(db, SRCS, S, NOW, _judge({"Report two": "same", "Report three": "followup"}, calls))
    eid, _ = cluster.create(store, a)
    two = _item("https://b.test/2", [0.8, 0.6, 0], "Report two", source_id="verge")
    three = _item("https://c.test/3", [0.7, 0.7, 0], "Report three", source_id="cnbc")
    jobs = [(it.url, it.title, it.text, [store.events[eid]]) for it in (two, three)]
    assert cluster.prefetch_rulings(store, jobs) == 2 and len(calls) == 2
    assert cluster.prefetch_rulings(store, jobs) == 0                     # nothing left to ask
    assert cluster.assign(store, two) == (eid, "judge")
    assert cluster.assign(store, three)[1] == "followup_judge"
    assert len(calls) == 2                                                # the assignment used the cache

    def down(title, text, events):
        raise llm.LLMError("timeout")
    store.judge = down
    four = _item("https://d.test/4", [0.75, 0.66, 0], "Report four", source_id="bbc")
    cluster.prefetch_rulings(store, [(four.url, four.title, four.text, [store.events[eid]])])
    assert store.ruling(four.url, eid) is None                            # a failed call caches nothing


def test_commentary_on_a_story_cannot_score_like_the_story():
    from worker import importance

    def member(pub, type="analysis", imp=9):
        return importance.Member(f"u-{pub}", pub, pub, True, type, imp, ["bigtech"], [])
    res = importance.compute([member("guardian"), member("verge")], "analysis", ["bigtech"])
    assert res.value == 7 and "commentary" in res.clamps
    # Widely picked up, it may keep its score; hard news is untouched.
    res = importance.compute([member("guardian"), member("verge"), member("bbc")], "analysis", ["bigtech"])
    assert res.value == 9
    res = importance.compute([member("guardian", "news"), member("bbc", "news")], "news", ["bigtech"])
    assert res.value == 9 and "commentary" not in res.clamps


class RejectingSession:
    """An embedding endpoint whose content screening rejects any batch containing a text with "REJECT"."""

    def __init__(self):
        self.batches = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.batches.append(list(json["input"]))
        bad = any("REJECT" in t for t in json["input"])

        class R:
            status_code = 422 if bad else 200
            text = '{"error": {"message": "input rejected by content safety screening"}}'
            headers = {}

            def json(self_inner):
                return {"data": [{"index": i, "embedding": [1.0, float(i), 0.0]} for i in range(len(json["input"]))]}
        return R()


def test_one_rejected_input_does_not_block_the_batch():
    # One screened article made every embedding batch fail with HTTP 422, tick after tick; 30 items never clustered.
    e = embed.Embedder("https://nv.test/v1", "k", "m", session=RejectingSession(), sleep=lambda s: None)
    texts = ["a", "b", "Title\nREJECT body", "d", "e"]
    vecs = e.embed(texts, "passage")
    assert [v is None for v in vecs] == [False, False, True, False, False]
    # The item's title stands in; if that is rejected too, a zero vector (its own event) rather than a failure.
    vecs = e.embed_safe(texts, "passage", fallbacks=["a", "b", "Title", "d", "e"])
    assert all(v is not None for v in vecs) and float(vecs[2] @ vecs[2]) > 0
    vecs = e.embed_safe(["ok", "REJECT"], "passage", fallbacks=["ok", "REJECT title"])
    assert not vecs[1].any() and len(vecs[1]) == len(vecs[0])


def test_curated_sources_skip_the_screening_call():
    by_id = SRCS.by_id()
    assert by_id["infoq"].curated and by_id["stratechery"].curated and by_id["rust_blog"].curated
    assert not by_id["nypost_business"].curated and not by_id["techcrunch"].curated
