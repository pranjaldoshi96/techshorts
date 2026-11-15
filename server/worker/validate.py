"""Deterministic trim and hard validation gates (spec 2.8). The model is never trusted to count.

`check` collects every failure so one retry can fix them all; the first failure's code is what health.json
counts (too_long, ungrounded, hedge_dropped, ...). Feedback strings are literal instructions for that retry.
"""
import re
from dataclasses import dataclass, field

from . import attribution as attribution_mod

FULL_MIN, FULL_MAX, FULL_TARGET = 45, 62, 52
SHORT_MIN, SHORT_MAX, SHORT_TARGET = 28, 42, 36
HEADLINE_MAX = 72
NOTIF_MAX = 110
NOTIF_TITLE_MAX = 40

BANNED = re.compile(r"\b(revolutioni[sz]e\w*|game-changer\w*|groundbreaking|delve\w*|landscape|seamless\w*|"
                    r"cutting-edge)\b", re.I)
BOILERPLATE = re.compile(r"\b(subscribe|newsletter|podcast|sign up)\b", re.I)
SUBSCRIBERS = re.compile(r"\bsubscribers?\b", re.I)
MARKDOWN = re.compile(r"\*\*|__|`|^\s*#|\[[^\]]+\]\(|^\s*[-*•] ", re.M)
# No "mine": the model never uses it as a pronoun, and "mine hunting" is news.
FIRST_PERSON = re.compile(r"\b(I|I'm|I've|I'd|me|my|we|We|we're|We're|our|Our|ours|us)\b")
META = re.compile(r"\b(the|this) (article|piece)\b", re.I)

HEDGE_SOURCE = re.compile(r"\b(suspected|alleged|allegedly|reportedly|accused|claims|according to)\b", re.I)
# "according to a regulatory filing" or "according to the statement" cites a document, not an uncertain claim;
# without this every earnings and IPO story would need "reportedly" in its headline.
DOCUMENT_SOURCE = re.compile(r"\s+(?:(?:a|an|the|its|their|his|her|this)\s+)?(?:[\w.'’-]+\s+){0,3}?(?:filings?|"
                             r"documents?|statements?|prospectus|data|figures|results|releases?|announcement|"
                             r"earnings|timetable|schedule|(?:annual|quarterly|earnings) reports?)\b", re.I)
HEDGE_KEPT = re.compile(r"\b(suspected|alleged|allegedly|allegations?|reportedly|reports?|reported|accused|"
                        r"accusations?|claims?|claimed|according to|says|said)\b", re.I)
PREFIX = re.compile(r"^[A-Z][\w.'’ -]{1,40}: ")
HEDGE_WINDOW = 12
LABELLED_TYPES = ("opinion", "analysis", "interview")
# The hedge rule reads the title and the opening of the text, where the headline's claim comes from. A hedge on a
# spec or a side fact further down ("...hours on a charge, SpaceX claims") doesn't bind the headline.
HEDGE_SCOPE_WORDS = 80

# Sentence-initial words that are capitalized only by position; anything else capitalized must be in the source.
FUNCTION_WORDS = set("""a an the it its it's this that these those they their them he his she her there here in on at
for with without after before as but and or nor by from to of now while meanwhile however also both each all some
many most more other others new if when once since until unless although though because so such despite under
over into about against amid among between during through within yet still then thus instead including only even
analysts investors users developers customers critics officials experts shares sales revenue the company""".split())

_WORD_SPLIT = re.compile(r"[\s/()\[\]\"“”‘—–]+")

# A source title that is someone's claim ("Anthropic can retrain Mythos cheaply, says former researcher"): a
# headline whose subject isn't that speaker must keep the attribution.
TITLE_CLAIM = re.compile(r"\b(says|said|claims|claimed)\b", re.I)
# Repetition: s1 must add to the headline, and no two sentences may say the same thing. Shares are over content
# words (>= 3 letters, not function words). In the 2026-11 shadow edition 20 of 61 cards opened with an s1 that
# restated the headline, so a 50-word card carried about 35 words of news.
S1_HEADLINE_MAX = 0.6
SENTENCE_JACCARD_MAX = 0.5
REPORT_PREFIX = "Report: "


# Tense and certainty (the owner's rule: "will issue" must never become "issues"). A source title that plainly
# describes a planned action binds the headline, which restates it: the headline must keep a future or prospective
# form. What counts as "plainly" in the title is narrow (PLAN_MARK, or headline-ese "X to VERB" such as "Sebi to
# issue AI usage guidelines", where the word after "to" must be a verb from FUTURE_VERBS so "Apple brings Siri to
# glasses" or "A guide to Rust" don't read as plans). What counts as keeping it in the headline is wide
# (FUTURE_MARK). The first version used the wide list on both sides and checked the push alert too: on the first
# 150 cards of the second VPS run it failed 8 that were fine ("May I ..." in a quote, "What Earth would be like",
# "Work to replace old copper wire led to ...", an alert stating a different, present fact). Weaker forms and the
# alert's lines are the fact-checker's job (entail.py, rule 2).
PLAN_MARK = re.compile(
    r"\b(will|won't|to be|set to|plans? to|planning to|poised to|expected to|is expected|are expected|slated to|"
    r"scheduled to|about to|aims? to|intends? to|prepares? to|preparing to|soon)\b", re.I)
FUTURE_MARK = re.compile(
    r"\b(will|won't|shall|to be|set to|plans? to|plans|planned|planning|poised to|expected|expects?|likely to|slated|"
    r"scheduled|due to (?:launch|ship|list|open|start|begin|arrive|debut)|about to|aims? to|aiming to|seeks? to|"
    r"seeking to|looks? to|looking to|prepares? to|preparing|readies|intends? to|wants? to|hopes? to|hoping to|"
    r"could|may|might|would|soon|upcoming|coming|next (?:week|month|quarter|year)|in talks|considers?|"
    r"considering|weighs?|weighing|mulls?|mulling|eyes|eyeing|nears?|nearing|proposes?|proposed|proposal|draft|"
    r"threatens? to|vows? to|pledges? to|promises? to|targets?|targeting|in the works|working on|on track|"
    r"wants?|hopes?|interested in|tipped|rumou?red|reportedly|possible|potential|ahead of|awaits?|pending|"
    r"readying|gearing up|explores?|exploring|pushes for|calls for|urges?|asks?|demands?|bids? (?:to|for))\b", re.I)
