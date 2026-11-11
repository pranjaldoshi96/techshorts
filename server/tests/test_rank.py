"""WP4: scoring (spec 2.11), tab rules (2.12), degraded mode (2.14) and the editorial file. Pure, no network."""
import math
import random
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from worker import degraded, editorial, rank
from worker.db import DB

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
TOP = rank.TABS["top"]


def cand(eid="ev_a", importance=6, type="news", topics=("ai",), age_h=2.0, n=1, techmeme=False, hn=0,
         authority=1.0, relevance=3, is_news=True, publisher=None, material_h=None, **kw):
    return rank.Candidate(
        id=eid, importance=importance, type=type, is_news=is_news, relevance=relevance, topics=list(topics),
        region=kw.pop("region", "global"), n_publishers=n, on_techmeme=techmeme, hn_points=hn, authority=authority,
        published_at=NOW - timedelta(hours=age_h),
        material_at=None if material_h is None else NOW - timedelta(hours=material_h),
        lead_source=publisher or eid, lead_publisher=publisher or eid, **kw)


# 2.11 worked check

def test_worked_check_anthropic_ipo():
    assert rank.coverage(10, True, 715) == pytest.approx(3.575, abs=1e-3)
    s = rank.score(10, 10, True, 715, 1.2, 14, 10)
    assert s == pytest.approx(1.058, abs=0.01)


def test_worked_check_fresh_series_a():
    s = rank.score(6, 1, True, 0, 1.2, 2, 10)
    assert s == pytest.approx(0.571, abs=0.01)


def test_worked_check_ipo_yields_at_about_20_2_h():
    series_a = rank.score(6, 1, True, 0, 1.2, 2, 10)
    # Closed form from the spec, and the first hour (to 0.01 h) at which the IPO's own decay drops below it.
    assert 14 + 10 * math.log(rank.score(10, 10, True, 715, 1.2, 14, 10) / series_a) == pytest.approx(20.2, abs=0.05)
    t = 14.0
    while rank.score(10, 10, True, 715, 1.2, t, 10) >= series_a:
        t += 0.01
    assert t == pytest.approx(20.2, abs=0.05)


def test_age_eff_uses_the_material_update_plus_3_h():
    c = cand(age_h=30, material_h=1)
    assert c.age_eff(NOW) == pytest.approx(4)
    assert cand(age_h=2, material_h=1).age_eff(NOW) == pytest.approx(2)


# 2.11 property: Top #1 at t + 24 h is a different event, unless importance 10 with a material update in the last 6 h

def top1(cands, now):
    ranked = rank.assemble(cands, TOP, now)
    return ranked[0][0].id if ranked else None


def scenario(rng):
    """A random Top-qualified event as it stands at t, and a day of news later: the spec's "fresh $25M Series A"
    (importance 6, one publisher plus Techmeme, authority 1.2, 2 h old) arriving at t + 22 h."""
    imp = rng.choice([6, 7, 8, 9, 10])
    return cand("ev_star", importance=imp, type=rng.choice(["news", "launch", "funding", "earnings", "policy"]),
                age_h=rng.uniform(0, 12), n=rng.randint(1, 16), techmeme=rng.random() < 0.6,
                hn=rng.choice([0, 0, 150, 400, 900, 2000]), authority=rng.choice([0.8, 1.0, 1.1, 1.2]),
                publisher="Star")


def later(c, hours=24, material_h=None):
    return rank.Candidate(**{**c.__dict__, "published_at": c.published_at,
                             "material_at": None if material_h is None else NOW + timedelta(hours=hours - material_h)})


FRESH = dict(eid="ev_fresh", importance=6, type="funding", topics=("startups_funding",), n=1, techmeme=True,
             authority=1.2, publisher="Fresh")


@pytest.mark.parametrize("seed", range(300))
def test_top1_changes_within_24_h(seed):
    rng = random.Random(seed)
    star = scenario(rng)
    assert top1([star], NOW) == "ev_star"
    t24 = NOW + timedelta(hours=24)
    fresh = cand(age_h=-22, **FRESH)        # published at t + 22 h, so 2 h old at t + 24 h
    assert top1([later(star), fresh], t24) != "ev_star", star


def test_top1_may_stay_only_for_importance_10_with_a_recent_material_update():
    star = cand("ev_star", importance=10, n=10, techmeme=True, hn=715, authority=1.2, age_h=14, publisher="Star")
    t24 = NOW + timedelta(hours=24)
    fresh = cand(age_h=-22, **FRESH)
    assert top1([later(star, material_h=1), fresh], t24) == "ev_star"
    assert top1([later(star, material_h=None), fresh], t24) == "ev_fresh"
    nine = rank.Candidate(**{**star.__dict__, "importance": 9})
    assert top1([later(nine, material_h=1), fresh], t24) in ("ev_star", "ev_fresh")     # allowed either way


