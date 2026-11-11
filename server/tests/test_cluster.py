"""WP3: embedding calibration, clustering into events, follow-ups, merges, lead choice and event importance.

No network. The fixture replay uses tests/fixtures/wp3_items.json: the 2026-11-11 edition's items with the LLM's
triage output and their embeddings, recorded once by tools/build_wp3_fixture.py. Each arrival order feeds items
one per tick, so the order really is the order the worker sees them.
"""
import json
import random
from datetime import timedelta

import pytest

from tools import fixture, label_pairs
from worker import cluster, config, embed, importance, llm, sources as sources_mod
from worker.db import DB, from_iso, iso

FIX = json.load(open(label_pairs.ITEMS))
ROWS = FIX["items"]
EDITION_AT = from_iso(json.load(open(fixture.FIXTURE))["publishedAt"])
CFG = config.load()
SRCS = sources_mod.load()
GOLD = json.load(open(label_pairs.GOLD))


def url(title_start):
    hits = [r["url"] for r in ROWS if r["title"].startswith(title_start)]
    assert len(hits) == 1, (title_start, hits)
    return hits[0]


def syn(sid):
    return next(r for r in ROWS if r.get("id") == sid)


rows_by_url = {r["url"]: r for r in ROWS}


def REPLAY_BY_TITLE(start):
    return rows_by_url[url(start)]


def gold(name):
    return [e["url"] for e in GOLD["events"][name]]


IPO_FILING = [url("Anthropic targets valuation"), url("Anthropic opens Australian retail IPO"),
              url("Anthropic’s Giant IPO"), url("Amazon and Google Will See Big Paydays"),
              url("Claude chatbot maker set for $1.3tn"), url("All 7 Anthropic founders"),
              url("Anthropic kicks off IPO roadshow")]
IPO_HARD_LEADS = {url("Anthropic targets valuation"), url("Anthropic opens Australian retail IPO"),
                  url("Claude chatbot maker set for $1.3tn"), url("Anthropic kicks off IPO roadshow"),
                  url("Anthropic’s Giant IPO")}
NOT_LEAD = {url("All 7 Anthropic founders"), url("Jim Cramer says Anthropic")}


# Fixture → state.db

def published(row):
    if row["kind"] == "synthetic":
        return EDITION_AT + timedelta(hours=row["published_offset_h"])
    return from_iso(row["published_at"])


def new_db():
    db = DB(":memory:")
    for s in SRCS.sources:          # every configured source passed check_sources (tier-1 is real)
        db.update_source(s.id, check_ok=1)
    return db


def pick_event(db, pick):
    """The recorded LLM pick as an event id now. Offline the model was shown neighbouring items, not events (see
    build_wp3_fixture --picks), so its pick is the event of the item it chose; if that item hasn't arrived yet in
    this order, the event of the closest candidate that has, which is the event the live prompt would have listed
    in its place."""
    if not pick:
        return "new"
    e = next((event_of(db, u) for u in [pick["url"]] + pick.get("candidates", []) if event_of(db, u)), None)
    if not e:
        return "new"
    e = cluster.resolve(db, e)
    return e if pick["kind"] == "join" else f"followup:{e}"


