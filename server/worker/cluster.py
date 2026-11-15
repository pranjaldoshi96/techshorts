"""Clustering written items into events (spec 2.9): serial assignment, follow-ups, merge pass, lead, event text.

Runs once per tick after the parallel LLM stage. Items are assigned one at a time in published order, and an
event created earlier in the same pass is a candidate for the next item, so the result doesn't depend on how the
LLM threads happened to finish.

A join needs all of: cosine to the event's anchor >= T_join; compatible types; an event younger than 48 h that
was updated in the last 24 h; and the judge's ruling that the item reports the same specific development as the
event (llm.judge_events, one small call per item that has candidates, cached in `judgements`). A ruling of
"followup" starts a new event with the candidate as its parent, so each development of a running story is its own
card and the tabs' storyline cap groups them. Above T_same the texts are the same report and no ruling is asked.

Why a judge: cosine plus a shared person cannot tell developments of one saga apart, because every report of the
saga recaps it and names the same people. In the 2026-11-15 VPS test run one event took 13 members at cosine
0.48-0.70 (an investors' letter, a petition, "Where was the security team?", two reports of a company's statement,
three features and a mobility newsletter), shipped under a 33-hour-old headline with "7 publishers", and hid that
day's developments. Without a judge (tests, or no LLM at all) the old rule applies: a shared non-org entity
(person, product or amount), or the writer's own pick and cosine >= T_join + 0.03. The anchor is the first hard-news member (earliest by publisher time), never a
centroid, so an event can't drift one near-duplicate at a time from "Anthropic files for IPO" to "Anthropic's
models", and the thresholds measured on item pairs by tools/label_pairs.py still mean what they meant there.

IDs are "ev_" + sha1(first member's canonical URL)[:10] and never change; a merge aliases the younger id to the
older one for 30 days.
"""
import concurrent.futures as cf
import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np

from . import embed as embed_mod, importance as importance_mod, sources as sources_mod, validate
from .db import from_iso, iso
from .llm import LLMError

log = logging.getLogger("techgist")

HARD_NEWS = importance_mod.HARD_NEWS
LABELLED = ("opinion", "analysis", "interview")
LEAD_MIN_WORDS = 120
# Joins that need the LLM's say-so even when the vectors agree: an earnings story and a breach at the same company
# are different events, and a launch and the funding round behind it usually are.
NEVER_JOIN = {frozenset(("earnings", "security_incident"))}
JOIN_ONLY_IF_PICKED = {frozenset(("launch", "funding"))}
# An item on an event more than this many hours old (the event's first report) adds a follow-up,
# and scores at most FOLLOWUP_CAP for the event's importance unless it carries a material update. The model rates
# per-item coverage of a week-long mega-story 8 whatever the anchors say (the 2026-11 shadow: hard-news 8s 58 -> 119
# after the follow-up anchors), and one inflated member lifts the event median.
FOLLOWUP_AFTER_H = 24
FOLLOWUP_CAP = 6
# A hard-news member this much newer than the lead goes into the rewrite as its newest report (llm.rewrite_event).
NEWER_REPORT = timedelta(hours=3)
# A planned event that happened: the event's headline is about what is coming ("set to become billionaires in
# upcoming IPO") and a new hard-news member reports it done ("Anthropic's IPO prices", "Acme ships X"). Ambiguous
# verbs are left out: CNBC's "Anthropic's IPO lands as the AI trade looks for a bright spot" meant the IPO week had
# arrived (it lists on Nov 19), and the rewrite it triggered rightly kept "upcoming".
PROSPECTIVE = re.compile(r"\b(set to|plans? to|planning|upcoming|expected to|expects? to|targets?|targeting|"
                         r"seeks?|seeking|eyes|eyeing|prepares?|preparing|considers?|considering|in talks|"
                         r"weighs?|weighing|could|may|will|aims? to|ahead of|files? for|filed for|nears?|nearing)\b",
                         re.I)
REALIZED = re.compile(r"\b(debuts?|debuted|prices|priced|lists|listed|goes public|went public|"
                      r"opens|opened|soars?|soared|jumps?|jumped|surges?|surged|closes|closed|completes?|completed|"
                      r"finali[sz]es|finali[sz]ed|launches|launched|ships|shipped|releases|released|resigns|resigned|"
                      r"steps down|stepped down|approves?|approved|passes|passed|signs|signed|wins|won|rules|ruled|"
                      r"agrees|agreed|confirms|confirmed|starts trading|"
                      r"is live|now live)\b", re.I)


def realizes(event_headline, item_title):
    """True when `item_title` reports done what `event_headline` describes as coming."""
    if not event_headline or not item_title:
        return False
    return bool(PROSPECTIVE.search(event_headline)) and bool(REALIZED.search(item_title)) \
        and not PROSPECTIVE.search(item_title)


# A later report that overtakes an earlier card: the outcome that makes it wrong or out of date. In the third
# review's edition "ULA launches Vulcan with 40 Amazon internet satellites" (rank 21) sat beside "ULA reportedly
# sees first total failure, losing 40 Amazon satellites" (rank 2): the failure was ruled a follow-up, rightly a
# new development, and the launch card stayed. The judge can now rule "supersedes" (llm.JUDGE_PROMPT); this word
# rule is the backstop for a follow-up it rules plain "followup": a reversal in the new title that the old one
# lacks, on titles that are plainly about the same thing.
REVERSAL = re.compile(
    r"\b(fail(?:s|ed|ure|ures)?|lost|loses?|loss of|destroy(?:s|ed|ing)?|explod(?:es|ed)|blows? up|blew up|"
    r"crash(?:es|ed)?|abort(?:s|ed)?|scrub(?:s|bed)?|cancel(?:s|led|ed)?|calls? off|called off|collaps(?:es|ed)|"
    r"falls? through|fell through|withdraw(?:s|n)?|withdrew|pulls? out|pulled out|retract(?:s|ed)?|"
    r"revers(?:es|ed)|rescind(?:s|ed)?|overturn(?:s|ed)|postpon(?:es|ed)|delay(?:s|ed))\b", re.I)
OVERTAKE_SHARED = 3
OVERTAKE_SHARE = 0.4


def overtakes(old_title, new_title):
    """True when `new_title` reports a reversal of what `old_title` reports: see REVERSAL."""
    if not old_title or not new_title or not REVERSAL.search(new_title) or REVERSAL.search(old_title):
        return False
    old, new = _title_words(old_title), _title_words(new_title)
    shared = old & new
    return len(shared) >= OVERTAKE_SHARED and len(shared) >= OVERTAKE_SHARE * len(old)


def overtaken(members):
    """The members whose report a later hard-news member of the same event has overtaken: it reports done what
    they describe as coming ("says he will sign" -> "signs petition", validate.realized_by), or a reversal of it
    (`overtakes`). The event's card must not be written from them: "Musk says he will sign petition" led a
    4-publisher event in which two members reported that he had signed."""
    return set(_overtaken_by(members))


def _overtaken_by(members):
    """{overtaken member's url: [the later members that overtook it]}."""
    out = {}
    hard = [m for m in members if m.hard and not m.thin]
    for m in members:
        newer = [n for n in hard if n is not m and n.published_at > m.published_at and (
            validate.realized_by(m.title, n.title) or overtakes(m.title, n.title))]
        if newer:
            out[m.url] = newer
    return out


def event_id(url_canonical):
    return "ev_" + hashlib.sha1(url_canonical.encode()).hexdigest()[:10]


@dataclass
class Settings:
    t_cand: float
    t_join: float
    t_signal: float
    pick_margin: float = 0.03
    join_max_age_h: float = 48
    join_max_idle_h: float = 24
    merge_max_age_h: float = 6
    alias_days: int = 30
    max_candidates: int = 5
    candidate_days: int = 7
    rewrite_cap_h: float = 2
    # At or above this cosine two texts are the same report (syndication, a feed listing a story twice).
    t_same: float = 0.9
    # Judge rulings for signals per pass; the rest wait a tick.
    signal_judgements: int = 12
    # Rulings are asked for in parallel before the serial assignment (a backlog tick asked 31 one after another,
    # about a minute); rewrites are serial and capped, the rest stay queued for the next tick.
    judge_workers: int = 8
    max_rewrites: int = 3

    @classmethod
    def from_cfg(cls, cfg):
        c = dict(cfg.get("cluster") or {})
        return cls(**{k: v for k, v in c.items() if k in cls.__dataclass_fields__})


# Entities

def _norm(s):
    return re.sub(r"[^\w$%.]+", " ", (s or "").lower()).strip(" .")


