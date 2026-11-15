"""Scoring and tab assembly (spec 2.11 and 2.12). Pure functions over Candidate records; publish_v1.py loads them.

score = I × S × C × A × F: importance, spice, coverage (traction), authority, freshness. Spice is the owner's
direction of 2026-11-15 ("all the spicy news ... that can make a really good headline"): how much a tech-curious
reader would want to tap and forward the story, scored by the writer on the story's own facts. It multiplies the
score (0.64 at spice 1, 1.0 at 5, 1.45 at 10), opens the tabs to a juicy story one or two importance points under
a tab's minimum, and keeps dry routine items (spice <= 2) out of Top unless they are major news.
Region-neutral (the app applies the user's region boost in R2). Freshness F decays from
event_published_at, the publisher's own clock, so a 5-day-old post re-fetched after downtime never looks new; a
material update resets the decay to "3 h old" (age_eff), which is how a story that keeps moving stays up.
"""
import math
import re
from dataclasses import dataclass, field

from .importance import HARD_NEWS

TOP_TYPES = HARD_NEWS
LABELLED = ("analysis", "opinion", "interview")
# Never a card in any tab, whatever the score (spec 2.12).
NEVER_TYPES = {"review", "explainer", "customer_story", "podcast", "promo", "deal", "roundup"}
DEGRADED_IMPORTANCE = 6
YIELD_FLOOR = 0.25      # drop events below 25% of the tab's median score
DIVERSITY_TOP10 = 2     # lead-publisher cap in ranks 1-10
DIVERSITY_TAB = 4       # and per tab
TUTORIAL_PER_10 = 1     # Dev: at most one tutorial per 10 cards
IMAGE_HAMMING = 6       # dHash distance at or under which two images count as the same picture
# One storyline (a story, its follow-ups and its look-alike angles) takes at most this many cards in a tab, the
# freshest/highest scored first. The 2026-11 shadow Top had 3 Nvidia/Huang cards and 3 Anthropic-IPO cards.
# 3 since each development of a saga is now its own card (cluster.py's judge): with 2, a day with five real
# developments in one story showed two of them.
# The cap of 3 holds for the head of a tab (its first STORYLINE_HEAD ranks) and for cards with no traction of
# their own. Past the head, a development that is itself widely covered (STRONG_PUBLISHERS outlets, a Techmeme
# headline or a big Hacker News thread) may still take a rank, up to STORYLINE_MAX: on 2026-11-15 the Nvidia saga
# produced six Techmeme headlines in a day, and the cap cut "Megaspeed opens internal probe" (8 publishers,
# Techmeme) and the petition and investor-letter cards while single-source items of spice 4 filled Top.
STORYLINE_CAP = 3
STORYLINE_HEAD = 12
STORYLINE_MAX = 8
STORYLINE_SHARE = 6     # ... or a sixth of the tab when that is more (storyline_limit)
STORYLINE_MAJOR = 7     # a development of this importance also counts ("Huang agrees to testify", one outlet)
# The story's own main event is never the card the cap cuts: with eight fresher developments of the saga in Top,
# "Jensen Huang resigns as Nvidia CEO" (importance 9, spice 10, a day old) was the one left out.
STORYLINE_EVENT = 8
STRONG_PUBLISHERS = 3
STRONG_HN = 300
STRONG_LOBSTERS = 100
# Spice (see the module docstring). An unscored card (written before spice existed, or unwritten) is neutral.
SPICE_NEUTRAL = 5
SPICY = 7               # at or above: may enter a tab below its importance minimum
SPICY_IMPORTANCE_GAP = 2
SPICY_MAX_H = 36
DRY = 2                 # at or below: not Top material unless importance >= DRY_TOP_IMPORTANCE
DRY_TOP_IMPORTANCE = 8
# Routine news (spice 3-4: "an ordinary funding round, a feature update, earnings in line") that one outlet
# carries and nobody is sharing is not Top material below importance 7; it keeps its place in its own tab. The
# third review's Top had "EaseMyTrip cuts Q2 net loss" and an Urban Company shareholder vote (both spice 4,
# importance 6, one outlet) while spicier stories of importance 5 were cut for room.
ROUTINE = 4
ROUTINE_TOP_IMPORTANCE = 7
# Words that mark a publisher title as tech, for the one case with no classification at all (an LLM outage).
TECH_TITLE = re.compile(
    r"\b(ai|a\.i\.|openai|anthropic|nvidia|google|apple|microsoft|meta|amazon|tesla|spacex|chatgpt|gemini|claude|"
    r"intel|amd|samsung|qualcomm|tsmc|oracle|salesforce|ibm|sony|nintendo|huawei|xiaomi|netflix|spotify|uber|"
    r"airbnb|stripe|tiktok|twitter|xai|deepmind|waymo|palantir|coinbase|binance|stablecoin\w*|blockchain|"
    r"llm|chip|chips|semiconductor|startup|startups|software|app|apps|iphone|android|cloud|data|cyber\w*|hack\w*|"
    r"breach|robot\w*|tech|ipo|funding|raises|crypto|bitcoin|satellite|rocket|gpu|model|coding|developer\w*|"
    r"internet|online|digital|ev|evs|quantum|drone\w*|smartphone\w*|algorithm\w*|outage)\b", re.I)
