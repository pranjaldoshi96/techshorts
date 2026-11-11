"""Scoring and tab assembly (spec 2.11 and 2.12). Pure functions over Candidate records; publish_v1.py loads them.

score = I × C × A × F, region-neutral (the app applies the user's region boost in R2). Freshness F decays from
event_published_at, the publisher's own clock, so a 5-day-old post re-fetched after downtime never looks new; a
material update resets the decay to "3 h old" (age_eff), which is how a story that keeps moving stays up.
"""
import math
from dataclasses import dataclass, field

from .importance import HARD_NEWS

TOP_TYPES = HARD_NEWS
LABELLED = ("analysis", "opinion", "interview")
# Never a card in any tab, whatever the score (spec 2.12).
NEVER_TYPES = {"review", "explainer", "customer_story", "podcast", "promo", "deal"}
DEGRADED_IMPORTANCE = 6
YIELD_FLOOR = 0.25      # drop events below 25% of the tab's median score
DIVERSITY_TOP10 = 2     # lead-publisher cap in ranks 1-10
DIVERSITY_TAB = 4       # and per tab
TUTORIAL_PER_10 = 1     # Dev: at most one tutorial per 10 cards
IMAGE_HAMMING = 6       # dHash distance at or under which two images count as the same picture


@dataclass(frozen=True)
class Tab:
    id: str
    title: str
    types: frozenset
    min_importance: int
    window_h: float
    tau_h: float
    max: int
    min_relevance: int = 2


TABS = {
    "top": Tab("top", "Top", frozenset(TOP_TYPES), 6, 36, 10, 30),
    "ai": Tab("ai", "AI", frozenset(TOP_TYPES + ("analysis", "opinion", "interview", "release_notes")), 5, 72, 18, 25),
    "startups": Tab("startups", "Startups", frozenset(TOP_TYPES + ("analysis", "interview")), 5, 72, 24, 25),
    "india": Tab("india", "India", frozenset(TOP_TYPES + ("analysis",)), 5, 72, 24, 20),
    "dev": Tab("dev", "Dev", frozenset(TOP_TYPES + ("release_notes", "analysis", "tutorial")), 4, 7 * 24, 48, 20),
    "reads": Tab("reads", "Reads", frozenset(("analysis", "opinion", "interview", "other")), 5, 14 * 24, 96, 15),
}
TAB_ORDER = ("top", "ai", "startups", "india", "dev", "reads")
TUNABLE = ("min_importance", "window_h", "tau_h", "max")


def tabs_for(cfg):
    """TABS with any `tabs.<id>.<field>` overrides from config.yaml (spec values when there are none)."""
    over = (cfg or {}).get("tabs") or {}
    out = {}
    for tid, tab in TABS.items():
        o = {k: v for k, v in (over.get(tid) or {}).items() if k in TUNABLE}
        out[tid] = Tab(**{**tab.__dict__, **o}) if o else tab
    return out


@dataclass
class Candidate:
    """One event as ranking sees it. `classified` is False for an item the LLM never saw (degraded mode only):
    it has no type or topics, so the filters that need them are skipped and it can only reach Top."""
    id: str
    importance: int
    type: str
    is_news: bool
    relevance: int
    topics: list
    region: str
    n_publishers: int
    on_techmeme: bool
    hn_points: int
    authority: float
    published_at: object            # event_published_at (datetime, UTC)
    material_at: object = None      # last_material_update_at
    lead_source: str = None         # source id
    lead_publisher: str = None      # v1_name, for the diversity caps
    lead_reads: bool = False
    lead_tier1: bool = False
    ai_text: bool = True            # validated headline + summary exist
    classified: bool = True
    image_hash: str = None          # dHash hex of the real (not generated) image
    headline_vec: object = None     # unit vector of the headline, for the duplicate guard
    boost: float = 1.0              # editorial boost / bury multiplier
    pin: frozenset = frozenset()    # tabs this event is pinned to the head of
    data: dict = field(default_factory=dict)    # whatever publish_v1 needs later (lead row, text, members)

    def age_h(self, now):
        return (now - self.published_at).total_seconds() / 3600

    def age_eff(self, now):
        a = self.age_h(now)
        if self.material_at is not None:
            a = min(a, (now - self.material_at).total_seconds() / 3600 + 3)
        return max(0.0, a)


def coverage(n_publishers, on_techmeme=False, hn_points=0):
    c = min(2.2, 1 + 0.5 * math.log2(max(1, n_publishers)))
    c *= 1.25 if on_techmeme else 1
    c *= min(1.3, 1 + 0.15 * math.log2(max(1, (hn_points or 0) / 100)))
    return c


def score(importance, n_publishers, on_techmeme, hn_points, authority, age_eff_h, tau_h):
    i = (importance / 10) ** 1.5
    return i * coverage(n_publishers, on_techmeme, hn_points) * authority * math.exp(-age_eff_h / tau_h)


def cand_score(c, tab, now, degraded=False):
    imp = DEGRADED_IMPORTANCE if degraded or not c.classified else c.importance
    return score(imp, c.n_publishers, c.on_techmeme, c.hn_points, c.authority, c.age_eff(now), tab.tau_h) * c.boost


def window_h(c, tab):
    if tab.id == "top" and c.importance >= 8:
        return 72
    return tab.window_h


def never_shipped(c):
    """Spec 2.12 "never shipped", for classified events."""
    return (not c.topics or c.relevance < 2 or c.type in NEVER_TYPES)