def insert(db, row, now, **over):
    llm_out = {**(row.get("llm") or {}), **over.pop("llm", {})}
    if row.get("pick") and "event" not in llm_out or llm_out.get("event") == "new":
        llm_out["event"] = pick_event(db, row.get("pick"))
        llm_out["material_update"] = (row.get("pick") or {}).get("material_update", llm_out.get("material_update"))
    u = over.pop("url", row["url"])
    pub = over.pop("published_at", published(row))
    signal = row["kind"] == "signal"
    db.insert_item({"url_canonical": u, "url": u, "source_id": row["source_id"], "title": row["title"],
                    "first_seen_at": now, "published_at": pub, "feed_published_at": pub,
                    "status": "signal" if signal else "written",
                    "extra": {"points": row.get("hn_points")} if signal else {}})
    if signal:
        db.conn.execute("INSERT INTO embeddings VALUES (?,?,?,?,?,?,?)",
                        (u, "query", "fixture", 0, None, embed.to_blob(embed.unpack_int8(row["emb_q"])), iso(now)))
        return
    db.conn.execute(
        "INSERT INTO writes (url_canonical, status, written_at, text_words, type, is_news, relevance, importance,"
        " breaking, sensitive, region, topics, entities, event, material_update, headline, summary, summary_short,"
        " notif_line, s1) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (u, over.pop("status", row["status"]), iso(now), row["text_words"], llm_out["type"],
         int(llm_out["is_news"]), llm_out["relevance"], llm_out["importance"], int(llm_out["breaking"]),
         int(llm_out["sensitive"]), llm_out["region"], json.dumps(llm_out["topics"]),
         json.dumps(llm_out["entities"]), llm_out.get("event", "new"), int(llm_out.get("material_update", False)),
         llm_out.get("headline"), llm_out.get("summary"), llm_out.get("summary_short"), llm_out.get("notif_line"),
         llm_out.get("s1")))
    db.conn.execute("INSERT INTO embeddings VALUES (?,?,?,?,?,?,?)",
                    (u, "passage", "fixture", 0, None, embed.to_blob(embed.unpack_int8(row["emb"])), iso(now)))


REPLAY = [r for r in ROWS if r.get("llm") and r.get("id") != "syn_huang_resigns"] + \
         [r for r in ROWS if r["kind"] == "signal"]


def order(name):
    rows = sorted(REPLAY, key=lambda r: (published(r), r["url"]))
    if name == "reverse":
        rows.reverse()
    elif name == "shuffled":
        random.Random(20261111).shuffle(rows)
    return rows


TICK = timedelta(minutes=2)
RETRY_TICKS = (15, 60)          # a held thin item's +30 min and +2 h retries, in 2 min ticks


def replay(name, start=EDITION_AT):
    """One item per 2-minute tick, in the given arrival order. A thin item still in no event is re-asked for its
    pick at its +30 min and +2 h retries (llm.write_pending does that), using the second recorded pick."""
    db = new_db()
    retries = []
    for k, row in enumerate(order(name)):
        now = start + k * TICK
        for at, r in [x for x in retries if x[0] == k]:
            if not event_of(db, r["url"]):
                db.conn.execute("UPDATE writes SET event = ? WHERE url_canonical = ?",
                                (pick_event(db, r["pick_retry"]), r["url"]))
        insert(db, row, now)
        if row.get("status") == "thin" and row.get("pick_retry"):
            retries += [(k + t, row) for t in RETRY_TICKS]
        cluster.run(db, CFG, SRCS, now)
    db.end = now
    return db


def event_of(db, u):
    row = db.one("SELECT event_id FROM event_members WHERE url_canonical = ?", u)
    return row and row["event_id"]


def ev(db, eid):
    return db.one("SELECT * FROM events WHERE id = ?", eid)


ORDERS = ["published", "reverse", "shuffled"]


@pytest.fixture(scope="module", params=ORDERS)
def replayed(request):
    return request.param, replay(request.param)


# WP3a: calibration

def test_pairs_fixture_meets_the_label_counts():
    pairs = label_pairs.load_pairs()
    same = [p for p in pairs if p["label"] == "same"]
    diff = [p for p in pairs if p["label"] == "different"]
    assert len(same) >= 60 and len(diff) >= 250
    whys = {p["why"] for p in diff}
    for needed in ("Glasswing", "RAISE", "Australia", "Nvidia earnings"):
        assert any(needed in w for w in whys), needed


def test_config_thresholds_are_the_calibrated_ones_at_097_precision():
    rows, vecs = label_pairs.load_rows()
    th, _ = label_pairs.thresholds(rows, vecs)
    for name, (t, curve) in th.items():
        assert CFG["cluster"][name] == t, name
        assert all(p >= 0.97 for s, tp, fp, p, r in curve if s >= t), name
    assert FIX["meta"]["dims"] == CFG["embed"]["dims"] and FIX["meta"]["lead_words"] == CFG["embed"]["lead_words"]


# WP3b: fixture replay in 3 arrival orders