# Dev is for developers: a dev_tools story whose main topic is dev tools or whose type is a developer release, or a
# real security incident. Company news that merely involves a dev-tools company (funding, earnings, M&A) is not.
DEV_TYPES = ("launch", "release_notes", "tutorial")
NOT_DEV_TYPES = ("funding", "acquisition", "earnings")
DEV_LABELLED_MIN = 5
# A security incident is Dev news only when it hits what developers build or run on: a vulnerability, a package or
# supply chain, a code host, CI, a cloud or API platform, a coding agent. The 2026-11 shadow Dev tab carried a
# Workday outage, a consumer scam explainer, an airline blocking AI agents and a weapons platform, all typed
# security_incident with the "security" topic.
DEV_SECURITY = re.compile(
    r"\b(vulnerab\w*|cves?|cve-[\d-]+|zero-days?|0-days?|exploit\w*|patch\w*|npm|pypi|crates\.io|packages?|"
    r"supply[- ]chain|github|gitlab|bitbucket|ci/cd|repos?|repositor\w+|source code|codebase|apis?|api keys?|sdks?|"
    r"secrets|credentials|aws|azure|gcp|cloud|kubernetes|docker|containers?|linux|kernel|open[- ]source|"
    r"developers?|coding|devops|post-?mortem|backdoor\w*|firmware)\b", re.I)
# Startups is for startups: a big company buying one, or a big company's earnings, is not startup news unless the
# model put startups_funding first ("Samsung in talks to acquire Modine Manufacturing" was [bigtech, startups_funding]).
STARTUP_STRICT_TYPES = ("acquisition", "earnings")
# A card with no validated text is a raw publisher title and a feed excerpt. Outside an LLM outage it never takes a
# Top rank (the 2026-11 shadow led Top with a Verge column whose write failed, excerpt "...follow Terrence O'Brien.
# Opt in for The Stepback here") and scores at most this importance elsewhere, so a written card on the same story wins.
UNWRITTEN_IMPORTANCE = 5
# Fresh tier-1 hard news at importance 5 may enter Top within this many hours of publication (the spec's minimum of
# 6 left a Sunday Top with 2 cards under 12 h while tier-1 outlets ran ordinary but current news).
TOP_FRESH_IMPORTANCE = 5
TOP_FRESH_H = 12
# ... but not an explainer the model typed as news ("How the ClickFix scam captures your computer", a Guardian
# consumer piece typed security_incident at 5): at 5 the lift is for plain reports only.
EXPLAINER_TITLE = re.compile(r"^(how|why|what|who|when|where|here's|inside|the [\w-]+ guide)\b", re.I)
# Once a storyline has a card in a tab, another card of it must be this fresh (age_eff): a fresh angle may sit next
# to the main card, a 3-day-old "targets a $1.4tn valuation" may not sit next to "the IPO lands".
STORYLINE_SECOND_MAX_H = 24
STORYLINE_SECOND_STRONG_H = 48
# Traction opens a tab the way spice does: a story Techmeme headlines, Hacker News is discussing or several
# outlets report may sit up to two importance points under a tab's minimum.
TRACTION_PUBLISHERS = 4
TRACTION_IMPORTANCE_GAP = 2
TRACTION_MAX_H = 36
# A widely shared essay or analysis may take a Top rank at this importance (it needs 8 otherwise), and a Reads
# place whatever its publisher: "All AI-generated code should be Open Source" (HN 896, a personal blog) was in no
# tab because Reads took only listed essay sources.
SHARED_LABELLED_IMPORTANCE = 5
# How a well-covered story stays up: next to the tab's own decay it has a slower tail, whose decay constant grows
# with the number of publishers (x1.25 at 2, x1.5 at 4, x1.79 at 9, at most x2) and a Techmeme headline. With one
# decay for all, a 9-publisher story a day old (the Starship catch) scored below every fresh single-source item
# and sat last of Top's 30. STAY_TAIL keeps spec 2.11's property that Top's first card changes within a day: at
# 24 h the tail of the best-covered story possible is 0.42 x e^-1.2 = 0.13, under a fresh importance-6 story.
STAY_MAX = 2.0
STAY_TAIL = 0.42
WIDE_WINDOW_H = 48      # Top's window for a story with WIDE_PUBLISHERS outlets (72 h at importance 8+, 36 h otherwise)
WIDE_PUBLISHERS = 5
# Push alerts (notification_score): candidates need this much spice, and labelled commentary needs to be major.
NOTIFY_MIN_SPICE = 7
NOTIFY_LABELLED_IMPORTANCE = 8
NOTIFY_BREAKING_H = 2


