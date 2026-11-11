"""Degraded mode (spec 2.14): when the LLM is out, the feed falls back to publisher titles and excerpts, never blank.

Edition-wide outage, from the write_log the LLM stage already keeps:
  - auth:      the latest chat call got 401/403 (key revoked or expired), until a call succeeds again;
  - 429 storm: >= 10 rate-limit replies in 15 min and more of them than successful calls;
  - pass rate: under 50% of item outcomes passing over the last 30 min (at least 6 outcomes);
  - no_key:    the worker has no key or model at all.
Per event, outside an outage: an event whose lead's text failed validation still publishes in degraded form when
it has >= 2 publishers or a tier-1 lead (spec 2.8 "Degraded publish"), so an important story is never hidden only
because the AI text failed.
"""
import re
from datetime import timedelta

from .db import iso
from . import text as text_mod

EXCERPT_WORDS = 55
STORM_WINDOW, STORM_MIN = timedelta(minutes=15), 10
PASS_WINDOW, PASS_MIN_OUTCOMES, PASS_FLOOR = timedelta(minutes=30), 6, 0.5
PROBE_EVERY = timedelta(minutes=10)


def llm_outage(db, now, have_client=True):
    """The outage reason, or None when the LLM is working."""
    if not have_client:
        return "no_key"
    last = db.one("SELECT outcome FROM write_log WHERE kind NOT IN ('item') AND outcome IS NOT NULL"
                  " AND outcome NOT IN ('http_429') ORDER BY at DESC, rowid DESC LIMIT 1")
    if last is not None and last["outcome"] in ("http_401", "http_403"):
        return "auth"
    calls = db.q("SELECT outcome FROM write_log WHERE at >= ? AND kind != 'item'", iso(now - STORM_WINDOW))
    n429 = sum(r["outcome"] == "http_429" for r in calls)
    if n429 >= STORM_MIN and n429 > sum(r["outcome"] == "ok" for r in calls):
        return "rate_limited"
    items = db.q("SELECT outcome FROM write_log WHERE at >= ? AND kind = 'item' AND outcome IN ('ok', 'failed')",
                 iso(now - PASS_WINDOW))
    if len(items) >= PASS_MIN_OUTCOMES and sum(r["outcome"] == "ok" for r in items) / len(items) < PASS_FLOOR:
        return "pass_rate"
    return None


def needs_probe(db, now):
    """In an auth outage with nothing to write, nothing would ever notice the key working again; one tiny call
    every 10 min does."""
    row = db.one("SELECT MAX(at) at FROM write_log WHERE kind != 'item'")
    return row["at"] is None or row["at"] < iso(now - PROBE_EVERY)


_SENT_END = re.compile(r"(?:(?<=[.!?…])|(?<=[.!?…][\"”’)]))\s+")


def excerpt(text, limit=EXCERPT_WORDS):
    """The publisher's text trimmed to <= 55 words at a sentence boundary. A first sentence that is already too
    long is cut at the limit with an ellipsis rather than shown whole."""
    text = text_mod.clean_feed(text or "") if text else ""
    text = " ".join(text.split())
    if not text:
        return ""
    out, n = [], 0
    for s in _SENT_END.split(text):
        w = len(s.split())
        if not w:
            continue
        if n + w > limit:
            break
        out.append(s)
        n += w
    if not out:
        return " ".join(text.split()[:limit]).rstrip(",;:") + "…"
    return " ".join(out)
