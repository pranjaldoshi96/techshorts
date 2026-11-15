"""When the news happened, as opposed to when an article about it was published or fetched.

A card's date, its freshness and its push-alert eligibility come from the event's clock (events.published_at).
That clock was the publisher's timestamp of the first report, which is right for a report of something that just
happened and wrong for a late write-up: in the third review's edition "Apple starts selling HomeView" was shown
as 15 minutes old and queued for a push alert because PYMNTS wrote it up on Sunday evening ("Apple began selling
its HomeView Friday (Nov. 13)"), and "Mercor raises $750 million" as 8 hours old from a Mint piece on a round
"announced on Tuesday".

Three sources, each only ever moving a date back:
  - the page's own publication time (text.page_published: a feed may carry the time of a later update, or none);
  - the writer's `news_date` (llm.PROMPT), accepted only when the text itself carries that date (`grounded`);
  - a plain statement of the day in the article's first sentence (`lede_date`), for when the writer gives none.
`news_at` returns the result for one written item; cluster.refresh applies the lead's to the event.
"""
import re
from datetime import datetime, time, timedelta, timezone

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
# News at least this old is dated by when it happened; under it the article's own time stands (a day's
# difference is within what time zones and "yesterday evening" explain).
MIN_LAG = timedelta(hours=36)
MAX_LAG = timedelta(days=30)
LEDE_WORDS = 200
_DAY = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|" \
         r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
# "began selling its HomeView Friday (Nov. 13)", "announced on Tuesday", "said Thursday": a past-tense verb, then
# within a few words the weekday. Not a relative or subordinate clause ("who resigned Thursday", "after ...").
_PAST = (r"(?:began|started|announced|launched|released|unveiled|introduced|said|reported|filed|raised|closed|"
         r"acquired|bought|sold|signed|agreed|ruled|fined|sued|charged|arrested|approved|rejected|confirmed|"
         r"disclosed|revealed|published|posted|shipped|debuted|listed|priced|resigned|fired|ousted|named|"
         r"appointed|cut|laid off|shut|suspended|banned|blocked|ordered|won|lost|completed|opened|went)")
LEDE_DAY = re.compile(rf"\b{_PAST}\b((?:\s+[\w$%'’.,-]+){{0,6}}?)\s+(?:on\s+|late\s+|early\s+)?{_DAY}\b", re.I)
SUBORDINATE = re.compile(r"\b(who|which|that|after|since|following|when|while|as|because|amid|whose|where|if|"
                         r"before|until|will|would|could|may|might|to|next|this coming)\b", re.I)
VAGUE = re.compile(r"\b(last week|earlier this week|earlier this month|last month|days ago|weeks ago)\b", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“])")


def parse_day(s):
    """A YYYY-MM-DD string as a date, or None."""
    m = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$", s or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc).date()
    except ValueError:
        return None


def _noon(day, published_at):
    """The moment a card shows for news dated only by its day: noon UTC, never after the article itself."""
    return min(datetime.combine(day, time(12), timezone.utc), published_at)


def grounded(day, text):
    """Does `text` itself carry `day`: its weekday by name, its day of the month next to its month, or a vague
    "last week" that a date in that range can stand for? The writer's date is used only when this holds, so an
    invented date never moves a card."""
    low = (text or "").lower()
    if re.search(rf"\b{WEEKDAYS[day.weekday()]}\b", low):
        return True
    month = MONTHS[day.month - 1]
    names = rf"(?:{month}|{month[:3]}\.?|{month[:4]}\.?)"
    if re.search(rf"\b{names}\s+{day.day}(?:st|nd|rd|th)?\b|\b{day.day}(?:st|nd|rd|th)?\s+(?:of\s+)?{names}\b", low):
        return True
    return bool(VAGUE.search(low))


def lede_date(text, published_at):
    """The day the article's first sentence says its news happened, when that is a weekday at least two days
    before publication stated in the main clause ("Apple began selling its HomeView Friday (Nov. 13), ...").
    None when the sentence gives no such day."""
    first = _SENTENCE_END.split((text or "").strip(), 1)[0]
    m = LEDE_DAY.search(first)
    if not m or SUBORDINATE.search(first[:m.start()]) or SUBORDINATE.search(m.group(1) or ""):
        return None
    back = (published_at.weekday() - WEEKDAYS.index(m.group(2).lower())) % 7
    if back < 2:
        return None
    day = (published_at - timedelta(days=back)).date()
    # An explicit date beside the weekday must agree with it ("Friday (Nov. 13)").
    after = first[m.end():m.end() + 16]
    d = re.search(rf"\(?\s*{_MONTH}\.?\s+(\d{{1,2}})\b", after, re.I)
    if d and int(d.group(2)) != day.day:
        return None
    return day


def news_at(claimed, published_at, title, text):
    """When the news of one item happened, or None when the article's own time stands: the writer's `claimed`
    date (YYYY-MM-DD) when the text carries it, else the first sentence's own statement, and only when that is
    MIN_LAG to MAX_LAG before the article."""
    opening = " ".join((text or "").split()[:LEDE_WORDS])
    day = parse_day(claimed)
    if day is not None and not grounded(day, f"{title} {opening}"):
        day = None
    if day is None:
        day = lede_date(opening, published_at)
    if day is None:
        return None
    at = _noon(day, published_at)
    return at if MIN_LAG <= published_at - at <= MAX_LAG else None