@dataclass(frozen=True)
class Dedup:
    """When two cards' headlines are one story (calibrated by tools/label_pairs.py on headline query vectors):
    cosine >= `alone`, or cosine >= `t` with a shared non-org entity (person, product, amount)."""
    alone: float
    t: float = None

    @classmethod
    def from_cfg(cls, cfg):
        d = (cfg or {}).get("dedup") or {}
        return cls(alone=d.get("t_headline_alone", 0.65), t=d.get("t_headline", 0.5))

    def cosine(self, a, b):
        if a.headline_vec is None or b.headline_vec is None:
            return None
        return float(a.headline_vec @ b.headline_vec)

    def duplicate(self, a, b):
        cos = self.cosine(a, b)
        if cos is None:
            return False
        return cos >= self.alone or (self.t is not None and cos >= self.t and bool(a.keys & b.keys))


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
    # Caps (the last number) were 30/25/25/20/20/15: the VPS test edition had 77 cards against the live feed's 235,
    # and 80 well-covered events of the last 38 h (importance 6+, spice 7+, 2+ publishers or Techmeme) were cut.
    # The owner asked for good stories, not fewer. Nothing is padded: the filters and the 25%-of-median floor decide.
    "top": Tab("top", "Top", frozenset(TOP_TYPES), 6, 36, 10, 60),
    "ai": Tab("ai", "AI", frozenset(TOP_TYPES + ("analysis", "opinion", "interview", "release_notes")), 5, 72, 18, 40),
    "startups": Tab("startups", "Startups", frozenset(TOP_TYPES + ("analysis", "interview")), 5, 72, 24, 40),
    "india": Tab("india", "India", frozenset(TOP_TYPES + ("analysis",)), 5, 72, 24, 30),
    "dev": Tab("dev", "Dev", frozenset(TOP_TYPES + ("release_notes", "analysis", "tutorial")), 4, 7 * 24, 48, 30),
    "reads": Tab("reads", "Reads", frozenset(("analysis", "opinion", "interview", "other")), 5, 14 * 24, 96, 25),
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
    keys: frozenset = frozenset()   # non-org entity keys of the members (cluster.entity_keys)
    storyline: str = None           # id shared by the cards of one storyline (publish_v1.storylines)
    title: str = ""                 # the headline the card shows (or the lead's title), for text rules
    spice: int = SPICE_NEUTRAL      # 1-10, how tap-worthy the story is (the writer's score)
    lobsters_points: int = 0
    on_reddit: bool = False
    github_stars: int = 0
    # The lead is an article imported from a Hacker News or Lobsters link under the catch-all source: nobody
    # chose the site (publish_v1.event_candidate).
    unvetted: bool = False
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


def coverage(n_publishers, on_techmeme=False, hn_points=0, lobsters_points=0, on_reddit=False, github_stars=0):
    """Traction: how widely the story is covered and shared. Each platform has its own capped multiplier, so no
    single one decides the order."""
    c = min(2.2, 1 + 0.5 * math.log2(max(1, n_publishers)))
    c *= 1.25 if on_techmeme else 1
    c *= min(1.3, 1 + 0.15 * math.log2(max(1, (hn_points or 0) / 100)))
    c *= min(1.15, 1 + 0.075 * math.log2(max(1, (lobsters_points or 0) / 25)))
    c *= 1.1 if on_reddit else 1            # among a subreddit's top posts of the day
    c *= min(1.1, 1 + 0.05 * math.log2(max(1, (github_stars or 0) / 100)))     # stars today on GitHub trending
    return c


