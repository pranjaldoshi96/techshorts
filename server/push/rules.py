"""Which story, if any, one device gets now. Pure: no clock, no database, no network.

The rules (limits in config.py `rules`; spec 6.2 where it has a number):
  - only stories with spice >= min_spice, at most max_age_h old by their news date;
  - never a story the device already got;
  - never a story that was already out when the device registered: the reader was looking at it in the app when
    they allowed alerts (and a reinstalled app, which has a new token and no history, is not sent old news);
  - nothing in the device's quiet hours; the best story seen during them is held (one per device, replaced when a
    better one comes) and sent first when they end, even if it is older than max_age_h by then;
  - at least min_gap_min between two alerts;
  - at most daily_cap a local day; a story of importance >= override_importance may go over that (up to
    daily_cap_hard), but never into quiet hours and never inside the gap.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_OFFSET = re.compile(r"^(?:GMT|UTC)([+-])(\d{1,2}):?(\d{2})?$")


@dataclass
class Story:
    event_id: str
    score: float
    spice: int
    importance: int
    news_at: datetime           # the story's own date, UTC
    known_at: datetime = None   # when it was queued for an alert, about when the edition first carried it


@dataclass
class History:
    got: set = field(default_factory=set)      # event ids this device was sent (or that failed for good)
    sent_local_dates: list = field(default_factory=list)   # the local date of each alert sent, as "YYYY-MM-DD"
    last_sent_at: datetime = None
    held: str = None                            # event id held for the end of quiet hours
    registered_at: datetime = None              # when the device first registered this token


@dataclass
class Decision:
    action: str                 # "send", "hold" or "none"
    event_id: str = None
    reason: str = ""


def zone(tz):
    """The device's time zone. iOS gives an IANA name, or "GMT+0530" for a fixed offset; anything else is UTC."""
    try:
        return ZoneInfo(tz)
    except Exception:
        m = _OFFSET.match(tz or "")
        if m:
            d = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
            if d <= timedelta(hours=14):
                return timezone(d if m.group(1) == "+" else -d)
        return timezone.utc


def local(now, tz):
    return now.astimezone(zone(tz))


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet(local_dt, rules):
    start, end = _minutes(rules["quiet_start"]), _minutes(rules["quiet_end"])
    t = local_dt.hour * 60 + local_dt.minute
    if start == end:
        return False
    return start <= t < end if start < end else (t >= start or t < end)


def _key(s):
    return (s.score, s.event_id)


def _out_before(s, hist):
    """The story was already out when this device registered."""
    return hist.registered_at is not None and (s.known_at or s.news_at) <= hist.registered_at


def decide(now, tz, stories, hist, rules):
    """`stories` are the ones the service holds that are released for sending (not killed, past the owner hold)."""
    loc = local(now, tz)
    stories = [s for s in stories if s.event_id not in hist.got and not _out_before(s, hist)]
    fresh = [s for s in stories if s.spice >= rules["min_spice"]
             and timedelta(minutes=-5) <= now - s.news_at <= timedelta(hours=rules["max_age_h"])]
    held = next((s for s in stories if s.event_id == hist.held
                 and now - s.news_at <= timedelta(hours=rules["held_max_age_h"])), None)
    best = max(fresh, key=_key, default=None)
    if in_quiet(loc, rules):
        if best is not None and (held is None or _key(best) > _key(held)):
            return Decision("hold", best.event_id, "quiet_hours")
        return Decision("none", reason="quiet_hours")
    pool = fresh + ([held] if held is not None and held not in fresh else [])
    if not pool:
        return Decision("none", reason="nothing_fresh")
    if hist.last_sent_at is not None and now - hist.last_sent_at < timedelta(minutes=rules["min_gap_min"]):
        return Decision("none", reason="gap")
    today = sum(1 for d in hist.sent_local_dates if d == loc.strftime("%Y-%m-%d"))
    if today >= rules["daily_cap"]:
        hard = rules.get("daily_cap_hard") or 0
        pool = [s for s in pool if s.importance >= rules["override_importance"]]
        if not pool or (hard and today >= hard):
            return Decision("none", reason="daily_cap")
    pick = held if held is not None and held in pool else max(pool, key=_key)
    return Decision("send", pick.event_id, "held_released" if pick is held else "fresh")