# 2.12 filters

@pytest.mark.parametrize("t", sorted(rank.NEVER_TYPES))
def test_never_shipped_types(t):
    c = cand(type=t, importance=9, n=5)
    assert all(rank.eligible(c, tab, NOW) is not None for tab in rank.TABS.values())


def test_never_shipped_relevance_and_empty_topics():
    assert all(rank.eligible(cand(relevance=1, importance=9), tab, NOW) for tab in rank.TABS.values())
    assert all(rank.eligible(cand(topics=(), importance=9), tab, NOW) for tab in rank.TABS.values())


def test_top_types_and_labelled_pieces_need_importance_8():
    assert rank.eligible(cand(type="opinion", importance=7), TOP, NOW) == "type"
    assert rank.eligible(cand(type="analysis", importance=8), TOP, NOW) is None
    assert rank.eligible(cand(type="release_notes", importance=9), TOP, NOW) == "type"
    assert rank.eligible(cand(importance=5), TOP, NOW) == "importance"


def test_top_window_36_h_or_72_h_from_event_published_at():
    assert rank.eligible(cand(importance=7, age_h=35), TOP, NOW) is None
    assert rank.eligible(cand(importance=7, age_h=37), TOP, NOW) == "window"
    assert rank.eligible(cand(importance=8, age_h=70), TOP, NOW) is None
    assert rank.eligible(cand(importance=8, age_h=73), TOP, NOW) == "window"
    # A material update brings an old story back inside the window (age_eff), never a re-fetch.
    assert rank.eligible(cand(importance=7, age_h=60, material_h=2), TOP, NOW) is None


def test_tab_topics_and_types():
    ai, dev, reads, india = (rank.TABS[t] for t in ("ai", "dev", "reads", "india"))
    assert rank.eligible(cand(topics=("bigtech",)), ai, NOW) == "topic"
    assert rank.eligible(cand(type="release_notes", topics=("ai",), importance=5), ai, NOW) is None
    assert rank.eligible(cand(type="tutorial", topics=("dev_tools",), importance=4, is_news=False), dev, NOW) is None
    assert rank.eligible(cand(type="analysis", importance=5, is_news=False), reads, NOW) == "not_reads"
    assert rank.eligible(cand(type="analysis", importance=5, is_news=False, lead_reads=True), reads, NOW) is None
    assert rank.eligible(cand(type="analysis", importance=6, is_news=False), TOP, NOW) == "not_news"
    assert rank.eligible(cand(topics=("startups_funding",), region="in", importance=5), india, NOW) is None


def test_unclassified_items_reach_only_top_and_only_from_tier1():
    c = cand(type=None, topics=(), relevance=0, classified=False, lead_tier1=True)
    assert rank.eligible(c, TOP, NOW) is None
    assert rank.eligible(c, rank.TABS["ai"], NOW) == "unclassified"
    assert rank.eligible(rank.Candidate(**{**c.__dict__, "lead_tier1": False}), TOP, NOW) == "unclassified"


# 2.12 assembly

def test_drop_below_25_percent_of_median_and_never_pad():
    cs = [cand(f"ev_{i}", importance=8, n=4, publisher=f"P{i}") for i in range(5)]
    cs.append(cand("ev_weak", importance=6, age_h=30, publisher="Weak"))
    ranked = rank.assemble(cs, TOP, NOW)
    assert [c.id for c, _ in ranked] == ["ev_0", "ev_1", "ev_2", "ev_3", "ev_4"]
    assert len(rank.assemble(cs[:3], TOP, NOW)) == 3


def test_publisher_diversity_2_in_top10_4_per_tab():
    cs = [cand(f"ev_{i:02}", importance=8, n=3, age_h=i * 0.1, publisher="Same") for i in range(8)]
    cs += [cand(f"ev_o{i:02}", importance=7, n=2, age_h=1 + i * 0.1, publisher=f"Other{i}") for i in range(12)]
    ranked = rank.assemble(cs, TOP, NOW)
    first10 = [c.lead_publisher for c, _ in ranked[:10]]
    assert first10.count("Same") == 2
    assert [c.lead_publisher for c, _ in ranked].count("Same") <= 4


