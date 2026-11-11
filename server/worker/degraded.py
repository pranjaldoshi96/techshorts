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

import requests

from .db import from_iso, iso
from . import text as text_mod

EXCERPT_WORDS = 55
STORM_WINDOW, STORM_MIN = timedelta(minutes=15), 10
PASS_WINDOW, PASS_MIN_OUTCOMES, PASS_FLOOR = timedelta(minutes=30), 6, 0.5
PROBE_EVERY = timedelta(minutes=10)


def key_check(db, cfg, key, now, session=None):
    """One 1-input embedding call with a 5 s timeout, every tick: the cheapest way to learn that the key was
    revoked even when nothing needs writing (embeddings share the key but not the chat budget). Only a 401/403 or
    a 200 is recorded; a timeout or 5xx says nothing about the key."""
    model = (cfg.get("embed") or {}).get("model")
    if not key or not model:
        return None
    try:
        r = (session or requests).post(cfg["llm"]["base_url"].rstrip("/") + "/embeddings", timeout=5,
                                       headers={"Authorization": f"Bearer {key}"},
                                       json={"model": model, "input": ["ping"], "input_type": "query"})
    except requests.RequestException:
        return None
    if r.status_code in (401, 403):
        db.set_meta("key_auth_failed_at", iso(now))
        return False
    if r.status_code == 200:
        db.set_meta("key_ok_at", iso(now))
        return True
    return None


def llm_outage(db, now, have_client=True):
    """The outage reason, or None when the LLM is working."""
    if not have_client:
        return "no_key"
    # Auth: whichever came last of the latest chat outcome and the per-tick key check.
    signals = []
    last = db.one("SELECT at, outcome FROM write_log WHERE kind NOT IN ('item') AND outcome IS NOT NULL"
                  " AND outcome IN ('ok', 'http_401', 'http_403') ORDER BY at DESC, rowid DESC LIMIT 1")
    if last is not None:
        signals.append((last["at"], last["outcome"] != "ok"))
    for key, failed in (("key_auth_failed_at", True), ("key_ok_at", False)):
        at = db.get_meta(key)
        if at:
            signals.append((at, failed))
    # On a tie (same second) the failure wins: a call that failed is proof, an older success isn't.
    if signals and max(signals, key=lambda x: (x[0], x[1]))[1]:
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


def outage_since(db, reason, now):
    """When the LLM stopped seeing new items, or None for "never saw any" (no_key). Only items first seen after
    this publish as outage singletons: anything older the LLM already judged, and that judgement stands.
    For auth it is the last success (chat ok or key check ok); with none on record, the first auth failure.
    The 429 storm and pass-rate outages are judged over a window, so their start is that window's start."""
    if reason == "rate_limited":
        return now - STORM_WINDOW
    if reason == "pass_rate":
        return now - PASS_WINDOW
    if reason != "auth":
        return None
    ok = db.one("SELECT MAX(at) at FROM write_log WHERE kind != 'item' AND outcome = 'ok'")["at"]
    ok = max(filter(None, (ok, db.get_meta("key_ok_at"))), default=None)
    if ok:
        return from_iso(ok)
    first = db.one("SELECT MIN(at) at FROM write_log WHERE kind != 'item' AND outcome IN ('http_401', 'http_403')")
    first = first["at"] or db.get_meta("key_auth_failed_at")
    return from_iso(first) if first else None


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
