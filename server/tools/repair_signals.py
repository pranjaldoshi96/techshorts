#!/usr/bin/env python3
"""Applies two signal fixes of 2026-11-16 to the items the state already holds.

1. Techmeme and Mediagazer picks dropped by the two-stories-in-one-title rule (`title_digest`) go back to being
   signals, so the next tick imports the linked article: Techmeme's own headlines join two facts of one story
   with ";" (the Anthropic IPO pick was dropped, and nothing else carried the story).
2. A Techmeme row that recorded the outlet's front page as its article ("http://www.scmp.com/") is offered to
   promote_signals again (the next poll of the feed gives it the headline's real link), and the item imported
   from the front page is dropped as `not_article` and taken out of its event. An event left with no member is
   closed (status `void`), so no card stays behind that opens a home page.

Run with the worker stopped (see deploy/README.md for the environment a tool needs):
  TECHGIST_STATE=<state dir> python3 -m tools.repair_signals [--hours 36] [--dry-run]
"""
import argparse
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import config, prefilter, sources as sources_mod  # noqa: E402
from worker.db import DB, iso, utcnow  # noqa: E402


def repair(db, srcs, now, hours=36):
    """[(what, source id or event id, title)] for every row changed."""
    since = iso(now - timedelta(hours=hours))
    picks = [s.id for s in srcs.sources if s.is_signal and s.kind == "techmeme"]
    marks = ",".join("?" * len(picks))
    done = []
    for r in db.q(f"SELECT * FROM items WHERE source_id IN ({marks}) AND status = 'dropped'"
                  " AND drop_rule = 'title_digest' AND published_at >= ?", *picks, since):
        if prefilter.check(r["title"], r["url"], digest=False):
            continue
        db.conn.execute("UPDATE items SET status = 'signal', drop_rule = NULL WHERE url_canonical = ?",
                        (r["url_canonical"],))
        done.append(("signal_again", r["source_id"], r["title"]))
    for r in db.q(f"SELECT * FROM items WHERE source_id IN ({marks}) AND status = 'signal' AND published_at >= ?",
                  *picks, since):
        extra = json.loads(r["extra"] or "{}")
        if extra.get("linked_url") and prefilter.no_path(extra["linked_url"]) \
                and extra.get("promoted") not in (None, "not_article") and extra.pop("promoted"):
            db.conn.execute("UPDATE items SET extra = ? WHERE url_canonical = ?",
                            (json.dumps(extra), r["url_canonical"]))
            done.append(("offered_again", r["source_id"], r["title"]))
    for r in db.q("SELECT * FROM items WHERE status NOT IN ('signal', 'dropped', 'seen', 'stale')"
                  " AND extra LIKE '%\"via\"%' AND published_at >= ?", since):
        if not prefilter.no_path(r["url"]):
            continue
        db.conn.execute("UPDATE items SET status = 'dropped', drop_rule = 'not_article' WHERE url_canonical = ?",
                        (r["url_canonical"],))
        done.append(("front_page_dropped", r["source_id"], r["title"]))
        for m in db.q("SELECT event_id FROM event_members WHERE url_canonical = ?", r["url_canonical"]):
            db.conn.execute("DELETE FROM event_members WHERE url_canonical = ? AND event_id = ?",
                            (r["url_canonical"], m["event_id"]))
            left = db.one("SELECT COUNT(*) n FROM event_members WHERE event_id = ?", m["event_id"])["n"]
            if not left:
                db.conn.execute("UPDATE events SET status = 'void', updated_at = ? WHERE id = ?",
                                (iso(now), m["event_id"]))
            done.append(("event_closed" if not left else "event_keeps_other_members", m["event_id"], r["title"]))
    db.commit()
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=36)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = config.load()
    db = DB(config.state_path(cfg, "state.db"))
    db.conn.execute("PRAGMA busy_timeout = 120000")
    if args.dry_run:
        db.commit = lambda: db.conn.rollback()
    for what, where, title in repair(db, sources_mod.load(), utcnow(), args.hours):
        print(f"{what:26} {where:14} {(title or '')[:90]}")


if __name__ == "__main__":
    main()