def entity_keys(entities):
    """Comparable keys for the non-org entities. People also match on surname ("Huang" = "Jensen Huang"), amounts
    on parsed value and currency ("$1.4T" = "$1.4 trillion"), so the model's spelling doesn't decide a join.
    Bare numbers and percentages are left out: "60%" or "3" turn up in unrelated stories."""
    keys = set()
    for name in entities.get("people") or []:
        n = _norm(name)
        if n:
            keys.add(("p", n))
            last = n.split()[-1]
            if len(last) >= 3:
                keys.add(("p", last))
    for name in entities.get("products") or []:
        n = _norm(name)
        if n:
            keys.add(("x", n))
    for a in entities.get("amounts") or []:
        for num in validate.numbers(a):
            if num.percent or (num.currency is None and num.value < 1000):
                continue
            keys.add(("a", num.currency, f"{num.value:.3g}"))
    return keys


# Items

NO_TEXT_DROP = "held_out:no_text"
LOST_TEXT_H = 48


@dataclass
class Item:
    url: str
    source_id: str
    title: str
    published_at: object
    status: str                 # writes.status: ok | thin | failed
    type: str
    is_news: bool
    relevance: int
    importance: int
    breaking: bool
    sensitive: bool
    region: str
    topics: list
    entities: dict
    pick: str
    material_update: bool
    text_words: int
    vec: object
    headline: str = None
    summary: str = None
    summary_short: str = None
    notif_line: str = None
    s1: str = None
    entailed: object = None
    text: str = None
    spice: int = None
    notif_title: str = None
    publisher: str = None       # items.extra.publisher: the outlet of an item imported from a signal's link
    feed_text: str = None
    via: str = None             # items.extra.via: the signal source whose link this article was imported from
    news_at: object = None      # when its news happened, if days before the article (newsdate.py)
    signals: dict = field(default_factory=dict)    # items.extra.signals: {signal source id: {points, ...}}
    keys: set = field(default_factory=set)

    @property
    def hard(self):
        return self.type in HARD_NEWS

    @property
    def thin(self):
        return self.status == "thin"

    @property
    def happened_at(self):
        """When the news this item reports happened: its own time, or earlier for a late write-up."""
        return self.news_at or self.published_at

    @property
    def can_lead(self):
        return self.hard and not self.thin and self.text_words >= LEAD_MIN_WORDS


ITEM_SQL = """
SELECT i.url_canonical, i.source_id, i.title, i.published_at, w.status, w.type, w.is_news, w.relevance,
       w.importance, w.breaking, w.sensitive, w.region, w.topics, w.entities, w.event, w.material_update,
       w.text_words, w.headline, w.summary, w.summary_short, w.notif_line, w.s1, w.entailed, w.text, e.vec,
       w.spice, w.notif_title, i.extra, i.feed_text, w.news_at
FROM items i JOIN writes w ON w.url_canonical = i.url_canonical
JOIN embeddings e ON e.url_canonical = i.url_canonical AND e.kind = 'passage'
"""


def _item(r):
    ents = json.loads(r["entities"] or "{}")
    extra = json.loads(r["extra"] or "{}")
    it = Item(url=r["url_canonical"], source_id=r["source_id"], title=r["title"],
              published_at=from_iso(r["published_at"]), status=r["status"], type=r["type"],
              is_news=bool(r["is_news"]), relevance=r["relevance"] or 0, importance=r["importance"] or 1,
              breaking=bool(r["breaking"]), sensitive=bool(r["sensitive"]), region=r["region"] or "global",
              topics=json.loads(r["topics"] or "[]"), entities=ents, pick=r["event"] or "new",
              material_update=bool(r["material_update"]), text_words=r["text_words"] or 0,
              vec=embed_mod.from_blob(r["vec"]), headline=r["headline"], summary=r["summary"],
              summary_short=r["summary_short"], notif_line=r["notif_line"], s1=r["s1"],
              entailed=r["entailed"], text=r["text"], spice=r["spice"], notif_title=r["notif_title"],
              publisher=extra.get("publisher"), feed_text=r["feed_text"], via=extra.get("via"),
              news_at=from_iso(r["news_at"]), signals=extra.get("signals") or {})
    it.keys = entity_keys(ents)
    return it


def pending_items(db, now, max_age_days=14):
    """Classified items not yet in an event, oldest publisher time first. Thin items that found no event stay
    pending (they may never start one), so they are retried each tick until they age out."""
    # An item dropped only because its text could not be had ("held_out:no_text": three fetches of an open
    # site failed, or a paywall) keeps its classification and stays clusterable for LOST_TEXT_H: it can still
    # back an event up, and one the tech world is sharing can still start one and get a brief (thin_can_seed).
    rows = db.q(ITEM_SQL + " WHERE w.type IS NOT NULL AND (i.status != 'dropped' OR (i.drop_rule = ?"
                " AND i.published_at >= ?)) AND i.published_at >= ?"
                " AND i.url_canonical NOT IN (SELECT url_canonical FROM event_members)"
                " ORDER BY i.published_at, i.url_canonical", NO_TEXT_DROP, iso(now - timedelta(hours=LOST_TEXT_H)),
                iso(now - timedelta(days=max_age_days)))
    return [_item(r) for r in rows]


# Events in memory for one pass

@dataclass
class Event:
    row: dict
    members: list               # Items, in join order
    anchor: Item = None
    keys: set = field(default_factory=set)
    dirty: set = field(default_factory=set)     # reasons to refresh: join, material_update, merge

    @property
    def id(self):
        return self.row["id"]

    def ev_type(self):
        return self.row.get("type") or (self.anchor.type if self.anchor else None)


class Store:
    """Live events plus their members, loaded once per pass and written back as they change."""

    def __init__(self, db, srcs, settings, now, judge=None):
        self.db, self.srcs, self.s, self.now = db, srcs, settings, now
        # judge(title, text, [(event id, headline, first report's title)]) -> {event id: relation}; None = no judge.
        self.judge = judge
        self.judge_calls = 0
        self.by_id = srcs.by_id()
        self.events = {}
        since = iso(now - timedelta(days=settings.candidate_days))
        rows = db.q("SELECT * FROM events WHERE status = 'live' AND (published_at >= ? OR updated_at >= ?)",
                    since, since)
        for r in rows:
            members = [_item(m) for m in db.q(
                ITEM_SQL + " JOIN event_members em ON em.url_canonical = i.url_canonical WHERE em.event_id = ?"
                " ORDER BY em.joined_at, em.rowid", r["id"])]
            ev = Event(dict(r), members)
            ev.anchor = next((m for m in members if m.url == r["anchor_url"]), members[0] if members else None)
            ev.keys = set().union(*(m.keys for m in members)) if members else set()
            self.events[ev.id] = ev

    def resolve(self, eid):
        row = self.db.one("SELECT id FROM event_alias WHERE old_id = ?", eid)
        return row["id"] if row else eid

    def ranked(self, vec):
        out = [(float(np.dot(vec, ev.anchor.vec)), ev) for ev in self.events.values() if ev.anchor is not None]
        out.sort(key=lambda x: (-x[0], x[1].id))
        return out

    def publisher(self, source_id, item=None):
        if item is not None and getattr(item, "publisher", None):
            return item.publisher
        src = self.by_id.get(source_id)
        return src.v1_name if src else source_id

    # The judge's rulings, cached so a pair is asked about once.

    def ruling(self, a, b):
        row = self.db.one("SELECT verdict FROM judgements WHERE a = ? AND b = ?", a, b)
        return row["verdict"] if row else None

    def rule(self, a, b, verdict):
        self.db.conn.execute("INSERT OR REPLACE INTO judgements VALUES (?,?,?,?)", (a, b, verdict, iso(self.now)))

    def relations(self, key, title, text, events):
        """{event id: relation} for `events` (Event objects), asking the judge only about the ones not yet ruled
        on. Raises LLMError when the judge can't be reached."""
        out = {ev.id: self.ruling(key, ev.id) for ev in events}
        ask = [ev for ev in events if out[ev.id] is None]
        if ask:
            self.judge_calls += 1
            got = self.judge(title, text, [(ev.id, ev.row.get("headline"), ev.anchor.title if ev.anchor else "")
                                           for ev in ask])
            for ev in ask:
                out[ev.id] = got.get(ev.id, "different")
                self.rule(key, ev.id, out[ev.id])
        return out

    def same_event(self, a, b):
        """The judge's ruling on two events (cached under the sorted pair), or None without a judge."""
        if self.judge is None:
            return None
        x, y = sorted((a, b), key=lambda e: e.id)
        key = "pair:" + x.id
        got = self.ruling(key, y.id)
        if got is None:
            new, old = sorted((a, b), key=lambda e: (e.row["published_at"], e.id), reverse=True)
            src = new.anchor
            self.judge_calls += 1
            got = self.judge(src.title, src.text, [(old.id, old.row.get("headline"),
                                                    old.anchor.title if old.anchor else "")]).get(old.id, "different")
            self.rule(key, y.id, got)
        return got

    def authority(self, source_id):
        src = self.by_id.get(source_id)
        return float(src.authority) if src else 0.8


