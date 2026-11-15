"""Deterministic attribution, decided before the LLM and enforced after it (spec 2.7).

When a rule matches, the item's type is forced to `opinion` and its headline must start with `Name:`, whatever
the model says. This is what stops a CNBC Cramer segment shipping as "Anthropic files IPO paperwork...".
"""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Person-name shaped tokens only ("Jim", "O'Brien", "J.D."), so "OpenAI CEO says" or "Google DeepMind:" don't
# read as a person speaking.
_NAME = r"[A-Z][a-z]+(?:[.'’-][A-Za-z]+)*\.?"
_SAYS = re.compile(rf"^({_NAME}) ({_NAME})(?: says\b|:)")
_BYLINE = re.compile(rf"\s\|\s({_NAME}(?: {_NAME})+)(?:\s*\([^)]*\))?\s*$")
_CRAMER_TITLE = re.compile(r"^(Jim )?Cramer\b")
# First-party company blogs title product pages "Gemini Intelligence: ..." and customer stories "... | Jane Doe";
# neither is a person's opinion, so the title rules don't apply to them.
FIRST_PARTY = {"openai", "google_ai", "anthropic", "huggingface", "github", "cloudflare"}
_NOT_FIRST_NAME = {"The", "A", "An", "This", "That", "How", "Why", "What", "When", "Our", "New"}
# Labels the headline may use as a prefix without them appearing in the source.
LABELS = ("Analysis", "Opinion", "Interview", "Report")


@dataclass(frozen=True)
class Attribution:
    name: str          # the prefix, a surname: "Cramer"
    full: str          # for retry feedback: "Jim Cramer"
    rule: str

    @property
    def prefix(self):
        return f"{self.name}:"

    def feedback(self):
        return f"Source is an opinion by {self.full}; prefix '{self.prefix}'."


def _surname(full):
    full = re.sub(r"^by\s+", "", (full or "").strip(), flags=re.I)
    first = re.split(r",| and | & ", full)[0].strip()
    parts = [p for p in first.split() if p[:1].isupper()]
    return (parts[-1].rstrip(".") if parts else None), first


def _byline(author, title, rule):
    name, full = _surname(author) if author else (None, None)
    if not name:
        m = _BYLINE.search(title)
        if m:
            name, full = _surname(m.group(1))
    return Attribution(name, full, rule) if name else Attribution("Opinion", "a columnist", rule)


def detect(url, title, author=None, source_id=None):
    """Returns the Attribution an item must carry, or None. `author` is the feed byline when there is one."""
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path.lower()
    title = (title or "").replace("’", "'").strip()
    if host.endswith("cnbc.com") and ("/jim-cramer" in path or "/cramer" in path) or _CRAMER_TITLE.search(title):
        return Attribution("Cramer", "Jim Cramer", "cramer")
    if host.endswith("theguardian.com") and "/commentisfree/" in path:
        return _byline(author, title, "guardian_commentisfree")
    if host.endswith("nytimes.com") and "/opinion/" in path:
        return _byline(author, title, "nyt_opinion")
    if source_id in FIRST_PARTY:
        return None
    m = _BYLINE.search(title)
    if m:
        name, full = _surname(m.group(1))
        return Attribution(name, full, "title_byline")
    m = _SAYS.match(title)
    if m and m.group(1) not in _NOT_FIRST_NAME:
        return Attribution(m.group(2).rstrip("."), f"{m.group(1)} {m.group(2)}", "title_says")
    return None


# A column or opinion section with no named columnist rule: the piece is commentary whatever the model says (the
# shadow run's "An overview of Jensen Huang's messy days", a Verge /column/ piece, came back type news, importance 9,
# and led Top). As analysis it carries the "Analysis:" label and reaches Top only at importance >= 8.
COLUMN_PATH = re.compile(r"/(column|columns|opinion|opinions|commentary|op-ed|oped|perspective|perspectives)/")
# importance.HARD_NEWS; importing it here would be circular.
HARD_NEWS = ("news", "launch", "funding", "acquisition", "earnings", "policy", "security_incident")


# AI as the subject or the means of a story, in the source title or the headline. The model filed "South Korea exam
# top scorer admits using generative AI to cheat" (Guardian, BBC) under science at relevance 1, so the app never
# showed it although the prompt says "ai" whenever AI or its effects are the subject.
AI_SUBJECT = re.compile(r"\b(AI|A\.I\.|[Aa]rtificial [Ii]ntelligence|[Gg]enerative AI|ChatGPT|LLMs?|[Cc]hatbots?|"
                        r"GPT-\d[\w.]*|OpenAI|Anthropic|DeepMind|[Dd]eepfakes?|[Mm]achine learning|AI agents?)\b")


def is_column(url):
    return bool(url) and bool(COLUMN_PATH.search(urlsplit(url).path.lower()))


def ai_subject(*texts):
    return any(t and AI_SUBJECT.search(t) for t in texts)


def apply(output, attribution, url=None, title=None):
    """Forces the type; the prefix is checked in validate.py so a missing one retries with feedback. A story whose
    source title or headline is about AI gets the "ai" topic, so the AI tab can take it.

    Relevance is left to the model. This used to raise it to 2 whenever the title mentioned AI, which shipped
    anything with "AI" in its title however off-topic the model judged it (a quiz, a lifestyle piece): the write
    prompt's relevance scale already says a story where AI is the subject or the means is at least 2, and the model
    is the one that has read the article."""
    if attribution is not None:
        output.type = "opinion"
    elif output.type in HARD_NEWS and is_column(url):
        output.type = "analysis"
    if ai_subject(title, getattr(output, "headline", None)):
        topics = list(output.topics or [])
        if "ai" not in topics:
            output.topics = topics + ["ai"]
    return output


def has_prefix(headline, attribution):
    return (headline or "").startswith(attribution.prefix + " ")