FUTURE_VERBS = set("""issue launch release unveil announce acquire buy sell raise invest cut lay hire fire open close
shut build bring add ban block sue pay settle appeal testify step resign join leave replace name appoint roll ship
debut list file go become get receive offer start begin stop end expand enter exit merge spin split make take give
allow require face hold host meet visit sign back fund test pilot introduce drop kill retire phase move shift double
triple increase reduce slash hike charge price publish share show reveal discuss review probe investigate vote
decide rule hear fine tax regulate restrict limit lift ease approve reject challenge fight defend seek set use
deploy adopt train power supply provide deliver manufacture produce develop design create turn convert integrate
support work partner team compete lead head run oversee speak address attend return restore reinstate keep remain
stay continue delay postpone cancel scrap suspend halt pause resume restart pull withdraw recall refund compensate
spend borrow repay float delist rename rebrand relocate tighten loosen scale grow shrink wind testify unveil
overhaul revamp tackle curb crack clamp mandate impose levy scrutinise scrutinize examine audit notify inform
detail outline brief renegotiate challenge exit""".split())
# "X to VERB" only counts at the start of the title (or of a segment after ":" or ";"), with a subject made of
# names: "Investors urge Nvidia board to keep doors open" is an infinitive after a finite verb, not a plan.
# Title Case and all-caps titles count too ("Sebi To Issue AI Usage Guidelines", "SEBI TO ISSUE ..."): the first
# version wanted a lower-case "to" and verb, so the rule never saw the feeds that capitalize every word.
FUTURE_TO = re.compile(r"^((?:[A-Z0-9][\w&.'’-]*,?\s+|(?:and|of|the|&|And|Of|The)\s+){1,5})[Tt][Oo]\s+"
                       r"([A-Za-z]{2,})\b")
NOT_SUBJECT = {"how", "why", "what", "when", "where", "who", "ways", "way", "time", "guide", "introduction",
               "intro", "welcome", "road", "path", "response", "reaction", "back", "return", "thanks", "due", "up",
               "here's", "need", "reasons", "reason", "things", "tips", "steps", "want", "ready", "learn",
               # a noun and its infinitive ("Work to replace old copper wire led to ..."), not a subject's plan
               "work", "bill", "law", "bid", "push", "effort", "efforts", "move", "deal", "race", "fight",
               "attempt", "drive", "campaign", "proposal", "rule", "rules", "tool", "tools", "app", "feature",
               "option", "right", "ability", "decision", "failure", "refusal", "pressure", "call", "calls", "plan",
               "plans", "talks", "vote", "order", "pledge", "promise", "threat", "chance", "struggle", "quest"}
# Speculation and causation the writer adds on its own ("The founder's personal spending may have contributed to
# the company's payroll crisis", from a CNBC report that drew no such link): each of these phrasings must be in the
# source text, or the sentence is the writer's inference.
SPECULATION = re.compile(r"\b((?:may|might|could|must) have (?:\w+ )?\w+(?:ed|en|wn|de|ne)|"
                         r"appears to have|seems to have|"
                         r"contributed to|suggests? that|suggesting that|raises? (?:questions|concerns|doubts)|"
                         r"raising (?:questions|concerns|doubts)|signals? that|signalling that|signaling that|"
                         r"paving the way|could lead to|may lead to|likely to lead)\b", re.I)
# Scale words that must be the source's own ("the record fine" from BBC's "among the largest").
SUPERLATIVE = re.compile(r"\b(record|record-breaking|largest|biggest|highest|lowest|worst|fastest|unprecedented|"
                         r"historic|first-ever|all-time|never before)\b", re.I)
# Unnamed sourcing or another outlet's reporting in the opening of the text: the card must say so.
SOURCED = re.compile(
    r"\b(?:people|persons?|sources?|officials?|executives?|employees|insiders?)(?: who (?:are|is|were))? "
    r"(?:familiar with|with (?:direct )?knowledge of|close to|briefed on)\b|"
    r"\b(?:sources|insiders)(?: have)? (?:said|say|told|claim)\b|"
    r"\baccording to (?:\w+ ){0,3}?(?:sources|people|insiders|a person|a report|reports)\b|"
    r"\b(?:unnamed|anonymous) (?:sources?|officials?|employees?)\b|\bcondition of anonymity\b|"
    r"\b(?:The Information|Bloomberg|Reuters|The Wall Street Journal|WSJ|The Financial Times|The New York Times|"
    r"CNBC|Axios|Business Insider|The Verge|TechCrunch|Semafor|Politico|The Washington Post)"
    r"(?: News)?(?: first)? report(?:ed|s)\b", re.I)
SOURCING_KEPT = re.compile(r"\b(sources?|insiders?|people familiar|reportedly|reports?|reported|according to|"
                           r"says|said|alleged|allegedly|allegations?|claims?|claimed|accused|accuses|leaked|"
                           r"leak)\b", re.I)
SOURCED_SCOPE_WORDS = 120
# Only third-party reporting gets the code-added "Report:" label; "claims", "alleged" and "accused" are someone's
# own statement, which the headline must attribute in words (the retry feedback asks for that).
REPORT_HEDGES = ("reportedly",)


def future_marker(text, pattern=FUTURE_MARK):
    """The first sign that `text` describes something planned or expected, or None. With `pattern=PLAN_MARK` only
    the plain forms count (what a source title must show before it binds the headline)."""
    m = pattern.search(text or "")
    if m:
        return m.group(0)
    for segment in re.split(r"[:;|]\s+|\s[–—-]\s", text or ""):
        verb = _headline_plan(segment)
        if verb:
            return f"to {verb}"
    return None


def _headline_plan(segment):
    """The verb of headline-ese "X to VERB" at the start of a title segment ("Sebi to issue ..."), or None."""
    m = FUTURE_TO.match(segment.strip())
    if m and m.group(2).lower() in FUTURE_VERBS and not (
            {w.strip(",").lower() for w in m.group(1).split()} & NOT_SUBJECT):
        return m.group(2).lower()
    return None