# Assignment

def parse_pick(pick):
    if not pick or pick == "new":
        return None, None
    if pick.startswith("followup:"):
        return "followup", pick.split(":", 1)[1].strip()
    if pick.startswith("ev_"):
        return "join", pick.strip()
    return None, None


def joinable(item, ev, cos, picked, now, s, judged=False):
    """None when `item` may join `ev`, else the first failing rule. `judged` leaves out the entity rule and the
    launch/funding pick rule: the judge's ruling on the pair replaces both."""
    if cos < s.t_join:
        return "low_cosine"
    if not judged and not (item.keys & ev.keys) and not (picked and cos >= s.t_join + s.pick_margin):
        return "no_entity"
    pair = frozenset((item.type, ev.ev_type()))
    if pair in NEVER_JOIN or (pair in JOIN_ONLY_IF_PICKED and not picked and not judged):
        return "type"
    ev_pub = from_iso(ev.row["published_at"])
    # An item published long before the event began is old background, not coverage of it.
    if ev_pub - item.published_at > timedelta(hours=s.join_max_age_h):
        return "item_too_old"
    if now - ev_pub >= timedelta(hours=s.join_max_age_h) or \
            now - from_iso(ev.row["updated_at"]) >= timedelta(hours=s.join_max_idle_h):
        return "event_too_old"
    return None


def assign(store, item):
    """Puts one item into an event. Returns (event_id, how), or (None, reason) for a thin item with no event or
    an item whose ruling could not be had this pass (it stays pending and is tried again next tick)."""
    s, now = store.s, store.now
    kind, picked = parse_pick(item.pick)
    if picked:
        picked = store.resolve(picked)
    if not item.vec.any():
        # No embedding (the endpoint was down; cluster.embed_fallback): title words stand in for the vector.
        ev = title_match(store, item)
        if ev is not None:
            return join(store, ev, item, 0.0, "title_fallback")
        if item.thin:
            return None, "thin_unmatched"
        return create(store, item, how="new_no_vector")
    ranked = store.ranked(item.vec)
    if store.judge is not None:
        return assign_judged(store, item, ranked, picked)

    if kind == "followup" and picked in store.events:
        # A second outlet's "Huang resigns" joins the follow-up the first one started, not a third event.
        for cos, ev in ranked:
            if ev.row.get("parent_id") == picked and joinable(item, ev, cos, False, now, s) is None:
                return join(store, ev, item, cos, "followup_join")
        return create(store, item, parent_id=picked, how="followup_llm")

    followup_of = None
    for cos, ev in ranked:
        if cos < s.t_join:
            break
        why = joinable(item, ev, cos, ev.id == picked, now, s)
        if why is None:
            return join(store, ev, item, cos, "llm_pick" if ev.id == picked else "cosine")
        # A big new fact on a story that is past its join window starts a follow-up ("Huang resigns" two days in).
        if (why == "event_too_old" and followup_of is None and item.material_update and item.importance >= 8
                and ((item.keys & ev.keys) or ev.id == picked)):
            followup_of = ev
    if followup_of is not None:
        return create(store, item, parent_id=followup_of.id, how="followup_material")
    if item.thin:
        return None, "thin_unmatched"
    return create(store, item)


def judged_candidates(store, item, ranked, picked):
    """(same-text event or None, [(cosine, event)] to put to the judge, a too-old event of the same story or None).
    The events are the nearest ones that pass the structural rules (types, ages)."""
    s, now = store.s, store.now
    cands, too_old = [], None
    for cos, ev in ranked:
        if cos < s.t_join:
            break
        why = joinable(item, ev, cos, ev.id == picked, now, s, judged=True)
        if why is None:
            if cos >= s.t_same:
                return (cos, ev), [], None
            if len(cands) < s.max_candidates:
                cands.append((cos, ev))
        elif why == "event_too_old" and too_old is None and ((item.keys & ev.keys) or ev.id == picked):
            too_old = ev
    return None, cands, too_old


def prefetch_rulings(store, jobs):
    """Asks the judge about several (key, title, text, [events]) at once and caches the answers. Only the calls
    run in threads; the cache is written here, in the worker's own thread. A failed call caches nothing, so the
    assignment asks again (and the item waits a tick if that fails too)."""
    jobs = [(key, title, text, [ev for ev in events if store.ruling(key, ev.id) is None])
            for key, title, text, events in jobs]
    jobs = [j for j in jobs if j[3]]
    if store.judge is None or not jobs:
        return 0

    def ask(job):
        key, title, text, events = job
        try:
            return store.judge(title, text, [(ev.id, ev.row.get("headline"), ev.anchor.title if ev.anchor else "")
                                             for ev in events])
        except LLMError:
            return None
    with cf.ThreadPoolExecutor(store.s.judge_workers) as pool:
        answers = list(pool.map(ask, jobs))
    for (key, _, _, events), got in zip(jobs, answers):
        store.judge_calls += 1
        if got is None:
            continue
        for ev in events:
            store.rule(key, ev.id, got.get(ev.id, "different"))
    return len(jobs)


def assign_judged(store, item, ranked, picked):
    """Assignment with the judge (see the module docstring): the nearest events that pass the structural rules
    are put to the judge in one call; "same" joins, "followup" starts a child event, anything else a new one."""
    same_text, cands, too_old = judged_candidates(store, item, ranked, picked)
    if same_text is not None:
        return join(store, same_text[1], item, same_text[0], "same_text")
    rel = {}
    if cands:
        try:
            rel = store.relations(item.url, item.title, item.text or item.headline or "", [ev for _, ev in cands])
        except LLMError as e:
            log.info("cluster judge unavailable for %s: %s", item.url, e)
            return None, "judge_failed"
    for cos, ev in cands:
        if rel.get(ev.id) == "same":
            return join(store, ev, item, cos, "judge")
    if item.thin and not thin_can_seed(store, item):
        # A thin item is classification only: it can back an event up, never start one.
        return None, "thin_unmatched"
    for cos, ev in cands:
        if rel.get(ev.id) in ("followup", SUPERSEDES):
            # The parent is the story's first event, so every development of a saga hangs off one root.
            eid, how = create(store, item, parent_id=ev.row.get("parent_id") or ev.id, how="followup_judge")
            # Every candidate the new report overtakes leaves the edition once this one has a card.
            for _, old in cands:
                if item.hard and not item.thin and (rel.get(old.id) == SUPERSEDES or (
                        rel.get(old.id) == "followup" and _overtakes_event(old, item))):
                    supersede(store, old, eid)
                    how = "supersedes_judge"
            return eid, how
    if too_old is not None and item.material_update and item.importance >= 8:
        return create(store, item, parent_id=too_old.id, how="followup_material")
    return create(store, item, how="thin_seed" if item.thin else "new")


SUPERSEDES = "supersedes"


def _overtakes_event(old, item):
    return overtakes(old.row.get("headline"), item.title) or overtakes(old.anchor.title if old.anchor else "",
                                                                       item.title)


def supersede(store, old, new_id):
    """Marks `old` as overtaken by the event `new_id`: the edition drops it while the newer event has a card
    (publish_v1.load_candidates). A report about the story, never an analysis or opinion, may do this."""
    new = store.events.get(new_id)
    if old.id == new_id or old.row.get("superseded_by") == new_id or (
            new is not None and new.row.get("superseded_by") == old.id):
        return
    old.row["superseded_by"] = new_id
    store.db.conn.execute("UPDATE events SET superseded_by = ? WHERE id = ?", (new_id, old.id))
    log.info("superseded: %s by %s", old.id, new_id)


# A paywalled outlet's scoop arrives as a title and a sentence. Such an item used to wait for an event to join
# and, when the scoop was exclusive, never found one: on 2026-11-15 the FT's "Huang has discussed a comeback as
# Nvidia CEO" (Techmeme's lead story), the NYT's Lutnick investigation (HN 361) and the FT's Pax Silica accord had
# no event and no card an hour after they broke. A thin hard-news item from an outlet that breaks news (authority
# >= SEED_AUTHORITY), or one Techmeme picked, now starts an event. The event has no card until it has traction
# and a brief written from the lines available (brief_due, llm.brief_event), or an open outlet's report joins.
SEED_AUTHORITY = 1.1


def thin_can_seed(store, item):
    if not item.hard or item.relevance < 2:
        return False
    via = store.by_id.get(item.via) if item.via else None
    return (store.authority(item.source_id) >= SEED_AUTHORITY or (via is not None and via.kind == "techmeme")
            or widely_shared(store, item))


