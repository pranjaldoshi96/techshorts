#!/usr/bin/env python3
"""Gives tier-1 items that were held out (dropped after their retries) one more write attempt.

For after a writer fix (the hedge-keeping select(), the repeat rules): the stories those rules would now save stay
dropped otherwise. Each item goes back to `held` with retry_at now and its attempt count kept, so the worker's next
LLM stage tries it once more, continuing from its stored answer and feedback, and a further failure drops it again.

Run from server/ against a test copy's state only:
  TECHGIST_STATE=~/.techshorts-r1 python3 -m tools.requeue --hours 12 [--all-tiers] [--dry-run]
"""
import argparse
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import config, sources as sources_mod  # noqa: E402
from worker.db import DB, iso, utcnow  # noqa: E402


def requeue(db, srcs, now, hours, all_tiers=False):
    """[(url_canonical, source_id, drop_rule, title)] re-queued."""
    tier1 = {s.id for s in srcs.sources if s.tier == 1}
    rows = db.q("SELECT url_canonical, source_id, drop_rule, title FROM items WHERE status = 'dropped'"
                " AND drop_rule LIKE 'held_out:%' AND published_at >= ?", iso(now - timedelta(hours=hours)))
    picked = [r for r in rows if all_tiers or r["source_id"] in tier1]
    for r in picked:
        db.conn.execute("UPDATE items SET status = 'held', retry_at = ?, drop_rule = NULL WHERE url_canonical = ?",
                        (iso(now), r["url_canonical"]))
    db.commit()
    return [(r["url_canonical"], r["source_id"], r["drop_rule"], r["title"]) for r in picked]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12)
    ap.add_argument("--all-tiers", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = config.load()
    if os.path.realpath(os.path.expanduser(cfg["state_dir"])) == os.path.realpath(os.path.expanduser("~/.techshorts")):
        sys.exit("refusing to touch the live state; set TECHGIST_STATE to a test copy")
    db = DB(config.state_path(cfg, "state.db"))
    db.conn.execute("PRAGMA busy_timeout = 120000")
    srcs = sources_mod.load()
    if args.dry_run:
        db.commit = lambda: db.conn.rollback()
    for url, sid, rule, title in requeue(db, srcs, utcnow(), args.hours, args.all_tiers):
        print(f"{sid:12} {rule:28} {(title or '')[:70]}")


if __name__ == "__main__":
    main()