def traction(c):
    """coverage() of a candidate, with every signal it carries."""
    return coverage(c.n_publishers, c.on_techmeme, c.hn_points, c.lobsters_points, c.on_reddit, c.github_stars)


def spice_factor(spice):
    """0.64 at 1, 1.0 at 5, 1.45 at 10."""
    return 1 + 0.09 * (min(10, max(1, spice or SPICE_NEUTRAL)) - SPICE_NEUTRAL)


def score(importance, n_publishers, on_techmeme, hn_points, authority, age_eff_h, tau_h, spice=SPICE_NEUTRAL,
          lobsters_points=0, on_reddit=False, github_stars=0):
    i = (importance / 10) ** 1.5
    return (i * spice_factor(spice) * coverage(n_publishers, on_techmeme, hn_points, lobsters_points, on_reddit,
                                               github_stars) * authority * math.exp(-age_eff_h / tau_h))


def staying_power(c):
    """The factor on a tab's decay constant in a well-covered story's slow tail (see STAY_MAX)."""
    return min(STAY_MAX, 1 + 0.25 * math.log2(max(1, c.n_publishers)) + (0.15 if c.on_techmeme else 0))


def cand_score(c, tab, now, degraded=False):
    imp = DEGRADED_IMPORTANCE if degraded or not c.classified else c.importance
    if not c.ai_text and not degraded:
        imp = min(imp, UNWRITTEN_IMPORTANCE)
    spice = SPICE_NEUTRAL if degraded or not c.classified else c.spice
    age = c.age_eff(now)
    base = score(imp, c.n_publishers, c.on_techmeme, c.hn_points, c.authority, 0, tab.tau_h, spice,
                 c.lobsters_points, c.on_reddit, c.github_stars) * c.boost
    return base * max(math.exp(-age / tab.tau_h), STAY_TAIL * math.exp(-age / (tab.tau_h * staying_power(c))))


def notification_score(c, now=None):
    """The order to push alerts in: spice x importance x traction (the owner's ranking), with spice squared and
    recency counted when `now` is given.

    Spice is squared because the alert exists to be tapped: with the three weighted alike, "Applied Digital seeks
    guarantees" (spice 6, 2 publishers, Techmeme) ranked 4th and "ULA loses Vulcan and 40 Amazon satellites"
    (spice 8, one publisher, an hour old) 12th. Recency halves the score over a card's first hours, and breaking
    news (importance 6+, under 2 h old) gets a lift, since a single-source story minutes old has had no time to be
    picked up and would otherwise always trail a morning story with three publishers."""
    s = (c.spice / 10) ** 2 * (c.importance / 10) * traction(c)
    if now is not None:
        age = c.age_eff(now)
        s *= 0.5 + 0.5 * math.exp(-age / 4)
        if age <= NOTIFY_BREAKING_H and c.importance >= 6:
            s *= 1.3
    return s


def notifiable(c):
    """May this card be a push-alert candidate at all? Not below spice 7 (a flat alert teaches people to ignore
    them), not commentary unless it is major, and not a site nobody vetted until a second outlet has the story."""
    if c.spice < NOTIFY_MIN_SPICE:
        return False
    if c.type in LABELLED and c.importance < NOTIFY_LABELLED_IMPORTANCE:
        return False
    return not (c.unvetted and not corroborated(c))


def has_traction(c):
    return c.n_publishers >= 2 or c.on_techmeme or c.hn_points >= 100 or c.lobsters_points >= 40 or c.on_reddit


def strong_traction(c):
    """Widely covered or widely shared in its own right (see STORYLINE_CAP)."""
    return (c.n_publishers >= STRONG_PUBLISHERS or c.on_techmeme or c.hn_points >= STRONG_HN
            or c.lobsters_points >= STRONG_LOBSTERS)


def widely_shared(c):
    """What readers are passing around, whoever published it."""
    return c.on_techmeme or c.hn_points >= STRONG_HN or c.lobsters_points >= STRONG_LOBSTERS or c.on_reddit


def corroborated(c):
    """A second outlet or an editor's pick stands behind the lead's report."""
    return c.n_publishers >= 2 or c.on_techmeme


