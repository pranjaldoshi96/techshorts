"""health.json and owner alerts (spec section 7).

Written to the state dir every tick and next to edition.json on every publish (the R2 safe-mode Action will read
the published copy). Alerts go to ntfy at most once per condition per 6 h; without NTFY_TOPIC they are logged.
"""
import json
import logging
import os
from datetime import timedelta

import requests

from . import sources as sources_mod
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
        "edition": edition_stats(db, now, publish),
        "freshness": freshness(db, now - timedelta(hours=24)),
    }


def _pct(sorted_values, p):
    return sorted_values[min(len(sorted_values) - 1, int(p * len(sorted_values)))] if sorted_values else None


def llm_stats(db, since):
    rows = db.q("SELECT kind, model, outcome, ms FROM write_log WHERE at >= ? AND kind != 'item'", iso(since))
    ok_ms = sorted(r["ms"] for r in rows if r["outcome"] == "ok" and r["ms"] is not None)
    by_model = {}
    for r in rows:
        by_model[r["model"]] = by_model.get(r["model"], 0) + 1
    return {
        "window": "24h", "calls": len(rows), "byKind": _count(rows, "kind"), "byModel": by_model,
        "http429": sum(r["outcome"] == "http_429" for r in rows),
        "errors": sum(r["outcome"] not in ("ok", "http_429") for r in rows),
        "p50ms": _pct(ok_ms, 0.5), "p95ms": _pct(ok_ms, 0.95),
    }


def validation_stats(db, since):
    """Pass rate over items with enough text; thin (no_text) items are counted separately as skips."""
    rows = db.q("SELECT outcome, reason FROM write_log WHERE at >= ? AND kind = 'item'", iso(since))
    ok = sum(r["outcome"] == "ok" for r in rows)
    failed = sum(r["outcome"] == "failed" for r in rows)
    skips = _count([r for r in rows if r["outcome"] != "ok"], "reason")
    return {"window": "24h", "passed": ok, "failed": failed, "thin": sum(r["outcome"] == "thin" for r in rows),
            "passRate": round(ok / (ok + failed), 3) if ok + failed else None, "skipReasons": skips}


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
    """Spec 2.10 drift check: the raw LLM score per item against the clamped score per event, over 24 h. ntfy
    alerts when more than 10% of items come back raw >= 8 (the model inflating, or a prompt regression)."""
    raw = [r["importance"] for r in db.q("SELECT importance FROM writes WHERE written_at >= ? AND importance IS NOT"
                                         " NULL", iso(since))]
    ev = db.q("SELECT importance FROM events WHERE status = 'live' AND updated_at >= ? AND importance IS NOT NULL",
              iso(since))
    hist = lambda vals: {str(i): sum(v == i for v in vals) for i in range(1, 11)}  # noqa: E731
    share = round(sum(v >= 8 for v in raw) / len(raw), 3) if raw else None
    if share is not None and len(raw) >= 20 and share > 0.10:
        notify(db, "importance_drift", f"{share:.0%} of items scored raw importance >= 8 in 24 h (limit 10%)")
    return {"window": "24h", "rawItems": hist(raw), "clampedEvents": hist([r["importance"] for r in ev]),
            "rawShareAtLeast8": share}


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


def freshness(db, since):
    """Publisher-to-app lag (event_published_at -> first edition with the event) and pipeline lag (first seen ->
    first edition), over events first published in the window. A post that was already old when the worker first
    saw it (a Reads item after downtime) counts in the first only for what it is: old."""
    rows = db.q("SELECT published_at, first_at, first_seen_at FROM shipped WHERE first_at >= ?", iso(since))
    pub = sorted((from_iso(r["first_at"]) - from_iso(r["published_at"])).total_seconds() / 60 for r in rows)
    seen = sorted((from_iso(r["first_at"]) - from_iso(r["first_seen_at"])).total_seconds() / 60
                  for r in rows if r["first_seen_at"])
    rnd = lambda v: None if v is None else round(v, 1)  # noqa: E731
    return {"window": "24h", "events": len(rows),
            "publisherToAppMin": {"p50": rnd(_pct(pub, 0.5)), "p95": rnd(_pct(pub, 0.95))},
            "firstSeenToAppMin": {"p50": rnd(_pct(seen, 0.5)), "p95": rnd(_pct(seen, 0.95))}}


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
    items = db.q("SELECT outcome FROM write_log WHERE at >= ? AND kind = 'item' AND outcome IN ('ok', 'failed')",
                 iso(now - timedelta(hours=1)))
    if len(items) >= 10:
        rate = sum(r["outcome"] == "ok" for r in items) / len(items)
        if rate < 0.8:
            on.append(("pass_rate", f"validation pass rate {rate:.0%} over 1 h (minimum 80%)"))
    calls = db.q("SELECT outcome FROM write_log WHERE at >= ? AND kind != 'item'", iso(now - timedelta(hours=1)))
    n429 = sum(r["outcome"] == "http_429" for r in calls)
    if n429 > 20:
        on.append(("rate_limited", f"{n429} LLM 429s in 1 h"))
    if any(r["outcome"] in ("http_401", "http_403") for r in calls):
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