# The same bars as rank.STRONG_HN and rank.STRONG_LOBSTERS: a story this widely shared gets a short attributed
# card from the lines available even when its page cannot be fetched, the way a paywalled scoop does.
SEED_HN, SEED_LOBSTERS = 300, 100


def widely_shared(store, item):
    """Is the item itself a big Hacker News or Lobsters link (its own points, kept in items.extra.signals)?"""
    for sid, pts in (item.signals or {}).items():
        src = store.by_id.get(sid)
        kind = (src.kind or ("hn" if sid.startswith("hn") else "")) if src else ""
        points = (pts or {}).get("points") or 0
        if (kind == "hn" and points >= SEED_HN) or (kind == "lobsters" and points >= SEED_LOBSTERS):
            return True
    return False


def create(store, item, parent_id=None, how="new"):
    eid = event_id(item.url)
    now = iso(store.now)
    row = {"id": eid, "status": "live", "parent_id": parent_id, "anchor_url": item.url, "lead_url": None,
           "created_at": now, "updated_at": now, "published_at": iso(item.published_at),
           "last_material_update_at": None, "headline_history": "[]", "on_techmeme": 0,
           # The rewrite trigger counts publishers that joined after this one (spec 2.9).
           "rewrite_publishers": json.dumps([store.publisher(item.source_id, item)])}
    cols = ", ".join(row)
    store.db.conn.execute(f"INSERT OR REPLACE INTO events ({cols}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))
    ev = Event(row, [], item)
    store.events[eid] = ev
    _add_member(store, ev, item, 1.0, how)
    parent = store.events.get(parent_id) if parent_id else None
    if parent is not None and item.hard and not item.thin and _overtakes_event(parent, item):
        supersede(store, parent, eid)
    return eid, how


def _set_anchor(ev):
    """The anchor is the event's first hard-news member with real text, "first" by publisher time. Arrival order
    would make the anchor (and so every later join) depend on which feed the worker happened to poll first; the
    publisher's clock doesn't. It can only ever move to an earlier report of the same story, never drift."""
    hard = [m for m in ev.members if m.hard and not m.thin]
    pool = hard or [m for m in ev.members if not m.thin] or ev.members
    first = min(pool, key=lambda m: (m.published_at, m.url))
    if ev.anchor is None or first.url != ev.anchor.url:
        ev.anchor = first
        ev.row["anchor_url"] = first.url


def join(store, ev, item, cos, how):
    _add_member(store, ev, item, cos, how)
    _set_anchor(ev)
    ev.row["updated_at"] = iso(store.now)
    mark_realized(store, ev, item)
    if item.material_update and item.hard:
        ev.dirty.add("material_update")
        _set_material(ev, item.happened_at)
    return ev.id, how


def mark_realized(store, ev, item):
    """Marks `item` a material update when it reports done what the event's headline describes as coming, on the
    same verb ("says he will sign petition" -> "signs petition"; validate.realized_by), or a reversal of it
    (`overtakes`). The rewrite then gets it as the newest report.

    The first version matched any prospective word in the headline against any "done" word in the title
    (`realizes`); in the shadow run it was wrong for 4 of the 6 members it marked, each wrong mark giving a
    day-old card a fresh publishedAt, and it was switched off. This one needs the same verb in both."""
    if item.material_update or not item.hard or item.thin:
        return False
    title = item.title
    if not any(validate.realized_by(old, title) or overtakes(old, title)
               for old in (ev.row.get("headline"), ev.anchor.title if ev.anchor and ev.anchor is not item else None)
               if old):
        return False
    if ev.anchor is not None and item.published_at <= ev.anchor.published_at:
        return False
    item.material_update = True
    store.db.conn.execute("UPDATE writes SET material_update = 1 WHERE url_canonical = ?", (item.url,))
    log.info("realized update: %s in %s", item.url, ev.id)
    return True


def _add_member(store, ev, item, cos, how):
    store.db.conn.execute("INSERT OR REPLACE INTO event_members VALUES (?,?,?,?,?)",
                          (item.url, ev.id, iso(store.now), how, round(cos, 4)))
    ev.members.append(item)
    ev.keys |= item.keys
    ev.dirty.add("join")


def _set_material(ev, at):
    cur = from_iso(ev.row.get("last_material_update_at"))
    if cur is None or at > cur:
        ev.row["last_material_update_at"] = iso(at)


# Merge pass

def merge_pass(store):
    """Fragments of one story younger than 6 h merge into the older event (spec 2.9). Age is event_published_at,
    as everywhere else: after downtime the worker creates a day's events in one pass, and judging youth by its own
    clock would let a stale story absorb a fresh one on a borderline cosine."""
    s, now = store.s, store.now
    merged = 0
    while True:
        young = [ev for ev in store.events.values() if ev.anchor is not None
                 and now - from_iso(ev.row["published_at"]) < timedelta(hours=s.merge_max_age_h)]
        best = None
        for i, a in enumerate(young):
            for b in young[i + 1:]:
                cos = float(np.dot(a.anchor.vec, b.anchor.vec))
                if cos < s.t_join or not (a.keys & b.keys):
                    continue
                if frozenset((a.ev_type(), b.ev_type())) in NEVER_JOIN:
                    continue
                if a.row.get("parent_id") == b.id or b.row.get("parent_id") == a.id:
                    continue        # a development and the story it follows are two cards on purpose
                if cos < s.t_same:
                    try:
                        if store.same_event(a, b) not in (None, "same"):
                            continue
                    except LLMError:
                        continue    # no ruling this pass; the pair is looked at again next tick
                if best is None or cos > best[0]:
                    best = (cos, a, b)
        if best is None:
            return merged
        _, a, b = best
        old, new = sorted((a, b), key=lambda e: (e.row["published_at"], e.row["created_at"], e.id))
        merge(store, new, old)
        merged += 1


def merge_twins(store):
    """Merges each twin the edition-wide duplicate guard found (table `twins`, written on publish) into its primary.
    Fragments older than the 6 h merge window used to stay two live events: the guard hid the twin while the primary
    was in the edition, and the twin shipped as a "new" card the moment the primary left (in the 2026-11 shadow run
    "Anthropic targets valuation of up to $1.4tn" shipped 3 min after its twin dropped out, 40 h after it was first
    seen). A twin member that reported a material update carries that update over, so a fresh development of an
    old story refreshes the surviving card instead of vanishing behind it."""
    merged = 0
    for r in store.db.q("SELECT twin_id, primary_id FROM twins ORDER BY at"):
        src = store.events.get(store.resolve(r["twin_id"]))
        dst = store.events.get(store.resolve(r["primary_id"]))
        if src is None or dst is None or src is dst:
            continue
        if src.row.get("parent_id") == dst.id or dst.row.get("parent_id") == src.id:
            continue
        # The edition's duplicate guard compares headlines; two developments of one saga can read alike ("Megaspeed
        # says Alice Huang left" / "Firm linked to Huang's ex-lover brokered $12B in chip orders"). The judge rules
        # before anything is merged, and the guard then leaves a pair ruled different alone (publish_v1).
        try:
            if store.same_event(src, dst) not in (None, "same"):
                store.db.conn.execute("DELETE FROM twins WHERE twin_id = ?", (r["twin_id"],))
                continue
        except LLMError:
            continue
        updates = [m.happened_at for m in src.members if m.material_update and m.hard]
        merge(store, src, dst)
        for at in updates:
            _set_material(dst, at)
        if updates:
            dst.dirty.add("material_update")
        merged += 1
    return merged


def merge(store, src, dst):
    db, now = store.db, iso(store.now)
    db.conn.execute("UPDATE event_members SET event_id = ? WHERE event_id = ?", (dst.id, src.id))
    db.conn.execute("UPDATE OR IGNORE event_signals SET event_id = ? WHERE event_id = ?", (dst.id, src.id))
    db.conn.execute("DELETE FROM event_signals WHERE event_id = ?", (src.id,))
    db.conn.execute("UPDATE events SET parent_id = ? WHERE parent_id = ?", (dst.id, src.id))
    db.conn.execute("UPDATE events SET superseded_by = ? WHERE superseded_by = ? AND id != ?",
                    (dst.id, src.id, dst.id))
    for ev in store.events.values():
        if ev.row.get("superseded_by") == src.id and ev is not dst:
            ev.row["superseded_by"] = dst.id
    if dst.row.get("superseded_by") == src.id:
        dst.row["superseded_by"] = None     # the two are one event now
    db.conn.execute("UPDATE event_alias SET id = ? WHERE id = ?", (dst.id, src.id))
    db.conn.execute("INSERT OR REPLACE INTO event_alias VALUES (?,?,?)", (src.id, dst.id, now))
    db.conn.execute("UPDATE events SET status = 'merged', updated_at = ? WHERE id = ?", (now, src.id))
    for ev in store.events.values():
        if ev.row.get("parent_id") == src.id:
            ev.row["parent_id"] = dst.id
    # Members keep their original join order: the older event's first, so its anchor stays.
    dst.members.extend(src.members)
    dst.keys |= src.keys
    dst.row["updated_at"] = now
    if src.row.get("last_material_update_at"):
        _set_material(dst, from_iso(src.row["last_material_update_at"]))
    dst.dirty.add("merge")
    del store.events[src.id]


# Lead, event text, importance

def choose_lead(store, ev):
    """Hard-news members with >= 120 words, ranked by cosine to the anchor x authority, then earliest; a validated
    write beats a failed one. Opinion, analysis and interview lead only when there is no hard-news member."""
    members = ev.members
    # The card follows the newest material fact the event holds: a member whose report a later member has
    # overtaken ("says he will sign" once others report "signs") does not lead while a current one can.
    stale = _overtaken_by(members)
    if stale:
        # First the reports that carry the newer fact, else any report that is not overtaken; an overtaken one
        # leads only when nothing else has a validated card (a stale card beats no card).
        newer = [n for ns in stale.values() for n in ns if n.url not in stale]
        current = [m for m in members if m.url not in stale]
        for pick in (newer, current):
            if any(m.hard and not m.thin and m.status == "ok" for m in pick):
                members = pick
                break
    pool = [m for m in members if m.can_lead]
    if not any(m.status == "ok" for m in pool):
        # No full-length report validated: a shorter hard-news report that did (NY Post's 114 words on Huang's
        # allies) leads before a longer one whose card failed, which would leave the event with no text.
        short_ok = [m for m in members if m.hard and not m.thin and m.status == "ok"]
        pool = short_ok or pool
    if not pool:
        hard = [m for m in members if m.hard and not m.thin]
        pool = hard or [m for m in members if not m.thin]
    if not pool:
        # Only thin members (paywalled feeds: a title and a sentence each). The best-placed outlet is the lead a
        # brief names and links to; the event has no card until a brief is written (brief_due).
        return min(members, key=lambda m: (-store.authority(m.source_id), m.published_at, m.url), default=None)
    anchor = ev.anchor.vec

    def key(m):
        score = float(np.dot(m.vec, anchor)) * store.authority(m.source_id)
        # A listed outlet leads before a site imported from a Hacker News link under the catch-all source.
        return (m.status != "ok", m.source_id == sources_mod.WEB_ID, -round(score, 4), m.published_at, m.url)
    return min(pool, key=key)


def refresh(store, ev, tier1_ok):
    """Recomputes everything derived from the members, and queues a rewrite when the spec asks for one."""
    row, members = ev.row, ev.members
    # A member that joined before the event's headline said "upcoming" (or before this rule) still counts.
    for m in members:
        if mark_realized(store, ev, m):
            _set_material(ev, m.happened_at)
            ev.dirty.add("material_update")
    _set_anchor(ev)
    # The event's clock is its first hard-news report. A profile or explainer that joined does not move it back:
    # "Meet the seven co-founders of Anthropic", published the day before, made the IPO-week card read 29 h old
    # when its report was 5 h old.
    clock = [m for m in members if m.hard and not m.thin] or members
    row["published_at"] = iso(min(m.published_at for m in clock))
    publishers = sorted({store.publisher(m.source_id, m) for m in members})
    row["publishers"], row["n_publishers"] = json.dumps(publishers), len(publishers)

    lead = choose_lead(store, ev)
    prev_lead = row.get("lead_url")
    if lead is not None and lead.url != prev_lead:
        row["lead_url"] = lead.url
        if prev_lead is not None:
            _set_material(ev, lead.happened_at)
            _queue_rewrite(row, "lead_change")
    # The clock is when the news happened, never when a late write-up of it was published or fetched: "Apple
    # starts selling HomeView" (PYMNTS, Sunday evening, on sales that began on Friday) was shown as 15 minutes old
    # and queued for a push alert.
    if lead is not None and lead.news_at is not None and iso(lead.news_at) < row["published_at"]:
        row["published_at"] = iso(lead.news_at)
    if "material_update" in ev.dirty:
        _queue_rewrite(row, "material_update")
    seen = set(json.loads(row.get("rewrite_publishers") or "[]"))
    if len(set(publishers) - seen) >= 2:
        _queue_rewrite(row, "publishers")
    # A hard-news report hours newer than the lead, published since the card's text was last written: the card
    # is rewritten with it as the newest report, so it states the newest account the event holds (see
    # llm.rewrite_event; the usual 2 h cap applies).
    if lead is not None:
        since = max(lead.published_at, from_iso(row.get("rewritten_at")) or lead.published_at)
        if any(m.hard and not m.thin and m is not lead and m.published_at > since
               and m.published_at - lead.published_at >= NEWER_REPORT for m in members):
            _queue_rewrite(row, "newer_report")

    if lead is not None:
        row.update(type=lead.type, topics=json.dumps(lead.topics), region=lead.region, relevance=lead.relevance,
                   is_news=int(lead.is_news))
        own = bool(lead.headline and lead.status == "ok")
        if row.get("text_from") == "rewrite" and not lead.hard:
            # An earlier rewrite of commentary (see due_rewrite): the lead's own card and score come back.
            row.update(text_from=None, rewrite_importance=None, rewrite_reason=None)
        if row.get("text_from") == BRIEF and not own and row.get("headline"):
            pass        # the brief stands until a member's own card validates (an open outlet's report)
        elif row.get("text_from") != "rewrite" or prev_lead != lead.url:
            _set_text(row, lead.headline, lead.summary, lead.summary_short, lead.notif_line, lead.s1,
                      lead.entailed, lead.url, store.now, lead.notif_title)
            row["rewrite_spice"] = None
    row["sensitive"] = int(any(m.sensitive for m in members))
    row["breaking_members"] = sum(m.breaking for m in members)
    _apply_signals(store, ev)

    region = row.get("region") or "global"
    start = story_start(row)
    imembers = []
    for m in members:
        src = store.by_id.get(m.source_id)
        t1 = src is not None and importance_mod.tier1_for(src, sources_mod.effective_tier(src, tier1_ok(src.id)),
                                                          region)
        imp = m.importance
        if (start is not None and not (m.material_update and m.hard)
                and m.published_at - start > timedelta(hours=FOLLOWUP_AFTER_H)):
            imp = min(imp, FOLLOWUP_CAP)
        imembers.append(importance_mod.Member(m.url, m.source_id, store.publisher(m.source_id, m), t1, m.type,
                                              imp, m.topics, m.entities.get("amounts") or []))
    hn = json.loads(row.get("hn") or "null") or {}
    res = importance_mod.compute(imembers, row.get("type"), json.loads(row.get("topics") or "[]"),
                                 bool(row.get("on_techmeme")), hn.get("points") or 0, row.get("rewrite_importance"))
    row.update(importance_raw=res.raw, importance=res.value, clamps=json.dumps(res.clamps))
    row["spice"] = event_spice(row, members, lead)
    ev.dirty.clear()
    save(store.db, row)


def event_spice(row, members, lead):
    """The event's spice: the rewrite call's score when the card is a rewrite, else the lead's (the card shows
    the lead's angle), else the median of the scored hard-news members. None when nothing was scored."""
    if row.get("text_from") in ("rewrite", BRIEF) and row.get("rewrite_spice"):
        return int(row["rewrite_spice"])
    if lead is not None and lead.spice:
        return int(lead.spice)
    scored = sorted(m.spice for m in members if m.spice and m.hard) or sorted(m.spice for m in members if m.spice)
    return scored[len(scored) // 2] if scored else None


# Briefs: a short attributed card for a story that has traction but no member with enough text to write from.
BRIEF = "brief"
BRIEF_MAX_AGE_H = 24
BRIEF_RETRY = timedelta(minutes=20)
BRIEF_MIN_WORDS = 25
BRIEF_MIN_WORDS_SHARED = 16
HN_LINE = "Hacker News (discussion, not the source of the report)"
BRIEF_HN = 150
BRIEF_SOLO_IMPORTANCE = 7
MAX_BRIEFS = 3


def brief_lines(store, ev):
    """What is known about an event with no full text: [(outlet, title, text)] from its members, best-placed
    outlet first, then Techmeme's own summary line when Techmeme picked the story. Text is each item's own (the
    feed sentence or the page's opening); nothing is taken from anywhere else."""
    out, seen = [], set()
    members = sorted(ev.members, key=lambda m: (-store.authority(m.source_id), m.published_at, m.url))
    for m in members:
        src = store.by_id.get(m.source_id)
        name = m.publisher or (src.name if src else m.source_id)
        text = " ".join((m.text or m.feed_text or "").split()[:90])
        key = (m.title.strip().lower(), text[:60].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((name, m.title.strip(), text))
    kinds = {s.id for s in store.srcs.sources if s.is_signal and s.kind == "techmeme"}
    for r in store.db.q("SELECT i.source_id, i.title, i.feed_text FROM event_signals s JOIN items i"
                        " ON i.url_canonical = s.url_canonical WHERE s.event_id = ?", ev.id):
        if r["source_id"] in kinds and (r["title"].strip().lower(), "") not in seen:
            src = store.by_id[r["source_id"]]
            text = " ".join((r["feed_text"] or "").split()[:90])
            if not any(text[:60].lower() == t[:60].lower() for _, _, t in out):
                out.append((f"{src.name} (summary of the report)", r["title"].strip(), text))
    # How widely the link is being discussed is itself a line the card may state: for a page that cannot be
    # fetched it is often the only fact besides the title.
    hn = json.loads(ev.row.get("hn") or "null") or {}
    if (hn.get("points") or 0) >= BRIEF_HN:
        out.append((HN_LINE, f"The link has {hn['points']} points and {hn.get('comments') or 0} comments on "
                             f"Hacker News", ""))
    return out


def brief_due(store, ev):
    """True when `ev` should get a brief now: no validated text from any member, hard news with a tech angle, under
    a day old, enough lines to write from, and traction: a Techmeme pick, a second publisher, a big Hacker News
    thread, or a scoop-breaking outlet's own report rated significant."""
    row, now = ev.row, store.now
    if row.get("headline") or not ev.members or row.get("type") not in HARD_NEWS or (row.get("relevance") or 0) < 2:
        return False
    if any(m.status == "ok" and m.headline for m in ev.members):
        return False
    if now - from_iso(row["published_at"]) > timedelta(hours=BRIEF_MAX_AGE_H):
        return False
    hn = (json.loads(row.get("hn") or "null") or {}).get("points") or 0
    lead = next((m for m in ev.members if m.url == row.get("lead_url")), None)
    solo = (lead is not None and store.authority(lead.source_id) >= SEED_AUTHORITY
            and (row.get("importance_raw") or 0) >= BRIEF_SOLO_IMPORTANCE)
    if not (row.get("on_techmeme") or (row.get("n_publishers") or 0) >= 2 or hn >= BRIEF_HN or solo):
        return False
    lines = brief_lines(store, ev)
    # A story Techmeme headlines or Hacker News is discussing at length gets its card from less: the title and
    # the discussion line are enough for a headline and one attributed sentence.
    need = BRIEF_MIN_WORDS_SHARED if row.get("on_techmeme") or hn >= SEED_HN else BRIEF_MIN_WORDS
    if sum(len(f"{title} {text}".split()) for _, title, text in lines) < need:
        return False
    last = from_iso(row.get("rewritten_at"))
    return (last is None or now - last >= BRIEF_RETRY
            or row.get("rewrite_publishers") != row.get("publishers"))


def story_start(row):
    """When the event began (its first report). A follow-up event starts its own clock: the outlets reporting
    "Huang resigns" are coverage of the resignation, not late coverage of the probe it follows."""
    return from_iso(row.get("published_at"))


def _queue_rewrite(row, reason):
    reasons = set(filter(None, (row.get("rewrite_reason") or "").split(",")))
    reasons.add(reason)
    row["rewrite_reason"] = ",".join(sorted(reasons))


def _set_text(row, headline, summary, short, notif, s1, entailed, source, now, notif_title=None):
    if headline and headline != row.get("headline"):
        hist = json.loads(row.get("headline_history") or "[]")
        hist.append({"at": iso(now), "headline": headline, "from": source})
        row["headline_history"] = json.dumps(hist[-20:])
    row.update(headline=headline, summary=summary, summary_short=short, notif_line=notif, s1=s1,
               entailed=entailed, text_from=source, notif_title=notif_title)


def save(db, row):
    cols = [k for k in row if k != "id"]
    db.conn.execute(f"UPDATE events SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                    (*[row[c] for c in cols], row["id"]))


# Signals (Techmeme, HN, The Information)

ORPHAN_KEY = "sigq:"


def signal_items(db, srcs, now, hours=48):
    """Signal sightings that may still need an event, as dicts: the signal sources' own rows, and the "orphans".

    An orphan is a Hacker News or Lobsters link that was imported as an article (fetch.promote_signals turns the
    signal's row into the article's, since both are the same URL) but could not be written and is in no event:
    a company page, a PDF landing page, a paywalled piece. Its points used to vanish with it. "SpaceX catches the
    Starship to end Flight 15" (spacex.com, HN 1,581) was held with no text while the Starship card, 9 publishers,
    showed no Hacker News points and sat last in Top. An orphan is matched to an event by its title like any
    signal, once per signal source that listed it; its query vector is stored under ORPHAN_KEY + url because the
    URL's own embedding row holds the article's passage vector."""
    signal_ids = [s.id for s in srcs.sources if s.is_signal]
    if not signal_ids:
        return []
    marks = ",".join("?" * len(signal_ids))
    since = iso(now - timedelta(hours=hours))
    out = [dict(r) for r in db.q(
        f"SELECT i.*, e.vec FROM items i LEFT JOIN embeddings e ON e.url_canonical = i.url_canonical"
        f" AND e.kind = 'query' WHERE i.status = 'signal' AND i.source_id IN ({marks})"
        f" AND i.published_at >= ?", *signal_ids, since)]
    for r in out:
        r["vec_key"] = r["url_canonical"]
    for r in db.q("SELECT i.*, e.vec FROM items i LEFT JOIN embeddings e ON e.url_canonical = ? || i.url_canonical"
                  " WHERE i.status != 'signal' AND i.extra LIKE '%\"signals\"%' AND i.published_at >= ?"
                  " AND i.url_canonical NOT IN (SELECT url_canonical FROM event_members)", ORPHAN_KEY, since):
        for sid, pts in (json.loads(r["extra"] or "{}").get("signals") or {}).items():
            if sid in signal_ids:
                out.append({**dict(r), "source_id": sid, "extra": json.dumps(_points(pts)),
                            "vec_key": ORPHAN_KEY + r["url_canonical"], "orphan": True})
    return out


def _points(extra):
    """Points and comments of a signal sighting, from the signal item's own extra or, for an article imported
    from a signal's link (fetch.promote_signals), the copy kept under its source id."""
    return {k: extra.get(k) for k in ("points", "comments", "hn_url") if extra.get(k) is not None}


def attach_signals(store):
    """Exact URL first (a sighting of a member URL, or Techmeme's linked article). Otherwise the title's cosine to
    the anchors >= T_signal picks the nearest event, and the judge must rule that the signal is about that event's
    development: on cosine alone, Techmeme's "Bessent urges Nvidia to hire an outside CEO" attached to the
    investors'-letter card and to the "Asian governments react" card, and each got Techmeme's boost. Without a
    judge a cosine match needs a shared non-org entity word in the title.

    Each signal attaches to one event: a match to a new event moves the row."""
    db, now = store.db, store.now
    member_of = {r["url_canonical"]: r["event_id"] for r in db.q("SELECT url_canonical, event_id FROM event_members")}
    signal_ids = {s.id for s in store.srcs.sources if s.is_signal}
    # A Reddit post attaches only through the article it links, never by its title: the title is a reader's own
    # words. Of 23 Reddit posts attached in the third review's run, 15 were title matches, among them "MAGA turns
    # on Lutnick amid China spy ..." on the Megaspeed blacklist card and "US aviation shutdown blamed on L3
    # Harris ..." on the Juniper routers card. Matches made that way before this rule are removed.
    by_url_only = {s.id for s in store.srcs.sources if s.is_signal and s.kind in URL_ONLY_KINDS}
    for sid in by_url_only:
        db.conn.execute("DELETE FROM event_signals WHERE source_id = ? AND how != 'url'", (sid,))
    attached = {(r["url_canonical"], r["source_id"]): (r["event_id"], r["how"])
                for r in db.q("SELECT event_id, url_canonical, source_id, how FROM event_signals")}
    n = 0
    budget = store.s.signal_judgements

    def attach(eid, url, source_id, how, extra):
        nonlocal n
        eid = store.resolve(eid)
        have = attached.get((url, source_id))
        if have and have[0] != eid:
            db.conn.execute("DELETE FROM event_signals WHERE url_canonical = ? AND source_id = ?", (url, source_id))
        n += _attach(db, eid, url, source_id, how, extra, now)
        attached[(url, source_id)] = (eid, how)

    # Sightings: a signal source listed a member's own URL.
    for r in db.q("SELECT s.*, i.extra FROM sightings s JOIN items i ON i.url_canonical = s.url_canonical"):
        if r["source_id"] in signal_ids and r["url_canonical"] in member_of:
            extra = (json.loads(r["extra"] or "{}").get("signals") or {}).get(r["source_id"]) or {}
            attach(member_of[r["url_canonical"]], r["url_canonical"], r["source_id"], "url", _points(extra))
    rows = signal_items(db, store.srcs, now)
    if store.judge is not None:
        # The title matches that need a ruling, newest first, asked in parallel up to the pass's budget.
        need, asked = [], set()
        for r in sorted(rows, key=lambda r: r["published_at"], reverse=True):
            extra = json.loads(r["extra"] or "{}")
            linked = extra.get("linked_url") or r["url_canonical"]
            if linked in member_of or r["url_canonical"] in member_of or r["vec"] is None \
                    or (r["url_canonical"], r["source_id"]) in attached or r["source_id"] in by_url_only:
                continue
            near = _near_events(store, r)
            if near and r["vec_key"] not in asked and any(store.ruling(r["vec_key"], ev.id) is None
                                                          for ev in near):
                asked.add(r["vec_key"])
                # An orphan is ruled on under its own key: the article's ruling as an item asked a different
                # question (is this report the same development), with the page's text.
                need.append((r["vec_key"], r["title"], "" if r.get("orphan") else r["feed_text"] or "", near))
            if len(need) >= budget:
                break
        prefetch_rulings(store, need)
    for r in rows:
        extra = json.loads(r["extra"] or "{}")
        key = (r["url_canonical"], r["source_id"])
        linked = extra.get("linked_url") or r["url_canonical"]
        eid = member_of.get(linked) or member_of.get(r["url_canonical"])
        if eid is not None:
            attach(eid, r["url_canonical"], r["source_id"], "url", extra)
            continue
        if key in attached:
            # Already matched by title: keep the event, refresh the points.
            if attached[key][0] in store.events:
                attach(attached[key][0], r["url_canonical"], r["source_id"], attached[key][1], extra)
            continue
        if r["vec"] is None or r["source_id"] in by_url_only:
            continue
        ranked = store.ranked(embed_mod.from_blob(r["vec"]))
        if not ranked or ranked[0][0] < store.s.t_signal:
            continue
        cos, ev = ranked[0]
        if store.judge is None:
            words = {w for w in re.findall(r"[a-z0-9]+", r["title"].lower()) if len(w) >= 4}
            names = {part for k in ev.keys if k[0] in ("p", "x") for part in str(k[1]).split()}
            if words & names:
                attach(ev.id, r["url_canonical"], r["source_id"], "cosine", extra)
            continue
        # Ruled above, or waiting for a later pass (over budget, or the judge could not be asked). The nearest
        # event the judge calls the same development gets the signal.
        for ev in _near_events(store, r):
            if store.ruling(r["vec_key"], ev.id) == "same":
                attach(ev.id, r["url_canonical"], r["source_id"], "judge", extra)
                break
    return n


SIGNAL_CANDIDATES = 3
URL_ONLY_KINDS = ("reddit",)


def _near_events(store, r):
    """The events a signal's title may be about: the nearest SIGNAL_CANDIDATES at cosine >= T_signal. Only the
    nearest was tried at first, and for a saga it is often a sibling: Hacker News's "SpaceX catches the Starship
    to end Flight 15" sat nearest "SpaceX aborts booster catch" (0.61, rightly ruled a different development)
    and never reached "SpaceX catches Starship upper stage" (0.52)."""
    ranked = store.ranked(embed_mod.from_blob(r["vec"]))
    return [ev for cos, ev in ranked[:SIGNAL_CANDIDATES] if cos >= store.s.t_signal]


def _attach(db, eid, url, source_id, how, extra, now):
    cur = db.conn.execute(
        "INSERT OR IGNORE INTO event_signals VALUES (?,?,?,?,?,?,?,?)",
        (eid, url, source_id, how, extra.get("points"), extra.get("comments"), extra.get("hn_url"), iso(now)))
    if cur.rowcount:
        return 1
    if extra.get("points") is not None:     # points grow; keep the latest
        db.conn.execute("UPDATE event_signals SET points = ?, comments = ? WHERE event_id = ? AND url_canonical = ?"
                        " AND source_id = ?", (extra.get("points"), extra.get("comments"), eid, url, source_id))
    return 0


def _apply_signals(store, ev):
    rows = store.db.q("SELECT * FROM event_signals WHERE event_id = ?", ev.id)
    kinds = {r["source_id"]: (store.by_id[r["source_id"]].kind if r["source_id"] in store.by_id else None)
             for r in rows}
    ev.row["on_techmeme"] = int(any(kinds[r["source_id"]] == "techmeme" or r["source_id"] == "techmeme"
                                    for r in rows))
    hn = [r for r in rows if r["source_id"].startswith("hn")]
    best = max(hn, key=lambda r: r["points"] or 0, default=None)
    ev.row["hn"] = json.dumps({"points": best["points"] or 0, "comments": best["comments"] or 0,
                               "url": best["hn_url"] or best["url_canonical"]}) if best else None
    # Other places a story is being shared, each with its own capped multiplier in rank.coverage: Lobsters by its
    # points, Reddit by the post's place among the day's top posts (fetch.REDDIT_LISTING; 1 = listed, place
    # unknown, for rows attached before places were kept).
    social = {}
    for r in rows:
        kind = kinds[r["source_id"]]
        points = r["points"] or (1 if kind == "reddit" else 0)
        if kind in ("lobsters", "reddit") and points > social.get(kind, 0):
            social[kind] = points
    ev.row["social"] = json.dumps(social) if social else None


# Embedding and LLM candidates

def embed_items(db, cfg, embedder, prepared, now):
    """Embeds the passages of items about to be written (one batched call) and returns their vectors by URL.
    `prepared` is [(row, text)] from llm.write_pending. A held item is re-embedded only if its text changed."""
    if embedder is None or not prepared:
        return {}
    lead_words = cfg["embed"].get("lead_words", 120)
    todo, out = [], {}
    for row, text in prepared:
        inp = embed_mod.item_input(row["title"], text.text if text else "", lead_words)
        sha = hashlib.sha1(inp.encode()).hexdigest()[:16]
        have = db.one("SELECT input_sha, vec FROM embeddings WHERE url_canonical = ?", row["url_canonical"])
        if have is not None and have["input_sha"] == sha:
            out[row["url_canonical"]] = embed_mod.from_blob(have["vec"])
        else:
            todo.append((row["url_canonical"], inp, sha))
    if todo:
        titles = {row["url_canonical"]: row["title"] for row, _ in prepared}
        vecs = _embed(embedder, [t[1] for t in todo], "passage", [titles[t[0]] for t in todo])
        for (url, _, sha), v in zip(todo, vecs):
            db.conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?,?)",
                            (url, "passage", embedder.model, len(v), sha, embed_mod.to_blob(v), iso(now)))
            out[url] = v
    return out


def candidates(db, cfg, vecs, now):
    """Up to 5 events per item with anchor cosine >= T_cand, for the write prompt (spec 2.6)."""
    s = Settings.from_cfg(cfg)
    store = Store(db, sources_mod.SourceList([], []), s, now)
    out = {}
    for url, v in vecs.items():
        out[url] = [{"id": ev.id, "headline": ev.row.get("headline") or ev.anchor.title, "s1": ev.row.get("s1") or ""}
                    for cos, ev in store.ranked(v)[:s.max_candidates] if cos >= s.t_cand]
    return out


def _embed(embedder, texts, input_type, fallbacks=None):
    """Vectors that are never None (Embedder.embed_safe); a test's fake embedder has only `embed`."""
    safe = getattr(embedder, "embed_safe", None)
    return safe(texts, input_type, fallbacks) if safe else embedder.embed(texts, input_type)


def embed_backlog(db, cfg, embedder, now, limit=50):
    """Classified items that missed their embedding (the embedding call failed that tick) get one from their stored
    text, so they can still cluster."""
    if embedder is None:
        return 0
    rows = db.q("SELECT i.url_canonical, i.title, w.text FROM items i JOIN writes w ON w.url_canonical = i.url_canonical"
                " LEFT JOIN embeddings e ON e.url_canonical = i.url_canonical WHERE w.type IS NOT NULL"
                " AND (e.url_canonical IS NULL OR e.model = ?) AND i.status != 'dropped' AND i.published_at >= ?"
                " LIMIT ?", FALLBACK_MODEL, iso(now - timedelta(days=14)), limit)
    if not rows:
        return 0
    lead_words = cfg["embed"].get("lead_words", 120)
    inputs = [embed_mod.item_input(r["title"], r["text"], lead_words) for r in rows]
    vecs = _embed(embedder, inputs, "passage", [r["title"] for r in rows])
    for r, inp, v in zip(rows, inputs, vecs):
        db.conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?,?)",
                        (r["url_canonical"], "passage", embedder.model, len(v),
                         hashlib.sha1(inp.encode()).hexdigest()[:16], embed_mod.to_blob(v), iso(now)))
    return len(rows)


