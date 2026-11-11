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


def apply(output, attribution):
    """Forces the type; the prefix is checked in validate.py so a missing one retries with feedback."""
    if attribution is not None:
        output.type = "opinion"
    return output


def has_prefix(headline, attribution):
    return (headline or "").startswith(attribution.prefix + " ")