def test_capped_cards_wait_for_a_later_rank_instead_of_ending_the_tab():
    # Three busy publishers: 2 each fill ranks 1-6; then the 4-per-tab cap applies rather than stopping at 6.
    cs = [cand(f"ev_{p}{i}", importance=7, n=2, age_h=i * 0.5 + j * 0.1, publisher=p)
          for j, p in enumerate("ABC") for i in range(6)]
    ranked = rank.assemble(cs, TOP, NOW)
    pubs = [c.lead_publisher for c, _ in ranked]
    assert len(ranked) == 12 and all(pubs.count(p) == 4 for p in "ABC")
    assert all(pubs[:6].count(p) == 2 for p in "ABC")
    # With enough publishers, a third card from one waits until rank 11 rather than being dropped.
    cs = [cand(f"ev_s{i}", importance=8, n=3, age_h=i * 0.1, publisher="Same") for i in range(3)]
    cs += [cand(f"ev_o{i:02}", importance=7, n=2, age_h=1 + i * 0.1, publisher=f"O{i}") for i in range(9)]
    ids = [c.id for c, _ in rank.assemble(cs, TOP, NOW)]
    assert ids.index("ev_s2") == 10


def test_dev_allows_one_tutorial_per_10():
    dev = rank.TABS["dev"]
    cs = [cand(f"ev_t{i}", type="tutorial", topics=("dev_tools",), importance=5, is_news=False,
               publisher=f"T{i}", age_h=i) for i in range(4)]
    cs += [cand(f"ev_n{i}", type="news", topics=("dev_tools",), importance=5, publisher=f"N{i}", age_h=5 + i)
           for i in range(8)]
    ranked = rank.assemble(cs, dev, NOW)
    assert sum(c.type == "tutorial" for c, _ in ranked[:10]) == 1


def test_image_hash_dedupe_in_top_10():
    a = cand("ev_a", importance=8, n=3, publisher="A", image_hash="ffff0000ffff0000")
    b = cand("ev_b", importance=7, n=3, publisher="B", image_hash="ffff0000ffff0001")    # 1 bit apart
    c = cand("ev_c", importance=7, n=3, publisher="C", image_hash="0123456789abcdef")
    assert [x.id for x, _ in rank.assemble([a, b, c], TOP, NOW)] == ["ev_a", "ev_c"]


def test_duplicate_guard_on_headline_vectors():
    v = np.array([1.0, 0.0])
    near = np.array([0.8, 0.6])         # cosine 0.8
    far = np.array([0.0, 1.0])
    a = cand("ev_a", importance=8, n=3, publisher="A", headline_vec=v)
    b = cand("ev_b", importance=7, n=3, publisher="B", headline_vec=near)
    c = cand("ev_c", importance=7, n=3, publisher="C", headline_vec=far)
    assert [x.id for x, _ in rank.assemble([a, b, c], TOP, NOW, dup_threshold=0.47)] == ["ev_a", "ev_c"]


def test_pins_and_boosts():
    a = cand("ev_a", importance=9, n=5, publisher="A")
    b = cand("ev_b", importance=6, publisher="B", pin=frozenset({"top"}))
    assert [x.id for x, _ in rank.assemble([a, b], TOP, NOW)] == ["ev_b", "ev_a"]
    old_pinned = cand("ev_old", importance=4, age_h=80, publisher="O", pin=frozenset({"top"}))
    assert rank.assemble([a, old_pinned], TOP, NOW)[0][0].id == "ev_old"
    buried = rank.Candidate(**{**a.__dict__, "boost": editorial.BURY})
    other = cand("ev_c", importance=7, n=2, publisher="C")
    assert [x.id for x, _ in rank.assemble([buried, other], TOP, NOW)][0] == "ev_c"


def test_degraded_ranking_fixes_importance_at_6():
    a = cand("ev_a", importance=9, n=1, publisher="A")
    b = cand("ev_b", importance=6, n=2, publisher="B")
    assert rank.assemble([a, b], TOP, NOW)[0][0].id == "ev_a"
    assert rank.assemble([a, b], TOP, NOW, degraded=True)[0][0].id == "ev_b"     # coverage x authority x F


# 2.14 degraded mode

def log(db, at, kind, outcome):
    db.log_write(at, None, kind, "m", outcome)


def test_outage_auth_until_a_call_succeeds():
    db = DB(":memory:")
    assert degraded.llm_outage(db, NOW) is None
    assert degraded.llm_outage(db, NOW, have_client=False) == "no_key"
    log(db, NOW - timedelta(minutes=3), "write", "ok")
    log(db, NOW - timedelta(minutes=1), "write", "http_401")
    assert degraded.llm_outage(db, NOW) == "auth"
    assert degraded.llm_outage(db, NOW + timedelta(hours=3)) == "auth"     # no call since: still out
    log(db, NOW + timedelta(minutes=1), "probe", "ok")
    assert degraded.llm_outage(db, NOW + timedelta(minutes=2)) is None