FALLBACK_MODEL = "fallback"
EMBED_ALERT_TICKS = 5
TITLE_MATCH = 0.6
_STOP = set("""the and for with from that this will are was were has have had its into over after about than new says
say said how why what who when where more most can could would should not but you your our their they them his her
him she one two all out off now just also amid back big inc report reports""".split())


def embed_fallback(db, cfg, now, limit=200):
    """Embedding endpoint down: classified items with no vector get a zero vector (model "fallback"), so they
    reach clustering. A zero vector is near nothing; `assign` then matches such an item to an event by title
    words (title_match) or starts its own, so a single-publisher card still ships. embed_backlog replaces the
    zero vectors when the endpoint answers again, and the merge pass and the edition's duplicate guard then join
    what title words kept apart."""
    rows = db.q("SELECT i.url_canonical FROM items i JOIN writes w ON w.url_canonical = i.url_canonical"
                " LEFT JOIN embeddings e ON e.url_canonical = i.url_canonical WHERE w.type IS NOT NULL"
                " AND e.url_canonical IS NULL AND i.status != 'dropped' AND i.published_at >= ? LIMIT ?",
                iso(now - timedelta(days=14)), limit)
    dims = (cfg.get("embed") or {}).get("dims") or 1024
    zero = embed_mod.to_blob(np.zeros(dims, dtype=np.float32))
    for r in rows:
        db.conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?,?)",
                        (r["url_canonical"], "passage", FALLBACK_MODEL, dims, None, zero, iso(now)))
    return len(rows)