def eligible(c, tab, now):
    """None when `c` may appear in `tab`, else the first failing rule (kept for health.json and tests)."""
    if c.age_eff(now) > window_h(c, tab) or c.age_h(now) > 14 * 24:
        return "window"
    if not c.classified:
        # Degraded mode only: a tier-1 item the LLM never saw. Top is the only tab that needs no topic.
        return None if tab.id == "top" and c.lead_tier1 else "unclassified"
    if never_shipped(c):
        return "never_shipped"
    if not c.is_news and tab.id not in ("reads", "dev"):
        return "not_news"
    # Top takes analysis, opinion and interview pieces only at importance >= 8.
    if c.type not in tab.types and not (tab.id == "top" and c.type in LABELLED and c.importance >= 8):
        return "type"
    if c.importance < tab.min_importance:
        return "importance"
    if c.relevance < tab.min_relevance:
        return "relevance"
    topics = set(c.topics)
    if tab.id == "ai" and "ai" not in topics:
        return "topic"
    if tab.id == "startups" and "startups_funding" not in topics:
        return "topic"
    if tab.id == "india" and not (c.region == "in" or "india" in topics):
        return "topic"
    if tab.id == "dev" and not topics & {"dev_tools", "security"}:
        return "topic"
    if tab.id == "reads" and not c.lead_reads:
        return "not_reads"
    return None


def hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def assemble(cands, tab, now, degraded=False, dup_threshold=None, stats=None):
    """Filter → drop below 25% of the median → sort → duplicate guard → diversity → image dedupe → cap. Never
    pads. Returns [(candidate, score)] in rank order."""
    pool = []
    for c in cands:
        # An owner's pin is a decision, not a signal: it skips the filters too.
        why = None if tab.id in c.pin else eligible(c, tab, now)
        if why is None:
            pool.append((c, cand_score(c, tab, now, degraded)))
        elif stats is not None:
            stats[why] = stats.get(why, 0) + 1
    if not pool:
        return []
    scores = sorted(s for _, s in pool)
    n = len(scores)
    med = scores[n // 2] if n % 2 else (scores[n // 2 - 1] + scores[n // 2]) / 2
    pinned = [(c, s) for c, s in pool if tab.id in c.pin]
    pool = [(c, s) for c, s in pool if tab.id not in c.pin and s >= YIELD_FLOOR * med]
    pool.sort(key=lambda x: (-x[1], x[0].id))
    out = list(pinned)
    per_pub, tutorials = {}, 0
    for c, _ in pinned:
        pub = c.lead_publisher or c.lead_source
        per_pub[pub] = per_pub.get(pub, 0) + 1

    def is_duplicate(c):
        # The merge pass should have joined these, but the feed must never show one story twice (spec 8 WP4 AC).
        # Headline vectors are query embeddings, where no labelled different-event fixture pair reaches T_join.
        return dup_threshold is not None and c.headline_vec is not None and any(
            o.headline_vec is not None and float(c.headline_vec @ o.headline_vec) >= dup_threshold for o, _ in out)

    def blocked(c, relax):
        """Why `c` can't take the next rank now (it may later), or None."""
        pub = c.lead_publisher or c.lead_source
        cap = DIVERSITY_TAB if relax or len(out) >= 10 else DIVERSITY_TOP10
        if per_pub.get(pub, 0) >= cap:
            return "diversity"
        if tab.id == "dev" and c.type == "tutorial" and tutorials >= TUTORIAL_PER_10 * (1 + len(out) // 10):
            return "tutorial_cap"
        if len(out) < 10 and c.image_hash and any(
                o.image_hash and hamming(c.image_hash, o.image_hash) <= IMAGE_HAMMING for o, _ in out[:10]):
            return "image_duplicate"
        return None

    # Greedy, one rank at a time: a card held back by the top-10 caps (publisher, tutorial, repeated picture) waits
    # for a later rank instead of being dropped. If only capped cards are left before rank 10, the 2-per-publisher
    # rule yields to the 4-per-tab one rather than ending the tab early.
    remaining = list(pool)
    while remaining and len(out) < tab.max:
        pick = None
        for relax in (False, True):
            for i, (c, s) in enumerate(remaining):
                if is_duplicate(c):
                    continue
                if blocked(c, relax) is None:
                    pick = i
                    break
            if pick is not None:
                break
        dups = [x for x in remaining if is_duplicate(x[0])]
        for _ in dups:
            _count(stats, "duplicate")
        if pick is None:
            for c, _ in remaining:
                if not is_duplicate(c):
                    _count(stats, blocked(c, True) or "capped")
            break
        c, s = remaining[pick]
        remaining = [x for j, x in enumerate(remaining) if j != pick and not is_duplicate(x[0])]
        out.append((c, s))
        pub = c.lead_publisher or c.lead_source
        per_pub[pub] = per_pub.get(pub, 0) + 1
        tutorials += c.type == "tutorial"
    return out


def _count(stats, key):
    if stats is not None:
        stats[key] = stats.get(key, 0) + 1


def build_tabs(cands, now, degraded=False, dup_threshold=None, stats=None, tabs=None):
    """{tab id: [(candidate, score)]} for every tab. `stats` collects per-tab filter counts."""
    tabs = tabs or TABS
    out = {}
    for tid in TAB_ORDER:
        s = {} if stats is not None else None
        out[tid] = assemble(cands, tabs[tid], now, degraded, dup_threshold, s)
        if stats is not None:
            stats[tid] = s
    return out
