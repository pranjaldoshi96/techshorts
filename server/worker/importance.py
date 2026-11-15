"""Event importance (spec 2.10): the LLM's score, clamped by hard signals it can't see.

One publisher's model score is a guess; how many publishers ran the story, and which ones, is evidence. So the
base value (median of the hard-news members, or the rewrite call's re-score) is capped unless the coverage backs
it, and floored only for a parsed $500M+ round that the coverage already backs.
"""
from dataclasses import dataclass, field
from statistics import median

from . import validate

HARD_NEWS = ("news", "launch", "funding", "acquisition", "earnings", "policy", "security_incident")
GADGET_TOPICS = {"consumer", "hardware"}
COMMENTARY = ("analysis", "opinion", "interview", "explainer")
FUNDING_FLOOR_USD = 500e6
# Floors from traction. The writer scores one article; it cannot see that nine outlets ran the story or that it
# leads Techmeme. In the second VPS test run the scores sat at 4-6 (7 events of 447 at importance 7, one at 8):
# "Bessent urges Nvidia to pick an outsider as its next CEO" (9 publishers, Techmeme) was a 5 and "Megaspeed opens
# internal probe" (8 publishers, Techmeme) a 5, so Top's order was decided by age alone.
WIDE_PUBLISHERS, WIDE_FLOOR = 5, 7          # or Techmeme plus 3 publishers
COVERED_PUBLISHERS, COVERED_FLOOR = 3, 6    # or Techmeme plus 2 publishers
# What Hacker News is discussing matters to the industry whatever one model made of the page.
SHARED_HN = ((600, 6), (300, 5))


@dataclass
class Member:
    """What importance (and the lead choice in cluster.py) needs to know about one event member."""
    url: str
    source_id: str
    publisher: str              # v1_name: TechCrunch and TechCrunch AI are one publisher
    tier1: bool                 # effective tier-1 for the event's region (see tier1_for)
    type: str
    importance: int
    topics: list = field(default_factory=list)
    amounts: list = field(default_factory=list)


@dataclass
class Result:
    raw: float
    value: int
    clamps: list


def tier1_for(source, effective_tier, region):
    """Tier-1 is per region (spec 2.2): India tier-1 sources count for region=in events, the others for global/us."""
    if effective_tier != 1:
        return False
    return (source.region == "in") == (region == "in")


def funding_usd(members):
    """Largest dollar amount named by a funding member, or 0."""
    best = 0.0
    for m in members:
        if m.type != "funding":
            continue
        for a in m.amounts:
            for n in validate.numbers(a):
                if n.currency == "$" and not n.percent:
                    best = max(best, n.value)
    return best


def compute(members, event_type, topics, on_techmeme=False, hn_points=0, rewrite_importance=None):
    hard = [m for m in members if m.type in HARD_NEWS]
    if rewrite_importance is not None:
        raw = float(rewrite_importance)
    else:
        raw = float(median(m.importance for m in (hard or members)))
    value = int(raw + 0.5)                  # half up: a 6.5 median is a 7, not banker's 6
    clamps = []
    publishers = {m.publisher for m in members}
    tier1 = {m.publisher for m in members if m.tier1}

    def cap(limit, name):
        nonlocal value
        if value > limit:
            value = limit
            clamps.append(name)

    # 1. One unconfirmed publisher can't claim more than "notable"; a tier-1 outlet's own report may be a 7 (the
    # report of Jensen Huang's resignation sat at 6 with one publisher while the saga's reactions outranked it).
    if not (len(publishers) >= 2 or on_techmeme or hn_points >= 100):
        cap(7 if tier1 else 6, "single_source")
    # 2. Gadget news from one outlet is never more than a mid-feed item.
    if len(publishers) == 1 and topics and set(topics) <= GADGET_TOPICS:
        cap(5, "gadget")
    # 3. Major-story importance needs major-outlet corroboration.
    corroborated = len(tier1) >= 2 or (len(tier1) >= 1 and on_techmeme)
    if not corroborated:
        cap(7, "no_tier1_corroboration")
    # 4. Release notes and tutorials are never Top material.
    if event_type in ("release_notes", "tutorial"):
        cap(4, "release_notes_tutorial")
    # 5. Commentary on a story is not the story. A feature on a week-old scandal came back 9 ("'Jensen Huang lost
    # his mind': how AI's smartest man fell for the world's oldest trick") and took a Top rank, which analysis gets
    # only at 8 or more; that needs the piece itself to be widely picked up.
    if event_type in COMMENTARY and not (len(publishers) >= 3 or on_techmeme):
        cap(7, "commentary")
    # 6. Coverage is evidence: a hard-news story many outlets ran, or one Techmeme headlines with others behind
    # it, is at least significant (see WIDE_PUBLISHERS). Applied after the caps, which it never contradicts: every
    # floor needs a second publisher or a signal.

    def floor(limit, name):
        nonlocal value
        if value < limit:
            value = limit
            clamps.append(name)

    if event_type in HARD_NEWS:
        if len(publishers) >= WIDE_PUBLISHERS or (on_techmeme and len(publishers) >= COVERED_PUBLISHERS):
            floor(WIDE_FLOOR, "wide_coverage")
        elif len(publishers) >= COVERED_PUBLISHERS or (on_techmeme and len(publishers) >= 2):
            floor(COVERED_FLOOR, "coverage")
    if event_type not in ("release_notes", "tutorial"):
        for points, level in SHARED_HN:
            if hn_points >= points:
                floor(level, "widely_shared")
                break
    # 7. A $500M+ round is major news, but only once clamp 3's corroboration holds.
    if corroborated and event_type == "funding" and funding_usd(members) >= FUNDING_FLOOR_USD and value < 8:
        value = 8
        clamps.append("funding_floor")
    return Result(raw, value, clamps)
