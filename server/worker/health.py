"""health.json and owner alerts (spec section 7).

Written to the state dir every tick and next to edition.json on every publish (the R2 safe-mode Action will read
the published copy). Alerts go to ntfy at most once per condition per 6 h; without NTFY_TOPIC they are logged.
"""
import json
import logging
import os
from datetime import timedelta

import requests

from statistics import median

from . import degraded as degraded_mod, sources as sources_mod
from .db import from_iso, iso, utcnow

log = logging.getLogger("techgist")
ALERT_EVERY = timedelta(hours=6)


def notify(db, condition, message):
    """ntfy alert, at most once per condition per 6 h. Without NTFY_TOPIC it only logs."""
    now = utcnow()
    last = from_iso(db.get_meta(f"alert:{condition}"))
    if last and now - last < ALERT_EVERY:
        return False
    db.set_meta(f"alert:{condition}", iso(now))
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        log.warning("ALERT %s: %s (NTFY_TOPIC unset; not sent)", condition, message)
        return True
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=message.encode(), timeout=10,
                      headers={"Title": f"Techgist: {condition}"})
    except requests.RequestException as e:
        log.warning("ntfy failed for %s: %s", condition, type(e).__name__)
    return True


def source_status(db, srcs):
    out = []
    for s in srcs.sources:
        if not s.polled:
            continue
        st = db.source_state(s.id)
        out.append({
            "id": s.id, "name": s.name, "tier": sources_mod.effective_tier(s, st["check_ok"]),
            "configuredTier": s.tier, "lastStatus": st["last_status"], "lastError": st["last_error"],
            "lastOkAt": st["last_ok_at"], "consecutiveFailures": st["consecutive_failures"],
            "checkOk": None if st["check_ok"] is None else bool(st["check_ok"]), "checkDetail": st["check_detail"],
        })
    return out