# Plan -> done drift on the same verb, wherever the plan is stated. The rule above reads only the source title and
# is satisfied by any prospective word in the headline; it missed "expected to price at $70 to $78" (in the text,
# not the title) shipped as "priced at $70 to $78", and "Sebi to issue" shipped as "Sebi issues ... ahead of
# budget" ("ahead of" counted as keeping the plan). Here every verb the source describes as still to come
# ("to issue", "will sign", "expected to price", "to be priced") is collected, and a card line that uses that verb
# as done ("issues", "signed", "priced") fails unless the source itself reports it done somewhere.
_ADV = r"(?:(?:also|soon|now|still|first|then|not|finally|eventually|formally|officially|[a-z]+ly)\s+)?"
PLANNED_VERB = re.compile(
    r"\b(?:will|won't|won’t|shall|set to|plans? to|planning to|poised to|expected to|expects? to|slated to|"
    r"scheduled to|about to|aims? to|aiming to|intends? to|intending to|prepares? to|preparing to|hopes? to|"
    r"hoping to|looks? to|looking to|seeks? to|seeking to|due to|likely to|going to|yet to|wants? to|vows? to|"
    r"pledges? to|promises? to|threatens? to|in talks to)\s+" + _ADV + r"(be\s+|have\s+)?([a-z]{3,})\b", re.I)
# Too common to say anything: "will make" next to "makes" somewhere else in a card is not drift.
GENERIC_VERBS = set("""be have do get make take go come see say tell use need want keep let help try continue remain
work give put look find know think feel seem become stay run turn show move live hold bring begin start include
allow provide offer face lead follow add set meet mean""".split())
IRREGULAR_DONE = {"sell": ("sold",), "buy": ("bought",), "pay": ("paid",), "build": ("built",), "win": ("won",),
                  "lose": ("lost",), "send": ("sent",), "spend": ("spent",), "rise": ("rose", "risen"),
                  "fall": ("fell", "fallen"), "lay": ("laid",), "leave": ("left",), "speak": ("spoke", "spoken"),
                  "hear": ("heard",), "cut": ("cut",), "hit": ("hit",), "shut": ("shut",), "quit": ("quit",),
                  "split": ("split",), "bid": ("bid",), "ban": ("banned",), "drop": ("dropped",),
                  "scrap": ("scrapped",), "ship": ("shipped",), "step": ("stepped",), "stop": ("stopped",),
                  "strike": ("struck",), "write": ("wrote", "written"), "withdraw": ("withdrew", "withdrawn"),
                  "choose": ("chose", "chosen"), "break": ("broke", "broken"), "freeze": ("froze", "frozen")}
_PARTICIPLE_BASE = {p: b for b, ps in IRREGULAR_DONE.items() for p in ps}
# Words before a verb form that keep it prospective, conditional or negated ("to be priced", "will be issued",
# "if approved", "has not signed").
GOVERNED = re.compile(
    r"^(to|will|won't|shall|be|could|may|might|would|should|can|must|if|whether|until|unless|before|once|when|"
    r"expected|expects?|set|plans?|planning|planned|poised|slated|scheduled|due|likely|about|aims?|aiming|"
    r"intends?|hopes?|hoping|seeks?|seeking|wants?|vows?|pledges?|promises?|threatens?|yet|not|never|"
    r"didn't|doesn't|hasn't|haven't|upcoming|proposed|potential|possible)$", re.I)
GOVERN_WINDOW = 4


def _bases(word, participle=False):
    """The base forms a verb token may stand for: itself, and for a participle after "be"/"have" ("priced",
    "issued", "launched", "sold") the candidates without the ending. A wrong candidate is harmless: it only
    yields done forms that never occur."""
    w = word.lower()
    out = {w}
    if participle:
        if w in _PARTICIPLE_BASE:
            out.add(_PARTICIPLE_BASE[w])
        if w.endswith("ied"):
            out.add(w[:-3] + "y")
        if w.endswith("ed"):
            out |= {w[:-1], w[:-2]}
            if len(w) > 5 and w[-3] == w[-4]:
                out.add(w[:-3])          # "scrapped" -> "scrap"
    return {b for b in out if len(b) >= 3}


def done_forms(base):
    """The forms that state `base` as done or happening: "issues", "issued" for "issue". Not the base form and
    not the gerund ("pricing" is a noun in "IPO pricing")."""
    b = base.lower()
    out = set(IRREGULAR_DONE.get(b, ()))
    if b.endswith("e"):
        out |= {b + "s", b + "d"}
    elif b.endswith("y") and len(b) > 2 and b[-2] not in "aeiou":
        out |= {b[:-1] + "ies", b[:-1] + "ied"}
    elif b.endswith(("s", "x", "z", "ch", "sh")):
        out |= {b + "es", b + "ed"}
    else:
        out |= {b + "s", b + "ed"}
    return out


def planned_verbs(*texts):
    """{base verb: the source's phrase} for every verb `texts` describe as still to come."""
    out = {}
    for text in texts:
        if not text:
            continue
        for m in PLANNED_VERB.finditer(text):
            for b in _bases(m.group(2), participle=bool(m.group(1))):
                if b not in GENERIC_VERBS:
                    out.setdefault(b, " ".join(m.group(0).split()))
        if len(text) < 300:             # headline-ese "X to VERB" is a title form, not prose
            for segment in re.split(r"[:;|]\s+|\s[–—-]\s", text):
                verb = _headline_plan(segment)
                if verb and verb not in GENERIC_VERBS:
                    out.setdefault(verb, f"to {verb}")
    return out


def stated_done(text, forms):
    """The first of `forms` that `text` uses as done: not after "to be", "will", "expected to", "if", "not" and
    the like within the few words before it in the same sentence. None when there is none."""
    words = re.findall(r"[A-Za-z][A-Za-z’'-]*|[.;:!?]", text or "")
    low = [w.lower().replace("’", "'") for w in words]
    for i, w in enumerate(low):
        if w not in forms:
            continue
        governed = False
        for prev in reversed(low[max(0, i - GOVERN_WINDOW):i]):
            if prev in (".", ";", ":", "!", "?"):
                break               # a marker in the sentence before governs nothing here
            if GOVERNED.match(prev):
                governed = True
                break
        if not governed:
            return words[i]
    return None


def done_before_due(line, title, text=""):
    """(the done form in `line`, the source's plan phrase) when `line` states as done a verb the source (title
    and text) only describes as planned; None otherwise. The source reporting the same verb as done anywhere
    ("Sebi issued draft rules in June and will issue final ones") clears it."""
    if not line:
        return None
    source = f"{title or ''}. {text or ''}"
    for base, phrase in planned_verbs(title, text).items():
        forms = done_forms(base)
        used = stated_done(line, forms)
        if used and not stated_done(source, forms):
            return used, phrase
    return None


