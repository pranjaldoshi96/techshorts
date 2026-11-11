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


def select(out, attribution=None):
    """Picks, per sentence, the model's version or its shorter `_alt`, so the summary fits: the combination
    inside both ranges that is closest to the 52/36 targets, else the closest one (whose problems then become
    retry feedback). Likewise the headline. Mutates and returns `out`; text is chosen, never cut."""
    def options(name):
        main, alt = getattr(out, name), getattr(out, name + "_alt", "")
        return [main] + ([alt] if alt and alt != main else [])

    best = None
    for s1 in options("s1"):
        for s2 in options("s2"):
            for s3 in options("s3"):
                n_short, n_full = word_count(join(s1, s2)), word_count(join(s1, s2, s3))
                miss = (max(0, SHORT_MIN - n_short, n_short - SHORT_MAX)
                        + max(0, FULL_MIN - n_full, n_full - FULL_MAX))
                key = (miss, abs(n_full - FULL_TARGET) + abs(n_short - SHORT_TARGET))
                if best is None or key < best[0]:
                    best = (key, s1, s2, s3)
    _, out.s1, out.s2, out.s3 = best
    candidates = [h for h in (out.headline, out.headline_alt) if h]
    # Analysis, opinion and interview pieces carry a label (spec 3). The model rarely adds one even when told, and
    # the label states no fact, so code adds it: "Analysis: ...". A deterministic attribution (Cramer:) is the
    # model's job instead, since the claims must be rewritten as that person's view.
    if attribution is None and out.type in LABELLED_TYPES and not any(PREFIX.match(h) for h in candidates):
        candidates = [f"{out.type.capitalize()}: {h}" for h in candidates]
    fitting = [h for h in candidates if len(h) <= HEADLINE_MAX]
    out.headline = fitting[0] if fitting else candidates[0] if candidates else out.headline
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


def check(out, source_text, title, source_name, attribution=None, author=None, lead=None):
    """Validates one LLM output (llm.WriteOutput) against the item's own text. Returns a Verdict.

    Grounding uses the whole text; the hedge rule looks at the title and the opening of `lead` (see
    HEDGE_SCOPE_WORDS)."""
    v = Verdict(ok=True)
    for code, msg in headline_problems(out.headline):
        v.fail(code, msg)
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

    source = f"{title}\n{source_text}"
    allowed = [*re.findall(r"[\w']+", source_name or ""), *re.findall(r"[\w']+", author or ""),
               *attribution_mod.LABELS]
    if attribution is not None:
        allowed.append(attribution.name)
    ok, bad = grounded(f"{full} {notif}", source, allowed, headline=out.headline or "")
    if not ok:
        v.fail("ungrounded", f"'{bad}' is not in the source text; use only facts from the text.")
    opening = " ".join((lead or source_text or "").split()[:HEDGE_SCOPE_WORDS])
    dropped = hedge_dropped(out.headline or "", f"{title}\n{opening}")
    if dropped:
        v.fail("hedge_dropped", f"Headline drops '{dropped[0]}'; keep the hedge. The source says: "
                                f"\"...{dropped[1]}...\". Keep that word or attribute the claim "
                                f"(\"..., report says\").")
    if attribution is not None and not attribution_mod.has_prefix(out.headline, attribution):
        v.fail("attribution", attribution.feedback())
    elif attribution is None and out.type in LABELLED_TYPES and not PREFIX.match(
            out.headline or ""):
        v.fail("attribution", f"This is {out.type}; start the headline with the author's surname or "
                              f"'{out.type.capitalize()}:'.")
    return v