def build(db, srcs, publish=None, now=None):
    now = now or utcnow()
    status = source_status(db, srcs)
    counts = db.prefilter_counts(now - timedelta(hours=24))
    polls = db.q("SELECT status, COUNT(*) n FROM polls WHERE at >= ? GROUP BY status", iso(now - timedelta(hours=1)))
    return {
        "generatedAt": iso(now),
        "sources": status,
        "sourceCheck": {"failed": [s["id"] for s in status if s["checkOk"] is False]},
        "polls1h": {str(r["status"]): r["n"] for r in polls},
        "prefilter": {"window": "24h", "total": sum(counts.values()), "byRule": counts},
        "items": {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM items GROUP BY status")},
        "llm": llm_stats(db, now - timedelta(hours=24)),
        "validation": validation_stats(db, now - timedelta(hours=24)),
        "events": event_stats(db, now - timedelta(hours=24)),
        "importance": importance_drift(db, now - timedelta(hours=24)),
        "spice": spice_stats(db, now - timedelta(hours=24), publish),
        "edition": edition_stats(db, now, publish),
        "freshness": freshness(db, now - timedelta(hours=24)),
    }


def _pct(sorted_values, p):
    return sorted_values[min(len(sorted_values) - 1, int(p * len(sorted_values)))] if sorted_values else None


def llm_stats(db, since):
    rows = db.q("SELECT kind, model, outcome, ms, tok_in, tok_out FROM write_log WHERE at >= ? AND kind != 'item'",
                iso(since))
    ok_ms = sorted(r["ms"] for r in rows if r["outcome"] == "ok" and r["ms"] is not None)
    by_model = {}
    for r in rows:
        by_model[r["model"]] = by_model.get(r["model"], 0) + 1
    return {
        "window": "24h", "calls": len(rows), "byKind": _count(rows, "kind"), "byModel": by_model,
        "http429": sum(r["outcome"] == "http_429" for r in rows),
        "errors": sum(r["outcome"] not in ("ok", "http_429") for r in rows),
        "p50ms": _pct(ok_ms, 0.5), "p95ms": _pct(ok_ms, 0.95),
        # As the endpoint reports them (`usage`); calls that failed or reported none count as 0.
        "tokens": {"prompt": sum(r["tok_in"] or 0 for r in rows), "completion": sum(r["tok_out"] or 0 for r in rows)},
    }


# The pass rate is per item, on its first ladder run: one row per URL. Counting every write_log row counted a bad
# item up to three times (its hold retries at +30 min and +2 h), and items that failed together came back together,
# so in the 2026-11 shadow run 23 of 39 hours read under 80% while only 5 were on first attempts. LLM errors
# (timeouts) are the endpoint's, not the text's, and are counted apart.
FIRST_ATTEMPT = "COALESCE(attempt, 1) = 1"


def first_attempt_rate(db, since, until=None):
    """(passed, failed) over items whose first ladder run fell in the window."""
    rows = db.q(f"SELECT outcome FROM write_log WHERE at >= ? AND at <= ? AND kind = 'item' AND {FIRST_ATTEMPT}"
                " AND outcome IN ('ok', 'failed')", iso(since), iso(until) if until else "9999")
    ok = sum(r["outcome"] == "ok" for r in rows)
    return ok, len(rows) - ok


def validation_stats(db, since):
    """Pass rate over items with enough text, on first attempts; thin (no_text) items and LLM errors are counted
    separately. eventualPassRate is the share of those items that passed on any attempt (hold retries)."""
    rows = db.q("SELECT outcome, reason, attempt FROM write_log WHERE at >= ? AND kind = 'item'", iso(since))
    ok, failed = first_attempt_rate(db, since)
    first = db.q(f"SELECT DISTINCT url_canonical FROM write_log WHERE at >= ? AND kind = 'item' AND {FIRST_ATTEMPT}"
                 " AND outcome IN ('ok', 'failed')", iso(since))
    urls = [r["url_canonical"] for r in first]
    eventual = 0
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        eventual += db.one("SELECT COUNT(DISTINCT url_canonical) n FROM write_log WHERE kind = 'item' AND outcome ="
                           f" 'ok' AND url_canonical IN ({','.join('?' * len(chunk))})", *chunk)["n"]
    skips = _count([r for r in rows if r["outcome"] != "ok"], "reason")
    retries = [r for r in rows if (r["attempt"] or 1) > 1 and r["outcome"] in ("ok", "failed")]
    return {"window": "24h", "passed": ok, "failed": failed, "thin": sum(r["outcome"] == "thin" for r in rows),
            "llmErrors": sum(r["outcome"] == "llm_error" for r in rows),
            "passRate": round(ok / (ok + failed), 3) if ok + failed else None,
            "eventualPassRate": round(eventual / len(urls), 3) if urls else None,
            "retries": {"passed": sum(r["outcome"] == "ok" for r in retries),
                        "failed": sum(r["outcome"] == "failed" for r in retries)},
            "skipReasons": skips}


def event_stats(db, since):
    live = db.q("SELECT n_publishers FROM events WHERE status = 'live' AND updated_at >= ?", iso(since))
    return {"window": "24h", "live": len(live), "multiPublisher": sum((r["n_publishers"] or 0) >= 2 for r in live),
            "merged": db.one("SELECT COUNT(*) n FROM events WHERE status = 'merged' AND updated_at >= ?",
                             iso(since))["n"],
            "aliases": db.one("SELECT COUNT(*) n FROM event_alias")["n"],
            "unclustered": db.one("SELECT COUNT(*) n FROM writes w JOIN items i USING (url_canonical) WHERE"
                                  " w.type IS NOT NULL AND i.status != 'dropped' AND w.written_at >= ? AND"
                                  " w.url_canonical NOT IN (SELECT url_canonical FROM event_members)",
                                  iso(since))["n"]}


def importance_drift(db, since):
    """Spec 2.10 drift check: the raw LLM score against the clamped score per event, over 24 h. ntfy alerts when
    more than 10% of stories come back raw >= 8 (the model inflating, or a prompt regression).

    A story is an event (the median raw score of its validated members) or an unclustered item: counted per item,
    one big story with 50 follow-ups swamped the share in the 2026-11 shadow run (89 of 211 items at 8+ were one
    scandal). Only validated writes count; failed and thin rows are classification-only guesses. The per-item
    histogram stays for diagnosis."""
    rows = db.q("SELECT w.importance, em.event_id FROM writes w LEFT JOIN event_members em USING (url_canonical)"
                " WHERE w.written_at >= ? AND w.importance IS NOT NULL AND w.status = 'ok'", iso(since))
    raw = [r["importance"] for r in rows]
    stories = {}
    for r in rows:
        stories.setdefault(r["event_id"] or id(r), []).append(r["importance"])
    per_story = [median(v) for v in stories.values()]
    ev = db.q("SELECT importance FROM events WHERE status = 'live' AND updated_at >= ? AND importance IS NOT NULL",
              iso(since))
    hist = lambda vals: {str(i): sum(v == i for v in vals) for i in range(1, 11)}  # noqa: E731
    share = round(sum(v >= 8 for v in per_story) / len(per_story), 3) if per_story else None
    if share is not None and len(per_story) >= 20 and share > 0.10:
        notify(db, "importance_drift", f"{share:.0%} of stories scored raw importance >= 8 in 24 h (limit 10%)")
    return {"window": "24h", "rawItems": hist(raw), "clampedEvents": hist([r["importance"] for r in ev]),
            "stories": len(per_story), "rawShareAtLeast8": share,
            "itemShareAtLeast8": round(sum(v >= 8 for v in raw) / len(raw), 3) if raw else None}


def spice_stats(db, since, publish=None):
    """Spice (how tap-worthy a story is, the writer's 1-10 score) over 24 h: per written item, per live event, and
    for the last edition's cards with the push-alert candidates in rank order (spice x importance x traction)."""
    items = [r["spice"] for r in db.q("SELECT spice FROM writes WHERE written_at >= ? AND status = 'ok'"
                                      " AND spice IS NOT NULL", iso(since))]
    events = [r["spice"] for r in db.q("SELECT spice FROM events WHERE status = 'live' AND updated_at >= ?"
                                       " AND spice IS NOT NULL", iso(since))]
    hist = lambda vals: {str(i): sum(v == i for v in vals) for i in range(1, 11)}  # noqa: E731
    out = {"window": "24h", "items": hist(items), "events": hist(events),
           "itemMedian": median(items) if items else None,
           "itemShareAtLeast7": round(sum(v >= 7 for v in items) / len(items), 3) if items else None,
           "itemShareAtMost2": round(sum(v <= 2 for v in items) / len(items), 3) if items else None}
    if publish:
        out["edition"] = publish.get("spice")
        out["notifications"] = (publish.get("notifications") or [])[:10]
    return out


def edition_stats(db, now, publish):
    """The last publish attempt's report (tabs, degraded cards, held events) plus the edition's age."""
    last_ok = db.one("SELECT * FROM publishes WHERE ok = 1 ORDER BY at DESC LIMIT 1")
    rejected = db.one("SELECT COUNT(*) n FROM publishes WHERE ok = 0 AND at >= ?", iso(now - timedelta(hours=24)))["n"]
    out = {"lastPublishAt": last_ok["at"] if last_ok else None,
           "editionAgeMin": round((now - from_iso(last_ok["at"])).total_seconds() / 60, 1) if last_ok else None,
           "degraded": last_ok["degraded"] if last_ok else None,
           "articles": last_ok["articles"] if last_ok else 0, "top": last_ok["top"] if last_ok else 0,
           "rejected24h": rejected}
    if publish:
        out["last"] = publish
    return out


LIVE_MIN = 20


def freshness(db, since):
    """Publisher-to-app lag (event_published_at -> first edition with the event) and pipeline lag (first seen ->
    first edition), over events first published in the window. A post that was already old when the worker first
    saw it (a Reads item after downtime) counts in the first only for what it is: old."""
    # A twin (the same story as an event that already shipped) isn't a first ship; it is merged away now (cluster.
    # merge_twins), but older rows still count it 1-2 days late.
    rows = db.q("SELECT s.published_at, s.first_at, s.first_seen_at, i.source_id FROM shipped s"
                " LEFT JOIN events e ON e.id = s.event_id LEFT JOIN items i ON i.url_canonical = e.lead_url"
                " WHERE s.first_at >= ? AND s.event_id NOT IN (SELECT twin_id FROM twins)", iso(since))
    pub = sorted((from_iso(r["first_at"]) - from_iso(r["published_at"])).total_seconds() / 60 for r in rows)
    seen = sorted((from_iso(r["first_at"]) - from_iso(r["first_seen_at"])).total_seconds() / 60
                  for r in rows if r["first_seen_at"])
    rnd = lambda v: None if v is None else round(v, 1)  # noqa: E731
    # Stories published while the worker was polling: the only rows that measure how fast a new story reaches the
    # edition. A seed or a catch-up after downtime ships hours-old posts, which made the overall p50 read 1,431 min
    # on the VPS test run's first day. "While polling" is judged by the worker's own poll log (a poll in the
    # LIVE_MIN minutes before the publisher's time), not by how soon the story was seen: the old rule (first seen
    # within 20 min of publication) threw out exactly the slow samples, a feed that lists a story 45 min late.
    live_rows = [r for r in rows if r["first_seen_at"] and was_polling(db, from_iso(r["published_at"]))]
    # Never negative: an event's clock is its first full report, and a paywalled outlet's thin item of the same
    # story may have been seen before that report was published.
    lag = lambda r, a, b: max(0.0, (from_iso(r[a]) - from_iso(r[b])).total_seconds() / 60)  # noqa: E731
    live = sorted(lag(r, "first_at", "published_at") for r in live_rows)
    feed = sorted(lag(r, "first_seen_at", "published_at") for r in live_rows)
    by_source = {}
    for r in live_rows:
        by_source.setdefault(r["source_id"] or "?", []).append(r)
    sources = {sid: {"events": len(v),
                     # publisher -> first seen is the feed's own delay plus the poll interval; first seen -> app
                     # is the worker's (text, write, fact-check, cluster, publish).
                     "feedLagMin": rnd(_pct(sorted(lag(r, "first_seen_at", "published_at") for r in v), 0.5)),
                     "publisherToAppMin": rnd(_pct(sorted(lag(r, "first_at", "published_at") for r in v), 0.5))}
               for sid, v in sorted(by_source.items(), key=lambda kv: -len(kv[1]))}
    return {"window": "24h", "events": len(rows),
            "publisherToAppMin": {"p50": rnd(_pct(pub, 0.5)), "p95": rnd(_pct(pub, 0.95))},
            "firstSeenToAppMin": {"p50": rnd(_pct(seen, 0.5)), "p95": rnd(_pct(seen, 0.95))},
            "live": {"events": len(live), "publisherToAppMin": {"p50": rnd(_pct(live, 0.5)),
                                                                "p95": rnd(_pct(live, 0.95))},
                     "feedLagMin": {"p50": rnd(_pct(feed, 0.5)), "p95": rnd(_pct(feed, 0.95))},
                     "bySource": sources}}


def was_polling(db, at):
    """Was the worker polling feeds in the LIVE_MIN minutes up to `at`?"""
    return db.one("SELECT 1 FROM polls WHERE at <= ? AND at >= ? LIMIT 1", iso(at),
                  iso(at - timedelta(minutes=LIVE_MIN))) is not None


def alerts(db, now, started_at=None, top_min=12):
    """Spec section 7 conditions that the tick can see (the per-source and drift alerts fire where they're
    measured). Returns the names that are currently true."""
    on = []
    last_ok = db.one("SELECT MAX(at) at FROM publishes WHERE ok = 1")["at"]
    ref = from_iso(last_ok) if last_ok else started_at
    if ref is not None and now - ref > timedelta(minutes=20):
        on.append(("no_publish", f"no edition published for {int((now - ref).total_seconds() // 60)} min"))
    last = db.one("SELECT top, ok FROM publishes ORDER BY at DESC LIMIT 1")
    if last is not None and last["ok"] and (last["top"] or 0) < top_min:
        on.append(("top_low", f"Top has {last['top']} stories (minimum {top_min})"))
    ok, failed = first_attempt_rate(db, now - timedelta(hours=1), now)
    if ok + failed >= 10 and ok / (ok + failed) < 0.8:
        on.append(("pass_rate", f"validation pass rate {ok / (ok + failed):.0%} over 1 h on {ok + failed} new items"
                                f" (minimum 80%)"))
    calls = db.q("SELECT outcome FROM write_log WHERE at >= ? AND kind != 'item'", iso(now - timedelta(hours=1)))
    n429 = sum(r["outcome"] == "http_429" for r in calls)
    if n429 > 20:
        on.append(("rate_limited", f"{n429} LLM 429s in 1 h"))
    slow = sum(degraded_mod.endpoint_failure(r["outcome"]) for r in calls)
    if slow >= 10 and slow > sum(r["outcome"] == "ok" for r in calls) / 4:
        on.append(("llm_timeouts", f"{slow} LLM timeouts or 5xx in 1 h"))
    key_failed = db.get_meta("key_auth_failed_at")
    if any(r["outcome"] in ("http_401", "http_403") for r in calls) or (
            key_failed and key_failed >= iso(now - timedelta(hours=1))):
        on.append(("llm_auth", "LLM API returned 401/403 in the last hour"))
    for name, msg in on:
        notify(db, name, msg)
    return [name for name, _ in on]


def _count(rows, key):
    out = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return out


def write(db, srcs, path, publish=None, now=None, extra=None):
    data = build(db, srcs, publish, now)
    data.update(extra or {})
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
    return data