def realized_by(planned_title, later_title):
    """The verb when `later_title` reports done what `planned_title` describes as coming, on the same verb:
    "Musk backs petition, says he will sign it" -> "Elon Musk signs petition" / "tech execs sign petition".
    None otherwise. (cluster.py: an event's card follows the newest material fact its members hold.)"""
    for base in planned_verbs(planned_title):
        forms = done_forms(base)
        if stated_done(later_title, forms | {base}) and not stated_done(planned_title, forms):
            return base
    return None


def tense_problem(line, title):
    """The title's future marker when the source title describes a planned action and `line` states it with no
    future or prospective form at all; None otherwise."""
    mark = future_marker(title, PLAN_MARK)
    if not mark or not line or future_marker(line):
        return None
    return mark


def invented(pattern, output, source):
    """Phrases matching `pattern` in `output` that the source doesn't contain."""
    hay = (source or "").lower().replace("’", "'")
    out = []
    for m in pattern.finditer(output or ""):
        phrase = m.group(0).lower()
        stem = re.sub(r"(s|ed|ing)$", "", phrase.split()[0]) if " " in phrase else phrase
        probe = phrase if " " not in phrase else phrase.replace(phrase.split()[0], stem, 1)
        if phrase not in hay and probe not in hay and phrase not in out:
            out.append(m.group(0))
    return out


def sourcing_dropped(headline, s1, text, source_name=""):
    """The source's unnamed-sourcing phrase when it sits in the opening of the text and neither the headline nor
    S1 attributes the story (to sources, a report, a named speaker or the outlet); None otherwise."""
    opening = " ".join((text or "").split()[:SOURCED_SCOPE_WORDS])
    m = SOURCED.search(opening)
    if not m:
        return None
    shown = f"{headline} {s1}"
    if SOURCING_KEPT.search(shown) or PREFIX.match(headline or ""):
        return None
    if source_name and re.search(re.escape(source_name.split()[0]), shown, re.I):
        return None
    return m.group(0)


def word_count(s):
    return len((s or "").split())


def join(*sents):
    return " ".join(s.strip() for s in sents if s and s.strip())


@dataclass
class Verdict:
    ok: bool
    reason: str = None                  # first failure, for health.json
    reasons: list = field(default_factory=list)
    feedback: list = field(default_factory=list)
    full: str = None
    short: str = None

    def fail(self, reason, feedback):
        self.ok = False
        self.reason = self.reason or reason
        self.reasons.append(reason)
        self.feedback.append(feedback)


# Per-sentence ranges that add up to the targets; used only to make retry feedback actionable.
SENTENCE_RANGES = {"S1": (16, 21), "S2": (14, 20), "S3": (13, 19)}


def _per_sentence(s1, s2, s3=None):
    parts = [("S1", s1), ("S2", s2)] + ([("S3", s3)] if s3 is not None else [])
    return "; ".join(f"{n} has {word_count(t)} words (needs {SENTENCE_RANGES[n][0]}-{SENTENCE_RANGES[n][1]})"
                     for n, t in parts)


def trim(s1, s2, s3):
    """Returns (full, short, problems). Over 62 words S3 is dropped and S1+S2 becomes full if it reaches 45.
    Short is S1+S2 too and capped at 42, so an over-long summary always ends up retrying in practice."""
    short, full = join(s1, s2), join(s1, s2, s3)
    n_full, n_short = word_count(full), word_count(short)
    problems = []
    if n_full > FULL_MAX:
        full = short
        if n_short < FULL_MIN:
            problems.append(("too_long", f"Full summary was {n_full} words; make it {FULL_TARGET} (shorten S3). "
                                         f"{_per_sentence(s1, s2, s3)}."))
    elif n_full < FULL_MIN:
        problems.append(("too_short", f"Full summary was {n_full} words; make it {FULL_TARGET}. "
                                      f"{_per_sentence(s1, s2, s3)}. Add specific facts from the text."))
    if not SHORT_MIN <= n_short <= SHORT_MAX:
        code = "too_long" if n_short > SHORT_MAX else "too_short"
        problems.append((code, f"S1+S2 was {n_short} words; make it {SHORT_TARGET}. {_per_sentence(s1, s2)}."))
    return full, short, problems