def test_positive_merges(replayed):
    name, db = replayed
    assert len({event_of(db, u) for u in IPO_FILING}) == 1, name
    # The Huang probe: every member with text in one event. Thin members (NYT and Stratechery teasers of ~25 words)
    # join only when they clear the join rule; one that doesn't waits in no event (never a card), not in another.
    spy = {event_of(db, u) for u in gold("huang_spy_probe") if rows_by_url[u]["status"] != "thin"}
    assert len(spy) == 1 and None not in spy, name
    assert {event_of(db, u) for u in gold("huang_spy_probe")} <= spy | {None}, name
    assert len({event_of(db, u) for u in gold("coreweave_earnings")}) == 1, name
    assert event_of(db, url("Introducing Businesses in ChatGPT")) == \
        event_of(db, url("OpenAI turns ChatGPT into a business directory")), name


def test_negative_merges(replayed):
    name, db = replayed
    ipo = event_of(db, IPO_FILING[0])
    for u in gold("ny_raise_act") + gold("australia_ai_penalties"):
        e = event_of(db, u)
        assert e is not None and e != ipo, (name, u)
    # Glasswing is an HN item whose page gave 49 words, so it stays a signal (spec 2.9 HN import needs 300): it must
    # not attach to the IPO either.
    glasswing = url("After 7 months, Glasswing")
    assert event_of(db, glasswing) is None
    assert ipo not in {r["event_id"] for r in db.q("SELECT event_id FROM event_signals WHERE url_canonical = ?",
                                                   glasswing)}, name
    assert event_of(db, syn("syn_nvidia_earnings")["url"]) != event_of(db, gold("huang_spy_probe")[0]), name


def test_ipo_lead_is_a_hard_news_filing_story(replayed):
    name, db = replayed
    lead = ev(db, event_of(db, IPO_FILING[0]))["lead_url"]
    assert lead in IPO_HARD_LEADS and lead not in NOT_LEAD, (name, lead)


def test_no_event_mixes_two_labelled_events(replayed):
    name, db = replayed
    owner = {u: e for e, items in GOLD["events"].items() for u in [i["url"] for i in items]}
    for e in db.q("SELECT id FROM events WHERE status = 'live'"):
        labels = {owner[r["url_canonical"]] for r in db.q("SELECT url_canonical FROM event_members WHERE event_id = ?",
                                                          e["id"]) if r["url_canonical"] in owner}
        assert len(labels) <= 1, (name, labels)


def test_every_item_is_in_one_live_event(replayed):
    name, db = replayed
    for r in REPLAY:
        if r["kind"] == "signal" or db.end - published(r) > timedelta(days=14):
            continue                # signals attach, they aren't members; > 14 days old is ignored (spec 2.3)
        e = event_of(db, r["url"])
        if r["status"] == "thin" and e is None:
            continue                # thin and alone: waits for a sibling, never starts a card
        assert e is not None and ev(db, e)["status"] == "live", (name, r["title"])


def test_event_text_comes_from_the_lead_and_published_at_is_the_earliest_member(replayed):
    name, db = replayed
    for e in db.q("SELECT * FROM events WHERE status = 'live'"):
        members = db.q("SELECT i.published_at FROM event_members m JOIN items i USING (url_canonical)"
                       " WHERE m.event_id = ?", e["id"])
        assert e["published_at"] == min(m["published_at"] for m in members)
        lead = db.one("SELECT headline FROM writes WHERE url_canonical = ?", e["lead_url"])
        assert e["headline"] == lead["headline"]


def test_ids_are_stable_across_replays_and_merged_ids_resolve():
    for name in ORDERS:
        a, b = replay(name), replay(name)
        ma = {r["url_canonical"]: r["event_id"] for r in a.q("SELECT * FROM event_members")}
        mb = {r["url_canonical"]: r["event_id"] for r in b.q("SELECT * FROM event_members")}
        assert ma == mb, name
        # Every id is sha1 of the member that created it (the anchor may move to an earlier report; the id never).
        for r in a.q("SELECT id FROM events WHERE status = 'live'"):
            urls = [m["url_canonical"] for m in a.q("SELECT url_canonical FROM event_members WHERE event_id = ?",
                                                    r["id"])]
            assert r["id"] in {cluster.event_id(u) for u in urls}
        # Re-running a pass with nothing new changes nothing.
        cluster.run(a, CFG, SRCS, a.end)
        assert ma == {r["url_canonical"]: r["event_id"] for r in a.q("SELECT * FROM event_members")}
        for al in a.q("SELECT * FROM event_alias"):
            target = cluster.resolve(a, al["old_id"])
            assert ev(a, target)["status"] == "live"
            assert ev(a, al["old_id"])["status"] == "merged"