def traction_pass(c, tab, now):
    """A story the tech world is plainly following may sit up to two importance points under a tab's minimum
    (see TRACTION_PUBLISHERS): Techmeme's headline of the hour scored importance 4-5 was in no tab."""
    gap = tab.min_importance - c.importance
    if not (c.ai_text and 0 < gap <= TRACTION_IMPORTANCE_GAP and c.age_eff(now) <= TRACTION_MAX_H):
        return False
    return widely_shared(c) or c.n_publishers >= TRACTION_PUBLISHERS


def spicy_pass(c, tab, now):
    """A juicy story may sit up to two importance points under a tab's minimum: the owner wants the founder feud
    and the leaked memo, which the importance scale rightly rates 4-5. Under the minimum by two it also needs
    traction (a second publisher, Techmeme or HN), so one outlet's gossip item doesn't get there alone."""
    gap = tab.min_importance - c.importance
    if not (c.ai_text and c.spice >= SPICY and 0 < gap <= SPICY_IMPORTANCE_GAP and c.age_eff(now) <= SPICY_MAX_H):
        return False
    if EXPLAINER_TITLE.match(c.title or ""):
        return False
    return gap == 1 or has_traction(c)


def window_h(c, tab):
    if tab.id == "top" and c.importance >= 8:
        return 72
    if tab.id == "top" and (c.n_publishers >= WIDE_PUBLISHERS or (c.on_techmeme and c.n_publishers >= 3)):
        return max(tab.window_h, WIDE_WINDOW_H)
    return tab.window_h


def never_shipped(c):
    """Spec 2.12 "never shipped", for classified events."""
    return (not c.topics or c.relevance < 2 or c.type in NEVER_TYPES)


def top_fresh(c, now):
    """Fresh tier-1 hard news at importance 5 (see TOP_FRESH_IMPORTANCE)."""
    return (c.importance >= TOP_FRESH_IMPORTANCE and c.lead_tier1 and c.ai_text and c.type in TOP_TYPES
            and c.age_eff(now) <= TOP_FRESH_H and not EXPLAINER_TITLE.match(c.title or ""))


def dev_security(c):
    return "dev_tools" in c.topics or bool(DEV_SECURITY.search(c.title or ""))


def eligible(c, tab, now, degraded=False):
    """None when `c` may appear in `tab`, else the first failing rule (kept for health.json and tests).
    `degraded` is an LLM-outage edition, the only time an unwritten card may take a Top rank."""
    if c.age_eff(now) > window_h(c, tab) or c.age_h(now) > 14 * 24:
        return "window"
    if tab.id == "top" and not c.ai_text and not degraded and tab.id not in c.pin:
        return "no_text"
    if not c.classified:
        # Degraded mode only: a tier-1 item the LLM never saw. Top is the only tab that needs no topic. Nothing has
        # judged whether it is tech at all (an outage test put a Verge entertainment interview at Top rank 1), so
        # it needs a second publisher or a tech word in its title.
        if tab.id == "top" and c.lead_tier1 and (c.n_publishers >= 2 or TECH_TITLE.search(c.title or "")):
            return None
        return "unclassified"
    if never_shipped(c):
        return "never_shipped"
    # Top takes analysis, opinion and interview pieces only at importance >= 8, or from 5 when the piece itself is
    # widely shared (a Techmeme headline, a big Hacker News thread) or three outlets carry it. Such a piece is
    # not "news" to the writer (an essay reports nothing), so it is exempt from the not-news rule as well: "All
    # AI-generated code should be Open Source" (HN 954) was kept out of Top by it.
    labelled_top = tab.id == "top" and c.type in LABELLED and (c.importance >= 8 or (
        (widely_shared(c) or c.n_publishers >= STRONG_PUBLISHERS) and c.ai_text
        and c.importance >= SHARED_LABELLED_IMPORTANCE))
    if not c.is_news and tab.id not in ("reads", "dev") and not labelled_top:
        return "not_news"
    if c.type not in tab.types and not labelled_top:
        return "type"
    if c.importance < tab.min_importance and not (tab.id == "top" and top_fresh(c, now)) \
            and not spicy_pass(c, tab, now) and not traction_pass(c, tab, now):
        return "importance"
    # A hard-news report from a site nobody chose (imported from a Hacker News or Lobsters link) needs a second
    # outlet or a Techmeme pick before it can take a Top rank: the fact-check reads the card against the page, not
    # the page's reliability. An essay is its author's view and is labelled as such.
    if tab.id == "top" and c.unvetted and c.type not in LABELLED and not corroborated(c):
        return "unvetted"
    if tab.id == "top" and c.spice <= DRY and c.importance < DRY_TOP_IMPORTANCE:
        return "dry"
    if tab.id == "top" and c.spice <= ROUTINE and c.importance < ROUTINE_TOP_IMPORTANCE and not has_traction(c):
        return "routine"
    if c.relevance < tab.min_relevance:
        return "relevance"
    topics = set(c.topics)
    if tab.id == "ai" and "ai" not in topics:
        return "topic"
    if tab.id == "startups" and ("startups_funding" not in topics or (
            c.type in STARTUP_STRICT_TYPES and c.topics[0] != "startups_funding")):
        return "topic"
    if tab.id == "india" and not (c.region == "in" or "india" in topics):
        return "topic"
    # Dev is developer news: dev tools, or a security incident with a developer angle (DEV_SECURITY; the LLM also
    # tags "security" for national or physical safety stories, consumer scams and SaaS outages, which don't belong
    # here). The LLM adds dev_tools to much company news (a rocket launch, CoreWeave earnings, a bike startup's
    # round) and to consumer AI apps, so it must be the main topic, or come second on release notes or a tutorial.
    if tab.id == "dev":
        dev = ("dev_tools" in topics and c.type not in NOT_DEV_TYPES
               and (c.topics[0] == "dev_tools" or c.type in ("release_notes", "tutorial")))
        sec = "security" in topics and c.type == "security_incident" and dev_security(c)
        if not (dev or sec):
            return "topic"
        if c.type in LABELLED and c.importance < DEV_LABELLED_MIN:
            return "importance"
    if tab.id == "reads" and not (c.lead_reads or (widely_shared(c) and c.ai_text)):
        return "not_reads"
    return None