def select(out, attribution=None, hedge_source=None):
    """Picks, per sentence, the model's version or its shorter `_alt`, so the summary fits: the combination
    inside both ranges that is closest to the 52/36 targets, else the closest one (whose problems then become
    retry feedback). Likewise the headline. Mutates and returns `out`; text is chosen, never cut.

    With `hedge_source` (hedge_scope(title, text)), a headline that keeps the source's hedge wins over one that
    drops it; when every offered headline drops it, the best one gets the "Report: " label, the same way code adds
    "Analysis:". The model's feedback retry fixed only about half of the hedge_dropped failures."""
    def options(name):
        out_ = [getattr(out, name)]
        for v in (getattr(out, name + "_alt", ""), getattr(out, name + "_long", "")):
            if v and v not in out_:
                out_.append(v)
        return out_

    # A retry with "don't use 'landscape'" mostly came back unchanged (fixture run 5), while another offered
    # version of that sentence was often clean. So style problems count like a length miss: a clean, fitting
    # combination wins. The gates themselves still run on whatever is picked.
    styled = {}

    def style(sent):
        if sent not in styled:
            styled[sent] = len(summary_problems(sent, None))
        return styled[sent]

    candidates = [h for h in options("headline") + [out.headline_short] if h]
    # Analysis, opinion and interview pieces carry a label (spec 3). The model rarely adds one even when told, and
    # the label states no fact, so code adds it: "Analysis: ...". A deterministic attribution (Cramer:) is the
    # model's job instead, since the claims must be rewritten as that person's view. Each candidate is labelled on
    # its own: a prefixed long headline and an unprefixed short one must not let the short one ship bare.
    if attribution is None and out.type in LABELLED_TYPES:
        candidates = [h if PREFIX.match(h) else f"{out.type.capitalize()}: {h}" for h in candidates]
    elif attribution is not None:
        # The item is this person's opinion whatever the model wrote, so the prefix is a fact about the source;
        # "Jim Cramer:", "Cramer says ..." and ", Cramer says" become the prefix instead of doubling it.
        who = rf"(?:{re.escape(attribution.full)}|{re.escape(attribution.name)})"
        says = re.compile(rf",?\s+{who}\s+(?:says|argues|warns)$")
        lead = re.compile(rf"^{who}(?::|\s+(?:says|argues|warns)(?:\s+that)?)\s+")

        def prefixed(h):
            if attribution_mod.has_prefix(h, attribution):
                return h
            rest = says.sub("", lead.sub("", h))
            return f"{attribution.prefix} {rest[:1].upper()}{rest[1:]}"
        candidates = [prefixed(h) for h in candidates]
    fitting = [h for h in candidates if not headline_problems(h)] or [h for h in candidates if len(h) <= HEADLINE_MAX]
    if hedge_source and fitting:
        hedged = [h for h in fitting if not hedge_problem(h, hedge_source)]
        if hedged:
            fitting = hedged
        elif all((hedge_problem(h, hedge_source) or ("",))[0] in REPORT_HEDGES for h in fitting):
            labelled = [REPORT_PREFIX + h for h in fitting if not PREFIX.match(h)]
            labelled = [h for h in labelled if not headline_problems(h)]
            fitting = labelled + fitting
        # Otherwise the hedge is someone's own claim or an accusation: "Report:" would mislabel it, so the first
        # headline goes to the hedge gate and its feedback asks the model to attribute the claim in words.
    out.headline = fitting[0] if fitting else candidates[0] if candidates else out.headline
    # The push alert never costs a card its place. A title over the limit (the model counts badly: 41 characters
    # failed two cards) gives way to the short headline, which obeys the same rules; a body over the limit to the
    # short first or second sentence. With no fitting title the edition shows the publisher's name.
    # A title that is only a name ("Gary Marcus", "Nvidia") says who, not what happened: it gives way the same way.
    names = [*(out.entities.people or []), *(out.entities.orgs or [])] if getattr(out, "entities", None) else []
    if attribution is not None:
        names += [attribution.full, attribution.name]
    title = (getattr(out, "notif_title", "") or "").strip()
    if not 0 < len(title) <= NOTIF_TITLE_MAX or name_only(title, names):
        short = (out.headline_short or "").strip()
        out.notif_title = short if (0 < len(short) <= NOTIF_TITLE_MAX and not PREFIX.match(short)
                                    and not name_only(short, names)) else ""
    if len(getattr(out, "notif_line", "") or "") > NOTIF_MAX:
        out.notif_line = next((s for s in (getattr(out, "s1_alt", ""), getattr(out, "s2_alt", ""))
                               if 0 < len(s) <= NOTIF_MAX), out.notif_line)
    # Sentences after the headline, so the repeat check sees the headline the card will show.
    best = None
    for s1 in options("s1"):
        for s2 in options("s2"):
            for s3 in options("s3"):
                n_short, n_full = word_count(join(s1, s2)), word_count(join(s1, s2, s3))
                miss = (max(0, SHORT_MIN - n_short, n_short - SHORT_MAX)
                        + max(0, FULL_MIN - n_full, n_full - FULL_MAX))
                bad = style(s1) + style(s2) + (style(s3) if s3 else 0) + len(
                    repeat_problems(out.headline, s1, s2, s3))
                key = ((bad > 0) + (miss > 0), bad, miss, abs(n_full - FULL_TARGET) + abs(n_short - SHORT_TARGET))
                if best is None or key < best[0]:
                    best = (key, s1, s2, s3)
    _, out.s1, out.s2, out.s3 = best
    return out


# Numbers

_CURRENCY = {"$": "$", "us$": "$", "usd": "$", "£": "£", "gbp": "£", "€": "€", "eur": "€", "₹": "₹", "rs": "₹",
             "rs.": "₹", "inr": "₹", "¥": "¥", "a$": "A$", "c$": "C$"}
_SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9,
          "trillion": 1e12, "tn": 1e12, "t": 1e12, "crore": 1e7, "cr": 1e7, "lakh": 1e5}
_NUMBER = re.compile(r"""
    (?<![\w.])
    (?P<cur>US\$|A\$|C\$|Rs\.?\s?|INR\s?|USD\s?|[$£€₹¥])?
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    (?:\s?(?P<word>thousand|million|billion|trillion|crore|lakh|mn|bn|tn|cr)\b|(?P<abbr>[kKmMbBtT])\b)?
    (?P<pct>\s?%|\s?percent\b|\s?per\ cent\b)?
""", re.X | re.I)
_NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r"|thirty|forty|fifty|hundred|dozen)\b", re.I)


@dataclass(frozen=True)
class Number:
    text: str
    value: float
    currency: str = None
    percent: bool = False


def numbers(text):
    out = []
    for m in _NUMBER.finditer(text or ""):
        value = float(m.group("num").replace(",", ""))
        scale = (m.group("word") or m.group("abbr") or "").lower()
        value *= _SCALE.get(scale, 1)
        cur = m.group("cur")
        cur = _CURRENCY.get(cur.strip().lower()) if cur else None
        out.append(Number(m.group(0).strip(), value, cur, bool(m.group("pct"))))
    extra = {"thirty": 30, "forty": 40, "fifty": 50, "hundred": 100, "dozen": 12}
    for m in _NUMBER_WORD.finditer(text or ""):
        w = m.group(1).lower()
        out.append(Number(w, float(_NUMBER_WORDS.get(w, extra.get(w, 0)))))
    return out


def number_grounded(n, source_numbers):
    for s in source_numbers:
        if n.currency and n.currency != s.currency:
            continue
        if n.percent and not s.percent:
            continue
        if abs(n.value - s.value) <= 0.005 * max(abs(n.value), abs(s.value)):
            return True
    return False


# Grounding

def _tokens(sentence):
    for raw in _WORD_SPLIT.split(sentence.strip()):
        for part in raw.split("-"):
            w = re.split(r"['’]", part)[0].strip(",.;:!?*")
            if w:
                yield w


def _in_source(word, hay):
    """Whole-word match, allowing only plural/possessive endings: "China" is not grounded by "Chinese"."""
    w = re.escape(word.lower().replace(".", ""))
    if re.search(rf"(?<![a-z0-9]){w}(?:s|es|'s)?(?![a-z0-9])", hay):
        return True
    return w.endswith("s") and len(w) > 3 and re.search(rf"(?<![a-z0-9]){w[:-1]}(?![a-z0-9])", hay) is not None


def capitalized(text, initial=True):
    """Capitalized tokens. Sentence-initial function words ("The", "Its") are skipped; with initial=False every
    sentence-initial word is, since "Developers ..." or "Startups ..." are capitalized only by position."""
    out = []
    for sentence in re.split(r"(?<=[.!?:;])\s+", text or ""):
        for i, w in enumerate(_tokens(sentence)):
            if not w[0].isupper() or len(w) < 2:
                continue
            if i == 0 and (not initial or w.lower() in FUNCTION_WORDS):
                continue
            out.append(w)
    return out


