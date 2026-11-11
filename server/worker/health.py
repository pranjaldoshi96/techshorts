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
    }


def write(db, srcs, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(build(db, srcs), f, indent=1)
    os.replace(tmp, path)