def storyline_worth(c, now):
    """What a card is worth to its storyline when the cap must choose: spice x importance x traction, recency
    counted (notification_score). Commentary on the story counts half: the cap is there to show developments."""
    return notification_score(c, now) * (0.5 if c.type in LABELLED else 1)


def storyline_limit(tab):
    """The most cards one storyline may take in `tab`: STORYLINE_MAX, a sixth of a long tab (10 of Top's 60)."""
    return max(STORYLINE_MAX, tab.max // STORYLINE_SHARE)


def storyline_cut(cands, tab, now):
    """The ids the storyline cap cuts from `tab`, decided before any rank is given.

    A storyline with more than STORYLINE_CAP eligible cards keeps its best by spice x importance x traction
    (notification_score, the owner's ranking, with its recency term): the best STORYLINE_CAP whatever they are,
    the story's main event always, and further cards that are worth a rank of their own (traction, a major
    development, or spice) up to storyline_limit. The cap used to take cards in the tab's score order, which is
    mostly freshness, and stop: in the third review's Top the Nvidia saga's eight places went to the eight
    freshest developments while "Huang told Nvidia exec 'I hope you die,' Post reports" (spice 8, Techmeme) and
    the resignation report itself (spice 9, 4 publishers) were cut."""
    groups = {}
    for c in cands:
        if c.storyline:
            groups.setdefault(c.storyline, []).append(c)
    limit, cut = storyline_limit(tab), set()
    for members in groups.values():
        if len(members) <= STORYLINE_CAP:
            continue
        kept = 0
        for i, c in enumerate(sorted(members, key=lambda c: (-storyline_worth(c, now), c.id))):
            if i < STORYLINE_CAP or c.importance >= STORYLINE_EVENT or (kept < limit and (
                    strong_traction(c) or c.importance >= STORYLINE_MAJOR or c.spice >= SPICY)):
                kept += 1
            else:
                cut.add(c.id)
    return cut


def hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def assemble(cands, tab, now, degraded=False, dup_threshold=None, stats=None, ruled_apart=None):
    """Filter → drop below 25% of the median → sort → duplicate guard → diversity → storyline → image
    dedupe → cap. Never pads. Returns [(candidate, score)] in rank order. `dup_threshold` is a Dedup, or a bare cosine.
    `ruled_apart(a_id, b_id)`: the clustering judge has ruled the two events different developments."""
    dedup = dup_threshold if isinstance(dup_threshold, Dedup) or dup_threshold is None else Dedup(dup_threshold)
    pool = []
    for c in cands:
        # An owner's pin is a decision, not a signal: it skips the filters too.
        why = None if tab.id in c.pin else eligible(c, tab, now, degraded)
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
    per_pub, per_story, tutorials = {}, {}, 0
    for c, _ in pinned:
        pub = c.lead_publisher or c.lead_source
        per_pub[pub] = per_pub.get(pub, 0) + 1
        if c.storyline:
            per_story[c.storyline] = per_story.get(c.storyline, 0) + 1

    def is_duplicate(c):
        # The merge pass should have joined these, but the feed must never show one story twice (spec 8 WP4 AC).
        # Two cards the edition-wide guard deliberately kept are not duplicates here either: a follow-up and its
        # parent, and a pair the judge ruled different developments. Without these exemptions the tab dropped
        # exactly the saga's developments the judge had separated ("Musk says he will sign petition for Huang's
        # Nvidia return", 4 publishers, against "Huang discusses possible return as Nvidia chief").
        return dedup is not None and any(dedup.duplicate(c, o) and not _kept_apart(c, o, ruled_apart)
                                         for o, _ in out)

    def blocked(c, relax):
        """Why `c` can't take the next rank now (it may later), or None."""
        pub = c.lead_publisher or c.lead_source
        # 4 per publisher in a tab of up to 32 cards, an eighth of a longer one (7 of Top's 60).
        cap = max(DIVERSITY_TAB, tab.max // 8) if relax or len(out) >= 10 else DIVERSITY_TOP10
        if per_pub.get(pub, 0) >= cap:
            return "diversity"
        n_story = per_story.get(c.storyline, 0) if c.storyline else 0
        # The cap's cut (story_cut, decided up front by spice x importance x traction), and its head rule: at
        # most STORYLINE_CAP of a storyline's kept cards in the tab's first ranks; the others wait for a later one.
        if c.id in story_cut or (n_story >= STORYLINE_CAP and len(out) < STORYLINE_HEAD):
            return "storyline"
        if n_story and c.age_eff(now) > (STORYLINE_SECOND_STRONG_H if strong_traction(c)
                                         else STORYLINE_SECOND_MAX_H):
            return "storyline_stale"
        if tab.id == "dev" and c.type == "tutorial" and tutorials >= TUTORIAL_PER_10 * (1 + len(out) // 10):
            return "tutorial_cap"
        if len(out) < 10 and c.image_hash and any(
                o.image_hash and hamming(c.image_hash, o.image_hash) <= IMAGE_HAMMING for o, _ in out[:10]):
            return "image_duplicate"
        return None

    story_cut = storyline_cut([c for c, _ in pool], tab, now)

    # Greedy, one rank at a time: a card held back by the top-10 caps (publisher, tutorial, repeated picture) waits
    # for a later rank instead of being dropped. If only capped cards are left before rank 10, the 2-per-publisher
    # rule yields to the 4-per-tab one rather than ending the tab early.
    remaining = list(pool)
    while remaining and len(out) < tab.max:
        if story_cut and all(c.id in story_cut or is_duplicate(c) or blocked(c, True) for c, _ in remaining):
            # Nothing else can take a rank and the tab has room: the cap exists to leave room for other stories,
            # so with none left waiting its cut cards take the remaining places, after everything else.
            story_cut = set()
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
        if c.storyline:
            per_story[c.storyline] = per_story.get(c.storyline, 0) + 1
        tutorials += c.type == "tutorial"
    return out


def _kept_apart(a, b, ruled_apart=None):
    if a.data.get("parent_id") == b.id or b.data.get("parent_id") == a.id:
        return True
    return bool(ruled_apart and ruled_apart(a.id, b.id))


def _count(stats, key):
    if stats is not None:
        stats[key] = stats.get(key, 0) + 1


def build_tabs(cands, now, degraded=False, dup_threshold=None, stats=None, tabs=None, ruled_apart=None):
    """{tab id: [(candidate, score)]} for every tab. `stats` collects per-tab filter counts."""
    tabs = tabs or TABS
    out = {}
    for tid in TAB_ORDER:
        s = {} if stats is not None else None
        out[tid] = assemble(cands, tabs[tid], now, degraded, dup_threshold, s, ruled_apart)
        if stats is not None:
            stats[tid] = s
    return out