def grounded(output, source, allowed=(), headline=""):
    """Every number and capitalized token in `output` must appear in `source`. Returns (ok, offending token).

    Numbers are compared by value (within 0.5%) after mapping million/mn/M etc., and currencies must match, so
    "$80 million" grounds "$80M" but "£1.4tn" does not ground "$1.4T". There is no allowlist of months or
    countries: only the source's own name (and attribution labels) may appear without being in the text."""
    src_numbers = numbers(source)
    for n in numbers(f"{headline} {output}"):
        if not number_grounded(n, src_numbers):
            return False, n.text
    hay = re.sub(r"\.", "", (source or "").lower().replace("’", "'"))
    allow = {a.lower() for a in allowed}
    # The headline's first word is its subject (spec 3), so it is checked; summary sentence starts are not.
    for w in capitalized(headline) + capitalized(output, initial=False):
        if any(ch.isdigit() for ch in w) and not any(ch.isalpha() for ch in w):
            continue        # pure numbers were checked above
        if w.lower() in allow:
            continue
        if not _in_source(w, hay):
            return False, w
    return True, None


def hedge_dropped(headline, source):
    """If the source hedges near an entity the headline names, the headline must keep a hedge or attribution.
    Returns (hedge word, source snippet) for a dropped hedge, or None."""
    if HEDGE_KEPT.search(headline) or PREFIX.match(headline):
        return None
    entities = {w.lower() for w in capitalized(headline)}
    if not entities:
        return None
    words = re.findall(r"[\w’']+", source or "")
    lowered = [w.lower().split("'")[0].split("’")[0] for w in words]
    text = " ".join(words)
    for m in HEDGE_SOURCE.finditer(text):
        if m.group(1).lower() == "according to" and DOCUMENT_SOURCE.match(text, m.end()):
            continue
        idx = len(text[:m.start()].split())
        # "according to iFixit" or "SpaceX claims": a headline naming the claimant already attributes the claim.
        hedge = m.group(1).lower()
        claimant = (lowered[idx + 2] if hedge == "according to" and idx + 2 < len(lowered)
                    else lowered[idx - 1] if hedge == "claims" and idx > 0 else None)
        if claimant in entities:
            continue
        window = lowered[max(0, idx - HEDGE_WINDOW): idx + HEDGE_WINDOW + 1]
        if any(_same_entity(w, e) for w in window for e in entities):
            return m.group(1).lower(), " ".join(words[max(0, idx - 6): idx + 8])
    return None


def hedge_scope(title, text):
    """What the hedge rule reads: the source title and the opening of its text (HEDGE_SCOPE_WORDS)."""
    return f"{title}\n" + " ".join((text or "").split()[:HEDGE_SCOPE_WORDS])


def title_claim_dropped(headline, title):
    """A title that reports someone's claim ("X, says Y" / "Y says X") binds a headline whose subject isn't the
    speaker: "Anthropic can retrain Mythos cheaply, says former researcher Coxon" can't ship as "Anthropic can
    retrain Mythos cheaply". Returns ("says", title) for a dropped attribution, or None."""
    if not title or HEDGE_KEPT.search(headline or "") or PREFIX.match(headline or ""):
        return None
    m = TITLE_CLAIM.search(title)
    if not m:
        return None
    before, after = title[:m.start()].rstrip(), title[m.end():].strip()
    if not before or before.endswith((",", "-", "—", ":")):
        speaker = after                 # "claim, says X"
    elif "," in before:
        speaker = before.rsplit(",", 1)[1]      # "claim, X says"
    else:
        speaker = before                # "X says claim"
    words = {w.lower() for w in re.findall(r"[\w’']+", speaker) if len(w) >= 3}
    head = [w.lower().split("'")[0].split("’")[0] for w in re.findall(r"[\w’']+", headline or "")]
    if not words or any(_same_entity(h, w) for h in head for w in words):
        return None                     # the speaker is named: their own claim, or attributed
    return m.group(1).lower(), title


def hedge_problem(headline, scope):
    """hedge_dropped over the hedge scope, plus the title's own claim attribution. (word, snippet) or None."""
    return hedge_dropped(headline, scope) or title_claim_dropped(headline, scope.split("\n", 1)[0])


def label_feedback(headline, own, scope):
    """What to tell the writer when the fact-check rejects a "Report:" label that select() added to its headline:
    the text hedges one claim ("what is reportedly its first mission failure") beside a name the headline uses,
    the headline carried no hedge, and "Report: ULA's Vulcan rocket fails" then labels as someone's reporting what
    the company itself confirmed. "Remove the label" brought the same headline back, and the label with it.
    None when the label was the writer's own or there is no dropped hedge."""
    if not (headline or "").startswith(REPORT_PREFIX) or (own or "").startswith(REPORT_PREFIX):
        return None
    hp = hedge_dropped(headline[len(REPORT_PREFIX):], scope)
    if not hp:
        return None
    return (f"The \"Report:\" label was added because the text hedges with \"{hp[0]}\" (\"...{hp[1]}...\") and "
            f"your headline had no hedge. Write the headline with \"{hp[0]}\" on the claim the text hedges, or "
            f"say who states the headline's fact (\"X says ...\"), so that no label is needed.")


def _content(s):
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9$%.]+", (s or "").lower().replace("’", "'"))
            if len(w) >= 3 and w not in FUNCTION_WORDS}


def _opening(s, n=4):
    """The first `n` words, lowercased and without a label prefix ("Report: "), or None when there are fewer."""
    s = PREFIX.sub("", s or "", count=1)
    words = re.findall(r"[a-z0-9$%']+", s.lower().replace("’", "'"))
    return tuple(words[:n]) if len(words) >= n else None


def repeat_problems(headline, s1, s2, s3=""):
    """s1 restating the headline, or two sentences saying the same thing. [(code, feedback)]."""
    out = []
    c1, ch = _content(s1), _content(headline)
    if c1 and ch and len(c1 & ch) / len(c1) >= S1_HEADLINE_MAX:
        out.append(("repeats_headline", "S1 repeats the headline; the reader has just read it. Open S1 with what "
                                        "the headline lacks: the key number, who, when or how."))
    elif _opening(s1) and _opening(s1) == _opening(headline):
        out.append(("repeats_headline", "S1 opens with the headline's own words; the reader has just read them. "
                                        "Start S1 with a different fact: the key number, who else, when or how."))
    sents = [("S1", c1), ("S2", _content(s2))] + ([("S3", _content(s3))] if s3 else [])
    for i, (na, a) in enumerate(sents):
        for nb, b in sents[i + 1:]:
            if a and b and len(a & b) / len(a | b) > SENTENCE_JACCARD_MAX:
                out.append(("repeats_sentence", f"{na} and {nb} say the same thing; give {nb} a different fact "
                                                 f"from the text."))
    return out