def test_event_id_is_sha1_of_first_member_url():
    db = new_db()
    row = REPLAY[0]
    insert(db, row, EDITION_AT)
    cluster.run(db, CFG, SRCS, EDITION_AT)
    import hashlib
    assert event_of(db, row["url"]) == "ev_" + hashlib.sha1(row["url"].encode()).hexdigest()[:10]


def test_events_created_earlier_in_the_same_tick_are_candidates():
    db = new_db()
    a, b = REPLAY_BY_TITLE("Anthropic kicks off IPO roadshow"), REPLAY_BY_TITLE("Anthropic targets valuation")
    insert(db, a, EDITION_AT)
    insert(db, b, EDITION_AT)
    stats = cluster.run(db, CFG, SRCS, EDITION_AT)        # one pass, two new items
    assert stats["new"] == 1 and stats["cosine"] == 1
    assert event_of(db, a["url"]) == event_of(db, b["url"])


# Follow-ups

def _spy_db():
    db = replay("published")
    return db, event_of(db, gold("huang_spy_probe")[0])


def test_huang_resigns_50h_later_is_a_followup_with_parent_id():
    db, spy = _spy_db()
    resigns = syn("syn_huang_resigns")
    later = from_iso(ev(db, spy)["published_at"]) + timedelta(hours=50)
    insert(db, resigns, later, published_at=later)
    cluster.run(db, CFG, SRCS, later)
    eid = event_of(db, resigns["url"])
    assert eid != spy and ev(db, eid)["parent_id"] == spy
    assert ev(db, eid)["published_at"] == iso(later)          # its own time drives freshness and breaking
    # A second outlet's report of the resignation joins the follow-up, not a third event.
    second = "https://www.theverge.com/2026/11/13/huang-resigns"
    insert(db, {**resigns, "source_id": "verge"}, later, url=second, published_at=later + timedelta(minutes=20))
    cluster.run(db, CFG, SRCS, later + timedelta(minutes=30))
    assert event_of(db, second) == eid


def test_llm_followup_pick_starts_a_child_event():
    db, spy = _spy_db()
    resigns = syn("syn_huang_resigns")
    at = db.end + timedelta(minutes=10)
    insert(db, resigns, at, published_at=at, llm={"event": f"followup:{spy}", "material_update": False})
    cluster.run(db, CFG, SRCS, at)
    eid = event_of(db, resigns["url"])
    assert eid != spy and ev(db, eid)["parent_id"] == spy


def test_material_update_inside_the_window_joins_and_marks_the_event():
    db, spy = _spy_db()
    resigns = syn("syn_huang_resigns")
    at = from_iso(ev(db, spy)["published_at"]) + timedelta(hours=40)
    insert(db, resigns, at, published_at=at)
    cluster.run(db, CFG, SRCS, at)
    assert event_of(db, resigns["url"]) == spy
    row = ev(db, spy)
    assert row["last_material_update_at"] == iso(at) and "material_update" in row["rewrite_reason"]


# Importance clamps

def test_single_source_gadget_launch_rated_7_is_clamped_to_5():
    db = replay("published")
    row = ev(db, event_of(db, syn("syn_gadget")["url"]))
    assert row["importance_raw"] == 7 and row["importance"] == 5
    assert json.loads(row["clamps"]) == ["single_source", "gadget"]


def M(pub, tier1=True, type="news", imp=9, topics=("ai",), amounts=()):
    return importance.Member(f"https://x/{pub}", pub, pub, tier1, type, imp, list(topics), list(amounts))


