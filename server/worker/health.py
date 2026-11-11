"""health.json and owner alerts (spec section 7).

R1 WP1 fills the source and prefilter sections; later work packages add freshness, LLM and content metrics.
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


def build(db, srcs):
    now = utcnow()
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


def _count(rows, key):
    out = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return out


def write(db, srcs, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(build(db, srcs), f, indent=1)
    os.replace(tmp, path)