def test_outage_from_the_per_tick_key_check():
    class R:
        def __init__(self, code):
            self.status_code = code

    class S:
        def __init__(self, code):
            self.code = code

        def post(self, *a, **k):
            return R(self.code)
    cfg = {"llm": {"base_url": "https://llm.test/v1"}, "embed": {"model": "m"}}
    db = DB(":memory:")
    log(db, NOW - timedelta(minutes=5), "write", "ok")
    assert degraded.key_check(db, cfg, "k", NOW, S(403)) is False
    assert degraded.llm_outage(db, NOW) == "auth"           # nothing to write, yet the revoked key is noticed
    assert degraded.key_check(db, cfg, "k", NOW + timedelta(minutes=1), S(500)) is None
    assert degraded.llm_outage(db, NOW + timedelta(minutes=1)) == "auth"
    assert degraded.key_check(db, cfg, "k", NOW + timedelta(minutes=2), S(200)) is True
    assert degraded.llm_outage(db, NOW + timedelta(minutes=2)) is None


def test_outage_on_429_storm_and_low_pass_rate():
    db = DB(":memory:")
    for i in range(12):
        log(db, NOW - timedelta(minutes=i), "write", "http_429")
    log(db, NOW, "write", "ok")
    assert degraded.llm_outage(db, NOW) == "rate_limited"
    db = DB(":memory:")
    for i in range(8):
        log(db, NOW - timedelta(minutes=i), "item", "failed" if i < 5 else "ok")
    assert degraded.llm_outage(db, NOW) == "pass_rate"


def test_degraded_excerpt_is_55_words_at_a_sentence_boundary():
    s1 = "Anthropic filed for an IPO on Tuesday, seeking a valuation of up to $1.4tn. "
    s2 = "The company said it would list on Nasdaq under the ticker ANTH. "
    text = (s1 + s2) * 4 + "Subscribe to our newsletter for more."
    out = degraded.excerpt(text)
    assert len(out.split()) <= 55 and out.endswith(".")
    assert "Subscribe" not in out
    assert degraded.excerpt("one " * 80).endswith("…")


# Editorial file

def test_editorial_file(tmp_path):
    cfg = {"state_dir": str(tmp_path)}
    (tmp_path / "editorial.yaml").write_text(
        "pin: [{event: ev_old, tab: ai}, {event: ev_gone, until: 2026-11-10T00:00:00Z}]\n"
        "bury: [ev_b]\nboost: {ev_c: 1.5}\nkill: [ev_k]\n"
        "override: {ev_o: {headline: Owner headline, summary: Owner summary.}}\nblock: [Engadget, example.com/x]\n")
    ed = editorial.load(cfg, NOW, resolve=lambda e: {"ev_old": "ev_new"}.get(e, e))
    assert ed.pins == {"ev_new": {"ai"}}
    assert ed.boost == {"ev_b": editorial.BURY, "ev_c": 1.5}
    assert ed.kill == {"ev_k"} and ed.override["ev_o"]["headline"] == "Owner headline"

    class S:
        id, name, v1_name = "engadget", "Engadget", "Engadget"
    assert ed.blocked(S, "https://engadget.com/a") and ed.blocked(None, "https://example.com/x/1")
    (tmp_path / "editorial.yaml").write_text("pin: [\n")
    assert editorial.load(cfg, NOW).pins == {}          # a broken file is ignored, not fatal


def test_tg_cli_round_trips_through_the_worker_loader(tmp_path):
    import os
    import subprocess
    import sys
    server = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "TECHGIST_STATE": str(tmp_path)}

    def tg(*args):
        r = subprocess.run([sys.executable, os.path.join(server, "tg"), *args], env=env, capture_output=True,
                           text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout
    tg("pin", "ev_a", "--tab", "ai", "--hours", "2")
    tg("bury", "ev_b")
    tg("kill", "ev_k")
    tg("boost", "ev_c", "1.5")
    tg("override", "ev_o", "--headline", "Owner headline", "--summary", "Owner summary.")
    tg("block", "Engadget")
    ed = editorial.load({"state_dir": str(tmp_path)}, datetime.now(timezone.utc))
    assert ed.pins == {"ev_a": {"ai"}} and ed.boost == {"ev_b": editorial.BURY, "ev_c": 1.5}
    assert ed.kill == {"ev_k"} and ed.override == {"ev_o": {"headline": "Owner headline", "summary": "Owner summary."}}
    assert ed.block == ["Engadget"]
    tg("clear", "ev_k")
    tg("clear", "ev_a")
    ed = editorial.load({"state_dir": str(tmp_path)}, datetime.now(timezone.utc))
    assert ed.kill == set() and ed.pins == {}


def test_tab_overrides_from_config():
    tabs = rank.tabs_for({"tabs": {"reads": {"min_importance": 4, "bogus": 1}}})
    assert tabs["reads"].min_importance == 4 and tabs["top"] is rank.TABS["top"]
    essay = cand(type="analysis", importance=4, is_news=False, lead_reads=True)
    assert rank.eligible(essay, rank.TABS["reads"], NOW) == "importance"
    assert rank.eligible(essay, tabs["reads"], NOW) is None