@pytest.mark.parametrize("members,kw,expected,clamps", [
    ([M("a")], {}, 6, ["single_source"]),
    ([M("a"), M("b")], {}, 9, []),
    ([M("a")], {"on_techmeme": True}, 9, []),           # 1 tier-1 + Techmeme is corroboration (clamp 3)
    ([M("a")], {"hn_points": 150}, 7, ["no_tier1_corroboration"]),
    ([M("a", tier1=False), M("b", tier1=False)], {}, 7, ["no_tier1_corroboration"]),
    ([M("a"), M("b", tier1=False)], {"on_techmeme": True}, 9, []),
    ([M("a", type="release_notes", imp=6), M("b", type="release_notes", imp=6)], {"event_type": "release_notes"},
     4, ["release_notes_tutorial"]),
    ([M("a", type="funding", imp=6, amounts=["$600 million"]), M("b", type="funding", imp=6, amounts=["$600M"])],
     {"event_type": "funding"}, 8, ["funding_floor"]),
    # The floor needs clamp 3's corroboration: one tier-1 alone doesn't get it.
    ([M("a", type="funding", imp=6, amounts=["$2B"]), M("b", tier1=False, type="funding", imp=6)],
     {"event_type": "funding"}, 6, []),
    ([M("a", imp=7, topics=("consumer", "hardware"))], {}, 5, ["single_source", "gadget"]),
])
def test_importance_clamps(members, kw, expected, clamps):
    kw = dict(kw)
    et = kw.pop("event_type", members[0].type)
    res = importance.compute(members, et, members[0].topics, **kw)
    assert (res.value, res.clamps) == (expected, clamps)


def test_importance_is_the_median_of_hard_news_members():
    ms = [M("a", imp=9), M("b", imp=7), M("c", imp=3, type="opinion"), M("d", imp=8)]
    assert importance.compute(ms, "news", ["ai"]).raw == 8
    assert importance.compute(ms, "news", ["ai"], rewrite_importance=6).value == 6


def test_tier1_is_per_region():
    by = SRCS.by_id()
    assert importance.tier1_for(by["techcrunch"], 1, "global")
    assert not importance.tier1_for(by["techcrunch"], 1, "in")
    assert importance.tier1_for(by["inc42"], 1, "in") and not importance.tier1_for(by["inc42"], 1, "us")
    assert not importance.tier1_for(by["wired"], 2, "global")


# Join rules (synthetic vectors)

def _item(u, vec, type="news", people=(), pub=EDITION_AT, pick="new", **kw):
    it = cluster.Item(url=u, source_id=kw.pop("source_id", "techcrunch"), title=u, published_at=pub,
                      status=kw.pop("status", "ok"), type=type, is_news=True, relevance=3, importance=kw.pop("imp", 6),
                      breaking=False, sensitive=False, region="global", topics=["ai"],
                      entities={"people": list(people)}, pick=pick, material_update=kw.pop("material", False),
                      text_words=kw.pop("words", 400), vec=embed.normalize(vec))
    it.keys = cluster.entity_keys(it.entities)
    return it


S = cluster.Settings(t_cand=0.5, t_join=0.6, t_signal=0.5)


def _ev(anchor, published=EDITION_AT, updated=EDITION_AT):
    e = cluster.Event({"id": "ev_x", "published_at": iso(published), "updated_at": iso(updated), "type": anchor.type},
                      [anchor], anchor, set(anchor.keys))
    return e


def test_join_rules():
    a = _item("a", [1, 0, 0], people=["Jensen Huang"])
    e = _ev(a)
    now = EDITION_AT
    assert cluster.joinable(_item("b", [1, 0, 0], people=["Huang"]), e, 0.65, False, now, S) is None
    assert cluster.joinable(_item("b", [1, 0, 0]), e, 0.65, False, now, S) == "no_entity"
    # The LLM's pick replaces the entity rule only with a margin above T_join.
    assert cluster.joinable(_item("b", [1, 0, 0]), e, 0.62, True, now, S) == "no_entity"
    assert cluster.joinable(_item("b", [1, 0, 0]), e, 0.63, True, now, S) is None
    assert cluster.joinable(_item("b", [1, 0, 0], people=["Huang"]), e, 0.59, True, now, S) == "low_cosine"
    sec = _item("s", [1, 0, 0], type="security_incident", people=["Huang"])
    assert cluster.joinable(sec, _ev(_item("a", [1, 0, 0], type="earnings", people=["Huang"])), 0.9, True, now,
                            S) == "type"
    fund = _item("f", [1, 0, 0], type="funding", people=["Huang"])
    launch_ev = _ev(_item("a", [1, 0, 0], type="launch", people=["Huang"]))
    assert cluster.joinable(fund, launch_ev, 0.9, False, now, S) == "type"
    assert cluster.joinable(fund, launch_ev, 0.9, True, now, S) is None
    b = _item("b", [1, 0, 0], people=["Huang"])
    assert cluster.joinable(b, _ev(a, published=now - timedelta(hours=49)), 0.9, False, now, S) == "event_too_old"
    assert cluster.joinable(b, _ev(a, updated=now - timedelta(hours=25)), 0.9, False, now, S) == "event_too_old"