def hedges_in(source, limit=3):
    """Snippets around the source's hedges, for the prompt: telling the model up front is cheaper than a retry."""
    out = []
    for m in HEDGE_SOURCE.finditer(source or ""):
        if m.group(1).lower() == "according to" and DOCUMENT_SOURCE.match(source, m.end()):
            continue
        before = source[:m.start()].split()[-6:]
        after = source[m.end():].split()[:8]
        out.append(" ".join(before + [m.group(1)] + after))
        if len(out) >= limit:
            break
    return out


def _same_entity(word, entity):
    # Prefix matches catch "Nvidia's"/"Chinese"; short entities ("AI") must match whole words.
    return word == entity or (len(entity) >= 4 and word.startswith(entity)) or (
        len(word) >= 4 and entity.startswith(word))


def name_only(title, names=()):
    """True when an alert title is a single word or exactly one of `names` (a person, a company, an outlet)."""
    norm = lambda x: " ".join(re.sub(r"[^\w\s]", " ", (x or "").lower()).split())  # noqa: E731
    t = norm(title)
    return len(t.split()) < 2 or t in {norm(n) for n in names if n}


# A brief (llm.brief_event) is a short card written from the few lines available on a paywalled scoop: S1, and S2
# when the lines hold a second fact. Its lengths are its own; every other check is the full card's.
BRIEF_S1 = (8, 28)
BRIEF_S2 = (6, 26)
# How a card may name an outlet whose listed name is longer.
OUTLET_ALIASES = {"financial times": ("ft",), "wall street journal": ("wsj", "journal"),
                  "new york times": ("nyt", "times"), "washington post": ("post", "wapo"),
                  "associated press": ("ap",), "the information": ("information",)}


def names_outlet(text, outlets):
    """True when `text` says whose reporting it is: one of `outlets` (or its usual short name), or a "Report:" /
    "Sources:" label or "sources say"."""
    low = " " + re.sub(r"[^\w\s:]", " ", (text or "").lower()) + " "
    if re.search(r"\b(report|reports|reported|reportedly|sources?|according to)\b", low):
        return True
    for o in outlets:
        o = re.sub(r"\s*\(.*?\)", "", (o or "").lower()).strip()
        for name in (o, *OUTLET_ALIASES.get(o, ())):
            if name and re.search(rf"\b{re.escape(name)}\b", low):
                return True
    return False


# A card written from a short text (a paywall's opening, a feed blurb) whose text does not itself report the news
# in its title. Bloomberg's "VC Firm Run by Madonna's Manager Has $7 Billion Anthropic Stake" came with 80 words
# of scene ("Dario Amodei was arguably one of the least famous people in the Beverly Hills home he visited in
# 2023 ..."): the card took its headline from the title and filled three sentences with the scene, every one of
# them true to the text and none about the stake. The text must carry the headline: each figure in it, and at
# least half of its names. Otherwise the item is a title with no report under it, and is handled like one
# (llm.write_item: classification only; a brief if the story has traction).
TEASER = "teaser_text"


def teaser_text(headline, body, allowed=()):
    """The first figure or name of `headline` that `body` (the item's text without its title) does not carry,
    when the body does not carry the headline's news; None when it does."""
    h = PREFIX.sub("", headline or "", count=1)
    have = numbers(body)
    for n in numbers(h):
        if not number_grounded(n, have):
            return n.text
    hay = re.sub(r"\.", "", (body or "").lower().replace("’", "'"))
    allow = {a.lower() for a in allowed}
    names = [w for w in capitalized(h) if any(ch.isalpha() for ch in w) and w.lower() not in allow]
    missing = [w for w in names if not _in_source(w, hay)]
    return missing[0] if names and len(missing) > len(names) / 2 else None


def headline_problems(headline):
    out = []
    h = (headline or "").strip()
    if not h:
        return [("headline_format", "Write a headline.")]
    if len(h) > HEADLINE_MAX:
        out.append(("too_long", f"Headline was {len(h)} characters ({word_count(h)} words); the limit is "
                                f"{HEADLINE_MAX} characters. Rewrite it in at most 10 words."))
    if "?" in h:
        out.append(("headline_format", "Headline must not be a question."))
    if h.endswith("."):
        out.append(("headline_format", "Headline must not end with a period."))
    letters = [c for c in h if c.isalpha()]
    if len(letters) > 3 and all(c.isupper() for c in letters):
        out.append(("headline_format", "Headline must be sentence case, not all caps."))
    if MARKDOWN.search(h):
        out.append(("format", "Plain text only, no markdown."))
    return out


def summary_problems(full, source_text):
    out = []
    if MARKDOWN.search(full):
        out.append(("format", "Plain text only, no markdown."))
    if FIRST_PERSON.search(full):
        out.append(("format", f"No first person ('{FIRST_PERSON.search(full).group(0)}'); write in third person."))
    if META.search(full):
        out.append(("format", "Don't refer to 'the article'; state the facts."))
    m = BANNED.search(full)
    if m:
        out.append(("banned_word", f"Don't use '{m.group(0)}'."))
    m = BOILERPLATE.search(full)
    if m:
        out.append(("boilerplate", f"Remove '{m.group(0)}'; describe the news, not the publication."))
    # "subscribers" is real news for Netflix, boilerplate for a newsletter: allowed only if the cleaned text has it.
    m = SUBSCRIBERS.search(full)
    if m and not SUBSCRIBERS.search(source_text or ""):
        out.append(("boilerplate", f"Remove '{m.group(0)}'; describe the news, not the publication."))
    return out


