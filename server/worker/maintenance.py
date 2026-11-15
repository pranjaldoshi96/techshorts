"""Keeps state.db bounded. Run once a day from the worker's tick (main.maintain).

What grows without it: one `polls` row per source per poll (about 50,000 a day), the write and prefilter logs, a
row per publish, and for every item its feed text, article text, the LLM's raw answers and a 4 KB embedding.

What is kept, and why it is safe to drop the rest:
  polls, prefilter_log      7 days    health.json reads the last 24 h; reports look back a few days
  write_log, publishes      14 days   24 h windows and the 15 min outage window; the newest good publish is kept
  embeddings                30 days   clustering joins within 48 h, the backlog embeds 14 days, tabs show 14 days
  items.feed_text,          30 days   emptied, the row stays: the URL must stay known or an old post that a feed
  writes.text/output/steps            still lists would come back as new
  alert_queue (finished)    45 days   the push dedupe record outlives event aliases (30 days)
Events, sightings and the slim item rows are kept: they are small and they are what "already seen" means.

Each statement touches at most BATCH rows, so one pass never holds the write lock for long; a table with more to
do is finished on the following ticks. Freed pages are reused by SQLite, so the file stops growing rather than
shrinking; the WAL is truncated at the end.
"""
import logging
from datetime import timedelta

from . import alertq
from .db import from_iso, iso

log = logging.getLogger("techgist")

DEFAULTS = {"logs_days": 7, "write_log_days": 14, "publishes_days": 14, "heavy_days": 30, "every_hours": 24}
BATCH = 5000
META_KEY = "pruned_at"


def settings(cfg):
    return {**DEFAULTS, **((cfg or {}).get("maintenance") or {})}


def _delete(db, table, where, *args):
    cur = db.conn.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT {BATCH})",
                          args)
    return cur.rowcount


def prune(db, cfg, now):
    """One bounded pass. Returns ({what: rows}, more) where `more` says a table hit BATCH and has rows left."""
    s = settings(cfg)
    done = {}
    logs = iso(now - timedelta(days=s["logs_days"]))
    heavy = iso(now - timedelta(days=s["heavy_days"]))
    done["polls"] = _delete(db, "polls", "at < ?", logs)
    done["prefilter_log"] = _delete(db, "prefilter_log", "at < ?", logs)
    done["write_log"] = _delete(db, "write_log", "at < ?", iso(now - timedelta(days=s["write_log_days"])))
    # The newest good publish is what "when did we last publish" reads; it stays however old it is.
    done["publishes"] = _delete(
        db, "publishes", "at < ? AND NOT (ok = 1 AND at = (SELECT MAX(at) FROM publishes WHERE ok = 1))",
        iso(now - timedelta(days=s["publishes_days"])))
    done["embeddings"] = _delete(
        db, "embeddings", "at < ? AND url_canonical NOT IN (SELECT url_canonical FROM items WHERE published_at >= ?)",
        heavy, heavy)
    cur = db.conn.execute(
        "UPDATE items SET feed_text = NULL WHERE rowid IN (SELECT rowid FROM items WHERE first_seen_at < ?"
        f" AND published_at < ? AND feed_text IS NOT NULL LIMIT {BATCH})", (heavy, heavy))
    done["items_text"] = cur.rowcount
    cur = db.conn.execute(
        "UPDATE writes SET text = NULL, output = NULL, steps = NULL WHERE rowid IN (SELECT rowid FROM writes"
        f" WHERE written_at < ? AND (text IS NOT NULL OR output IS NOT NULL OR steps IS NOT NULL) LIMIT {BATCH})",
        (heavy,))
    done["writes_text"] = cur.rowcount
    alertq.ensure(db)
    done["alert_queue"] = alertq.prune(db, now)
    done = {k: v for k, v in done.items() if v}
    return done, any(v >= BATCH for v in done.values())


def due(db, cfg, now):
    last = from_iso(db.get_meta(META_KEY))
    return last is None or now - last >= timedelta(hours=settings(cfg)["every_hours"])


def run(db, cfg, now):
    """The daily pass, when it is due. Commits. Returns what was pruned ({} when not due or nothing to do)."""
    if not due(db, cfg, now):
        return {}
    done, more = prune(db, cfg, now)
    if not more:
        # Stamped only when the pass finished, so a large backlog continues on the next tick.
        db.set_meta(META_KEY, iso(now))
    db.commit()
    try:
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:  # a reader holding the WAL is not an error worth a failed tick
        log.info("wal checkpoint skipped: %s", e)
    if done:
        log.info("pruned: %s%s", done, " (more next tick)" if more else "")
    return done