def test_entity_keys_normalise_people_and_amounts():
    k = cluster.entity_keys({"people": ["Jensen Huang"], "amounts": ["$1.4 trillion", "60%", "7"],
                             "products": ["Claude"], "orgs": ["Nvidia"]})
    assert ("p", "huang") in k and ("x", "claude") in k
    assert k & cluster.entity_keys({"amounts": ["$1.4T"]})
    assert not k & cluster.entity_keys({"amounts": ["£1.4tn", "60%"], "orgs": ["Nvidia"]})


# Merge pass and aliases

def _store_with(db, items, now=EDITION_AT):
    store = cluster.Store(db, SRCS, S, now)
    for it in items:
        cluster.create(store, it)
    return store


def _db_items(db, items, now=EDITION_AT):
    for it in items:
        db.insert_item({"url_canonical": it.url, "url": it.url, "source_id": it.source_id, "title": it.title,
                        "first_seen_at": now, "published_at": it.published_at, "status": "written"})


def test_merge_into_the_older_event_and_alias_resolves():
    db = new_db()
    old = _item("https://a/old", [1, 0.1, 0], people=["Huang"], pub=EDITION_AT - timedelta(hours=2))
    new = _item("https://b/new", [1, 0, 0.1], people=["Huang"], source_id="verge")
    _db_items(db, [old, new])
    store = _store_with(db, [new, old])           # the younger one happened to be created first
    assert cluster.merge_pass(store) == 1
    survivor, gone = cluster.event_id(old.url), cluster.event_id(new.url)
    assert set(store.events) == {survivor}
    assert cluster.resolve(db, gone) == survivor
    assert {m.url for m in store.events[survivor].members} == {old.url, new.url}
    # Older than 6 h by publisher time: left alone.
    db2 = new_db()
    old7 = _item("https://a/old7", [1, 0.1, 0], people=["Huang"], pub=EDITION_AT - timedelta(hours=8))
    new7 = _item("https://b/new7", [1, 0, 0.1], people=["Huang"], source_id="verge", pub=EDITION_AT - timedelta(hours=7))
    _db_items(db2, [old7, new7])
    store2 = _store_with(db2, [new7, old7])
    assert cluster.merge_pass(store2) == 0


def test_aliases_expire_after_30_days():
    db = new_db()
    db.conn.execute("INSERT INTO event_alias VALUES ('ev_old', 'ev_new', ?)", (iso(EDITION_AT - timedelta(days=31)),))
    db.conn.execute("INSERT INTO event_alias VALUES ('ev_old2', 'ev_new', ?)", (iso(EDITION_AT - timedelta(days=2)),))
    cluster.run(db, CFG, SRCS, EDITION_AT)
    assert cluster.resolve(db, "ev_old") == "ev_old" and cluster.resolve(db, "ev_old2") == "ev_new"


# Lead

def test_lead_is_hard_news_with_text_then_authority_then_earliest():
    db = new_db()
    anchor = _item("https://g/1", [1, 0, 0], source_id="guardian", people=["Huang"])
    opinion = _item("https://c/1", [1, 0, 0], type="opinion", source_id="techcrunch", people=["Huang"])
    short = _item("https://t/1", [1, 0, 0], source_id="techcrunch", words=80, people=["Huang"])
    _db_items(db, [anchor, opinion, short])
    store = _store_with(db, [anchor])
    e = store.events[cluster.event_id(anchor.url)]
    for it in (opinion, short):
        cluster.join(store, e, it, 1.0, "cosine")
    assert cluster.choose_lead(store, e).url == anchor.url
    tc = _item("https://t/2", [1, 0, 0], source_id="techcrunch", people=["Huang"])
    cluster.join(store, e, tc, 1.0, "cosine")
    assert cluster.choose_lead(store, e).url == tc.url              # authority 1.2 beats 1.1
    only_opinion = _store_with(new_db(), [opinion])
    assert cluster.choose_lead(only_opinion, only_opinion.events[cluster.event_id(opinion.url)]).url == opinion.url