def check(out, source_text, title, source_name, attribution=None, author=None, lead=None, brief=False,
          outlets=(), short_card=False):
    """Validates one LLM output (llm.WriteOutput) against the item's own text. Returns a Verdict.

    Grounding uses the whole text; the hedge rule looks at the title and the opening of `lead` (see
    HEDGE_SCOPE_WORDS). `brief` checks a brief's own lengths (BRIEF_S1, BRIEF_S2) and that it names whose
    reporting it is (`outlets`). `short_card` is a card from a short text (llm.SHORT_TEXT_WORDS): its summary is
    S1 + S2 only, so the third sentence is neither required nor shipped."""
    v = Verdict(ok=True)
    for code, msg in headline_problems(out.headline):
        v.fail(code, msg)
    if brief:
        out.s3 = ""
        full = short = join(out.s1, out.s2)
        n1, n2 = word_count(out.s1), word_count(out.s2)
        if not BRIEF_S1[0] <= n1 <= BRIEF_S1[1]:
            v.fail("too_long" if n1 > BRIEF_S1[1] else "too_short",
                   f"S1 was {n1} words; make it {BRIEF_S1[0]} to {BRIEF_S1[1]}.")
        if n2 and not BRIEF_S2[0] <= n2 <= BRIEF_S2[1]:
            v.fail("too_long" if n2 > BRIEF_S2[1] else "too_short",
                   f"S2 was {n2} words; make it {BRIEF_S2[0]} to {BRIEF_S2[1]}, or leave it empty.")
        if not names_outlet(f"{out.headline} {out.s1}", outlets):
            v.fail("sourcing_dropped", "Say whose reporting this is in the headline or S1: name the outlet "
                                       f"({', '.join(outlets[:3]) or 'the outlet'}) or start with 'Report:'.")
    elif short_card:
        out.s3 = ""
        full, short, problems = trim(out.s1, out.s2, "")
        # Only the S1+S2 range applies: there is no third sentence to make up the full length.
        problems = [p for p in problems if p[1].startswith("S1+S2")]
        full = short
        for code, msg in problems:
            v.fail(code, msg)
        missing = teaser_text(out.headline, source_text, [*re.findall(r"[\w']+", source_name or ""),
                                                           *attribution_mod.LABELS])
        if missing:
            v.fail(TEASER, f"The text does not report the headline's news ('{missing}' is only in the title): "
                           f"write the headline and both sentences from what the text itself states.")
    else:
        full, short, problems = trim(out.s1, out.s2, out.s3)
        for code, msg in problems:
            v.fail(code, msg)
    v.full, v.short = full, short
    for code, msg in summary_problems(join(out.s1, out.s2, out.s3), source_text):
        v.fail(code, msg)
    notif = (out.notif_line or "").strip()
    if not notif:
        v.fail("notif", "Write a notif_line.")
    elif len(notif) > NOTIF_MAX:
        v.fail("too_long", f"notif_line was {len(notif)} characters ({word_count(notif)} words); the limit is "
                           f"{NOTIF_MAX} characters. Rewrite it in at most 12 words.")
    elif notif.rstrip(".").lower() == (out.headline or "").lower():
        v.fail("notif", "notif_line must add new information, not repeat the headline.")
    elif "?" in notif:
        v.fail("notif", "notif_line must state the fact, not ask a question.")
    notif_title = (getattr(out, "notif_title", "") or "").strip()
    if len(notif_title) > NOTIF_TITLE_MAX:
        v.fail("too_long", f"notif_title was {len(notif_title)} characters; the limit is {NOTIF_TITLE_MAX}. "
                           f"Use at most 5 words.")

    source = f"{title}\n{source_text}"
    allowed = [*re.findall(r"[\w']+", source_name or ""), *re.findall(r"[\w']+", author or ""),
               *attribution_mod.LABELS]
    if attribution is not None:
        allowed.append(attribution.name)
    for o in outlets:
        # A brief names whose reporting it is, by the outlet's name or its usual short form ("FT", "WSJ").
        o = re.sub(r"\s*\(.*?\)", "", o or "").strip()
        allowed += re.findall(r"[\w']+", o) + [a.upper() if len(a) <= 4 else a.capitalize()
                                               for a in OUTLET_ALIASES.get(o.lower(), ())]
    ok, bad = grounded(f"{full} {notif}", source, allowed, headline=f"{out.headline or ''}. {notif_title}".strip(". "))
    if not ok:
        v.fail("ungrounded", f"'{bad}' is not in the source text; use only facts from the text.")
    for code, msg in repeat_problems(out.headline, out.s1, out.s2, out.s3):
        v.fail(code, msg)
    dropped = hedge_problem(out.headline or "", hedge_scope(title, lead or source_text))
    if dropped:
        v.fail("hedge_dropped", f"Headline drops '{dropped[0]}'; keep the hedge. The source says: "
                                f"\"...{dropped[1]}...\". Keep that word on the claim the source hedges, or "
                                f"say who states the headline's fact (\"X says ...\", \"..., report says\"); "
                                f"never put the hedge on a fact the source states outright.")
    mark = tense_problem(out.headline, title)
    if mark:
        v.fail("tense_drift", f"Headline states as done what the source says is still to come (the source title "
                              f"says '{mark}': \"{title}\"). Keep the future form: 'to ...', 'will ...', "
                              f"'plans to ...'.")
    else:
        for label, line in (("Headline", out.headline), ("The summary", full), ("The alert title", notif_title),
                            ("The alert", notif)):
            drift = done_before_due(line, title, source_text)
            if drift:
                v.fail("tense_drift", f"{label} says '{drift[0]}', stating as done what the source says is still "
                                      f"to come (\"{drift[1]}\"). Keep the source's future or expected form.")
                break
    shown = f"{out.headline or ''} {full} {notif} {notif_title}"
    for phrase in invented(SPECULATION, shown, source):
        v.fail("speculation", f"'{phrase}' is your inference, not in the source text; state only what the text "
                              f"states, or drop the sentence's claim.")
    for phrase in invented(SUPERLATIVE, shown, source):
        v.fail("superlative", f"'{phrase}' is not in the source text; use the source's own wording for scale.")
    unsourced = sourcing_dropped(out.headline, f"{out.s1} {notif}", lead or source_text, source_name)
    if unsourced:
        v.fail("sourcing_dropped", f"The source attributes this to unnamed sources or a report "
                                   f"(\"{unsourced}\"); say so in the headline or S1 (\"sources say\", "
                                   f"\"{source_name or 'the outlet'} reports\").")
    if attribution is not None and not attribution_mod.has_prefix(out.headline, attribution):
        v.fail("attribution", attribution.feedback())
    elif attribution is None and not brief and out.type in LABELLED_TYPES and not PREFIX.match(
            out.headline or ""):
        v.fail("attribution", f"This is {out.type}; start the headline with the author's surname or "
                              f"'{out.type.capitalize()}:'.")
    return v
