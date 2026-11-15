#!/usr/bin/env python3
"""Applies a source's `seed_days` to a state that was seeded before the source had one.

A source's first poll marks items older than its horizon as `seen`. When a source later gets a longer horizon
(seed_days in sources.yaml), its older posts stay `seen` and its tab stays thin for a week. This puts the `seen`
items inside the new horizon back to `new`, so the worker writes them on its next ticks (newest news first, as
always). Items the prefilter would drop are dropped. Stop the worker first.

Usage (from server/): TECHGIST_STATE=... python3 -m tools.reseed [--dry-run]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import config, fetch, prefilter, sources as sources_mod  # noqa: E402
from worker.db import DB, iso, utcnow  # noqa: E402


def reseed(db, srcs, cfg, now, dry_run=False):
    """{source id: items put back}."""
    out = {}
    for s in srcs.sources:
        if not s.seed_days or not s.polled or s.is_signal:
            continue
        since = iso(now - fetch.seed_horizon(s, cfg))
        rows = db.q("SELECT url_canonical, url, title FROM items WHERE source_id = ? AND status = 'seen'"
                    " AND feed_published_at IS NOT NULL AND published_at >= ?", s.id, since)
        for r in rows:
            rule = "domain_blocklist" if srcs.blocked(r["url"]) else prefilter.check(r["title"], r["url"])
            if dry_run:
                continue
            if rule:
                db.conn.execute("UPDATE items SET status = 'dropped', drop_rule = ? WHERE url_canonical = ?",
                                (rule, r["url_canonical"]))
                db.log_drop(now, s.id, r["url"], r["title"], rule)
            else:
                db.conn.execute("UPDATE items SET status = 'new' WHERE url_canonical = ?", (r["url_canonical"],))
        if rows:
            out[s.id] = len(rows)
    if not dry_run:
        db.commit()
    return out


def main():
    cfg = config.load()
    db = DB(config.state_path(cfg, "state.db"))
    got = reseed(db, sources_mod.load(), cfg, utcnow(), "--dry-run" in sys.argv)
    print(f"{sum(got.values())} items put back to new: {got}")


if __name__ == "__main__":
    main()