def test_lead_change_sets_material_update_and_queues_a_rewrite():
    db = new_db()
    first = _item("https://g/1", [1, 0, 0], source_id="guardian", people=["Huang"], pub=EDITION_AT - timedelta(hours=3))
    _db_items(db, [first])
    store = _store_with(db, [first])
    e = store.events[cluster.event_id(first.url)]
    cluster.refresh(store, e, lambda s: True)
    assert e.row["lead_url"] == first.url and not e.row.get("rewrite_reason")
    tc = _item("https://t/2", [1, 0, 0], source_id="techcrunch", people=["Huang"])
    cluster.join(store, e, tc, 1.0, "cosine")
    cluster.refresh(store, e, lambda s: True)
    assert e.row["lead_url"] == tc.url and e.row["last_material_update_at"] == iso(tc.published_at)
    assert "lead_change" in e.row["rewrite_reason"]


# Signals

def test_signal_attaches_by_cosine_and_sets_hn(replayed):
    name, db = replayed
    rows = db.q("SELECT * FROM event_signals")
    for r in rows:
        assert ev(db, r["event_id"])["status"] == "live"
    # Every attached signal is above T_signal or an exact URL match.
    assert all(r["how"] in ("url", "cosine") for r in rows)


def test_signal_exact_url_match():
    db = new_db()
    item = _item("https://example.com/story", [1, 0, 0], people=["Huang"])
    _db_items(db, [item])
    store = _store_with(db, [item])
    db.add_sighting(item.url, "techmeme", "Story", None, EDITION_AT)
    assert cluster.attach_signals(store) == 1
    cluster.refresh(store, store.events[cluster.event_id(item.url)], lambda s: True)
    assert ev(db, cluster.event_id(item.url))["on_techmeme"] == 1


# Rewrite

class FakeClient:
    def __init__(self, replies, entail="yes"):
        self.replies, self.entail, self.calls = list(replies), entail, []

    def chat(self, model, messages, max_tokens=900, kind="write", temperature=0.2):
        self.calls.append((kind, messages))
        if kind == "entail":
            return json.dumps({"entailed": self.entail, "unsupported": ""})
        return json.dumps(self.replies.pop(0))


SRC_TEXT = ("Nvidia chief executive Jensen Huang returned to the office on Monday after the board opened a review of "
            "his relationship with a suspected Chinese spy, a woman US officials suspect of spying for China. The CEO "
            "said little. The FBI is looking into the allegations, and Nvidia hired outside lawyers to check whether "
            "company secrets were shared. Huang told staff the company would carry on as usual while the review continues. Nvidia shares fell 4% on Monday. ")
REWRITE = {"importance": 9, "headline": "Nvidia reviews Huang's ties to suspected Chinese spy",
           "s1": "Nvidia's board opened a review of CEO Jensen Huang's relationship with a woman US officials suspect "
                 "of spying.",
           "s2": "The FBI is looking into the allegations, and Nvidia hired outside lawyers to check whether secrets "
                 "were shared.",
           "s3": "Huang told staff the company would carry on as usual while the review continues this week.",
           "notif_line": "The FBI is looking into the allegations."}


def _rewrite_event(text=SRC_TEXT):
    db = new_db()
    a = _item("https://g/1", [1, 0, 0], source_id="guardian", people=["Huang"])
    b = _item("https://c/1", [1, 0.05, 0], source_id="cnbc", people=["Huang"])
    c = _item("https://b/1", [1, 0, 0.05], source_id="bbc", people=["Huang"])
    for it in (a, b, c):
        it.text, it.headline = text, "Lead headline"
    _db_items(db, [a, b, c])
    store = _store_with(db, [a])
    e = store.events[cluster.event_id(a.url)]
    cluster.join(store, e, b, 0.99, "cosine")
    cluster.join(store, e, c, 0.99, "cosine")
    cluster.refresh(store, e, lambda s: True)
    return store, e


