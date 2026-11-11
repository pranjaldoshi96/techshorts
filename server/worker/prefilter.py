"""Cheap drops before any LLM work (spec section 2.4). Every drop names its rule, which health.json counts.

Bare "deal", "sale" and "review" are deliberately not title rules: "Microsoft closes $69B Activision deal",
"US bans Nvidia chip sale to China" and "FTC opens antitrust review of OpenAI" are news. Product reviews are
removed later by the LLM `type`. "Review:" mid-title ("Huupe Mini 2 Review: More Game...") and
/review/ URL segments (Wired) are safe signals, though.
"""
import re
from urllib.parse import urlsplit

URL_PATHS = ("/deals/", "/coupons/", "/shopping/", "/gift-guide/", "/sponsored/", "/insights/", "/brandstory/",
             "/partner-content/", "/podcast/", "/events/", "/review/", "/reviews/")

TITLE_RULES = [
    ("title_question", r"^(what|why|how|when) (is|are|your|to|you|do|does)\b"),
    ("title_review", r"(^review:|\breview$|\breview:|:\s*review\b)"),
    ("title_fragments", r"^fragments:"),
    ("title_quoting", r"^quoting "),
    ("title_shopping", r"\b(discounted|% off|save \$|best price|lowest price|promo code|coupon)\b"),
    ("title_sponsored", r"\bsponsored\b"),
    ("title_summit", r"\bsummit 20\d\d\b"),
    ("title_podcast", r"\b(podcast|episode)\b"),
    ("title_gift_guide", r"\bgift guide\b"),
    ("title_best_to_buy", r"\bbest .* (to buy|for 20\d\d)\b"),
    # Ported from the 1.2 app's ContentFilter (FeedService.swift), which the live edition still uses.
    ("v1_title", "|".join([
        r"\d+% off", r"\$\d+ off", r"\bon sale\b", r"\b(best|lowest) price", r"all-time low", r"black friday",
        r"cyber monday", r"prime day", r"gift guide", r"coupon", r"promo code", r"\bbest\b.{0,60}\bdeals?\b",
        r"\bdeals? of the (day|week)\b", r"\bsponsored\b", r"^(the )?\d+ best\b", r"\bbest\b.*\(20\d\d\)",
        r"\bfounder summit\b", r"\btechcrunch disrupt\b", r"\bstrictlyvc\b",
        r"\btickets?\b.{0,40}\b(sale|save|discount|price)",
    ])),
]
V1_PATH = re.compile(r"/deals?/|deal-sale|-deals$|coupon|gift-guide|/sponsored", re.I)

_TITLE = [(name, re.compile(rx, re.I)) for name, rx in TITLE_RULES]


def normalize_title(title):
    # Curly apostrophes and non-breaking spaces would otherwise defeat the word patterns.
    return re.sub(r"\s+", " ", title.replace("’", "'").replace(" ", " ")).strip()


def check(title, url):
    """Returns the name of the first rule that drops this item, or None to keep it."""
    path = urlsplit(url).path.lower()
    for p in URL_PATHS:
        if p in path or path.endswith(p.rstrip("/")):
            return "url" + p.rstrip("/").replace("/", "_")
    if V1_PATH.search(path):
        return "v1_path"
    t = normalize_title(title)
    for name, rx in _TITLE:
        if rx.search(t):
            return name
    return None