def _title_words(title):
    return {w for w in re.findall(r"[a-z0-9$]+", (title or "").lower()) if len(w) > 2 and w not in _STOP}


def title_match(store, item):
    """The live event a vector-less item belongs to by title words: at least TITLE_MATCH of the words of its title
    and a member's are shared (Jaccard), and the event passes the age rules. None when there is none."""
    words = _title_words(item.title)
    if len(words) < 3:
        return None
    best = None
    for ev in store.events.values():
        if ev.anchor is None or joinable(item, ev, 1.0, False, store.now, store.s, judged=True) is not None:
            continue
        for m in ev.members:
            other = _title_words(m.title)
            share = len(words & other) / len(words | other) if other else 0
            if share >= TITLE_MATCH and (best is None or share > best[0]):
                best = (share, ev)
    return best[1] if best else None


def embed_signals(db, embedder, srcs, now, limit=50):
    """Signal headlines get a query vector (they have no text of their own)."""
    if embedder is None:
        return 0
    todo = {}
    for r in signal_items(db, srcs, now):
        if r["vec"] is None:
            todo.setdefault(r["vec_key"], r["title"])
    todo = list(todo.items())[:limit]
    if not todo:
        return 0
    vecs = _embed(embedder, [title for _, title in todo], "query")
    for (key, _), v in zip(todo, vecs):
        db.conn.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,?,?,?)",
                        (key, "query", embedder.model, len(v), None, embed_mod.to_blob(v), iso(now)))
    return len(todo)