def test_two_new_publishers_queue_a_rewrite_and_it_replaces_the_text():
    store, e = _rewrite_event()
    assert e.row["rewrite_reason"] == "publishers" and cluster.due_rewrite(e.row, store.now, store.s)
    client = FakeClient([REWRITE])
    assert llm.rewrite_event(client, {"llm": {"model": "m"}}, store, e)
    assert e.row["headline"] == REWRITE["headline"] and e.row["text_from"] == "rewrite"
    assert e.row["rewrite_importance"] == 9 and not e.row["rewrite_reason"]
    assert [h["headline"] for h in json.loads(e.row["headline_history"])] == ["Lead headline", REWRITE["headline"]]
    # Within 2 h no new rewrite, unless a material update.
    e.row["rewrite_reason"] = "publishers"
    assert not cluster.due_rewrite(e.row, store.now + timedelta(hours=1), store.s)
    e.row["rewrite_reason"] = "material_update"
    assert cluster.due_rewrite(e.row, store.now + timedelta(minutes=5), store.s)


def test_rewrite_numbers_must_be_in_the_lead_or_two_members():
    store, e = _rewrite_event()
    bad = {**REWRITE, "s3": "Nvidia shares fell 9% on Monday as investors weighed the review of the chief."}
    client = FakeClient([bad, bad])
    assert not llm.rewrite_event(client, {"llm": {"model": "m"}}, store, e)
    assert e.row["headline"] == "Lead headline" and e.row["text_from"] != "rewrite"
    assert "9%" in client.calls[1][1][-1]["content"]          # the retry was told which number


def test_rewrite_entailment_failure_keeps_the_lead_text():
    store, e = _rewrite_event()
    assert not llm.rewrite_event(FakeClient([REWRITE, REWRITE], entail="no"), {"llm": {"model": "m"}}, store, e)
    assert e.row["headline"] == "Lead headline"


# Embedding helpers

def test_int8_packing_keeps_cosine():
    import numpy as np
    rng = np.random.default_rng(1)
    a, b = embed.normalize(rng.normal(size=1024)), embed.normalize(rng.normal(size=1024))
    b = embed.normalize(a + 0.5 * b)
    assert abs(embed.cosine(embed.unpack_int8(embed.pack_int8(a)), embed.unpack_int8(embed.pack_int8(b)))
               - embed.cosine(a, b)) < 0.005


def test_item_input_is_title_plus_lead_words():
    assert embed.item_input(" Title ", "one two three four", 2) == "Title\none two"
    assert embed.item_input("Title", "", 120) == "Title"


class FakeEmbedSession:
    def __init__(self):
        self.bodies = []

    def post(self, u, json=None, timeout=None, headers=None):
        self.bodies.append(json)

        class R:
            status_code = 200
            headers = {}

            def __init__(self, n):
                self.n = n

            def json(self):
                return {"data": [{"index": i, "embedding": [1.0, float(i)]} for i in range(self.n)]}
        return R(len(json["input"]))


def test_embedder_batches_50_and_sends_input_type_and_dims():
    s = FakeEmbedSession()
    e = embed.Embedder("https://x/v1", "k", "m", dims=1024, session=s)
    out = e.embed([f"t{i}" for i in range(120)], "query")
    assert len(out) == 120 and [len(b["input"]) for b in s.bodies] == [50, 50, 20]
    assert all(b["input_type"] == "query" and b["dimensions"] == 1024 for b in s.bodies)


class TimeoutSession:
    def __init__(self):
        self.n = 0

    def post(self, *a, **kw):
        import requests
        self.n += 1
        raise requests.ReadTimeout()


def test_embedder_gives_up_fast_when_the_endpoint_is_down():
    s = TimeoutSession()
    e = embed.Embedder("https://x/v1", "k", "m", session=s, sleep=lambda x: None)
    with pytest.raises(embed.Unavailable):
        e.embed([f"t{i}" for i in range(50)])
    assert s.n == embed.Embedder.MAX_TIMEOUTS          # no halving into 50 single-input retries