# The stage

def run(db, cfg, srcs, now, embedder=None, rewriter=None, judge=None, briefer=None):
    """One clustering pass: assign pending items serially, attach signals, merge, then refresh changed events.
    `rewriter(store, ev)` is the event rewrite call (llm.rewrite_event), skipped when None. `judge(title, text,
    events)` is the same-development ruling (llm.judge_events); without it the entity rule decides.
    `briefer(store, ev)` writes a brief for an event with traction and no full text (llm.brief_event)."""
    s = Settings.from_cfg(cfg)
    stats = Counter()
    if embedder is not None:
        try:
            stats["embedded_backlog"] = embed_backlog(db, cfg, embedder, now)
            stats["signals_embedded"] = embed_signals(db, embedder, srcs, now)
            db.set_meta("embed_failed_ticks", "0")
        except LLMError as e:
            log.warning("embedding failed: %s", e)
            stats["embed_failed"] += 1
            failed = int(db.get_meta("embed_failed_ticks") or 0) + 1
            db.set_meta("embed_failed_ticks", str(failed))
            # The embedding endpoint is the one stage with no second provider (the thresholds are calibrated for
            # its model). Without a vector an item never reached clustering, so no new card appeared while it was
            # down. Written items now cluster by title words until it is back (see embed_fallback).
            stats["embed_fallback"] = embed_fallback(db, cfg, now)
            if failed >= EMBED_ALERT_TICKS:
                from . import health
                health.notify(db, "embeddings_down", f"the embedding endpoint has failed {failed} ticks in a row "
                                                     f"({e}); new items cluster by title words until it answers")
    store = Store(db, srcs, s, now, judge)
    pending = pending_items(db, now)
    if judge is not None:
        jobs = []
        for item in pending:
            _, cands, _ = judged_candidates(store, item, store.ranked(item.vec), None)
            if cands:
                jobs.append((item.url, item.title, item.text or item.headline or "", [ev for _, ev in cands]))
        prefetch_rulings(store, jobs)
    for item in pending:
        eid, how = assign(store, item)
        stats[how] += 1
    stats["signals_attached"] = attach_signals(store)
    stats["merged"] = merge_pass(store)
    stats["twins_merged"] = merge_twins(store)
    tier1_ok = db.check_ok
    # Signals may have changed any event's coverage, so every live event in the window is refreshed; it is cheap.
    for ev in list(store.events.values()):
        refresh(store, ev, tier1_ok)
    if rewriter is not None:
        # Material updates first, then the best-covered events; the rest stay queued for the next tick.
        due = [ev for ev in store.events.values() if due_rewrite(ev.row, now, s)]
        due.sort(key=lambda ev: ("material_update" not in (ev.row.get("rewrite_reason") or ""),
                                 -(ev.row.get("n_publishers") or 0), ev.id))
        for ev in due[:s.max_rewrites]:
            try:
                done = rewriter(store, ev)
            except LLMError as e:       # incl. AuthError: the lead's own text stays; degraded mode is WP4's
                log.warning("event rewrite stopped: %s", e)
                break
            stats["rewritten" if done else "rewrite_failed"] += 1
            refresh(store, ev, tier1_ok)
    if briefer is not None:
        due = [ev for ev in store.events.values() if brief_due(store, ev)]
        due.sort(key=lambda ev: (-(ev.row.get("on_techmeme") or 0), -(ev.row.get("n_publishers") or 0),
                                 -(ev.row.get("importance") or 0), ev.id))
        for ev in due[:MAX_BRIEFS]:
            try:
                done = briefer(store, ev)
            except LLMError as e:
                log.warning("brief stopped: %s", e)
                break
            stats["briefed" if done else "brief_failed"] += 1
            refresh(store, ev, tier1_ok)
    stats["judge_calls"] = store.judge_calls
    db.conn.execute("DELETE FROM judgements WHERE at < ?", (iso(now - timedelta(days=s.candidate_days)),))
    db.conn.execute("DELETE FROM event_alias WHERE at < ?", (iso(now - timedelta(days=s.alias_days)),))
    db.conn.execute("DELETE FROM twins WHERE at < ?", (iso(now - timedelta(days=s.alias_days)),))
    db.commit()
    return stats


def due_rewrite(row, now, s):
    """A rewrite at most every 2 h, except a hard-news material update, which goes straight through."""
    reasons = set(filter(None, (row.get("rewrite_reason") or "").split(",")))
    if not reasons or (row.get("n_publishers") or 0) < 2:
        return False
    # Commentary is each author's own: three analyses of a week-old resignation, rewritten into one card and
    # re-scored as "the event", shipped as "Analysis: Jensen Huang resigns as Nvidia CEO after reports of
    # affairs" at importance 9. A rewrite merges reports of one development, so it needs a report to lead it.
    if row.get("type") not in HARD_NEWS:
        return False
    last = from_iso(row.get("rewritten_at"))
    return "material_update" in reasons or last is None or now - last >= timedelta(hours=s.rewrite_cap_h)


def resolve(db, eid):
    """An id from a push, Saved or a /e/ link, after any merges."""
    row = db.one("SELECT id FROM event_alias WHERE old_id = ?", eid)
    return row["id"] if row else eid
