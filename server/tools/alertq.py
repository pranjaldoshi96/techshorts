#!/usr/bin/env python3
"""Looks into the durable push-alert queue (worker/alertq.py) and, for testing it, puts a row in or walks one
through the sender's steps. Run from server/ with TECHGIST_STATE set, as the worker's user:

  tools/alertq.py                      counts by status and the waiting rows (read-only)
  tools/alertq.py list [--all]         rows, newest first
  tools/alertq.py enqueue ID "title" "body"     queue a test row (says queued or duplicate)
  tools/alertq.py claim                what the sender does first: take the best waiting row (it becomes `sending`)
  tools/alertq.py sent ROW_ID          ... and after a successful send
  tools/alertq.py cancel ID            drop a waiting row (the owner's kill)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import alertq, config  # noqa: E402
from worker.db import DB, utcnow  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="stats", choices=["stats", "list", "enqueue", "claim", "sent", "cancel"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--all", action="store_true", help="list: include finished rows")
    a = ap.parse_args(argv)
    cfg = config.load()
    db = DB(config.state_path(cfg, "state.db"))
    db.conn.execute("PRAGMA busy_timeout = 30000")
    alertq.ensure(db)
    now = utcnow()
    if a.cmd in ("stats", "list"):
        print(alertq.stats(db, now))
        where = "" if a.all else "WHERE status IN ('queued', 'sending')"
        for r in db.q(f"SELECT * FROM alert_queue {where} ORDER BY id DESC LIMIT 50"):
            print(f"{r['id']:>5} {r['status']:<9} {r['event_id']:<28} score={r['score']} attempts={r['attempts']}"
                  f" queued={r['queued_at']} sent={r['sent_at']}  {r['title']}")
    elif a.cmd == "enqueue":
        event_id, title, body = a.args
        print(alertq.enqueue(db, event_id, title, body, now, score=0.0, published_at=now))
    elif a.cmd == "claim":
        for al in alertq.claim(db, now):
            print(f"claimed row {al.id} event {al.event_id} collapse_id {al.collapse_id} attempt {al.attempts}"
                  f" redelivery={al.redelivery}: {al.title}")
    elif a.cmd == "sent":
        alertq.mark_sent(db, int(a.args[0]), now)
        print("marked sent")
    elif a.cmd == "cancel":
        print(f"cancelled {alertq.cancel(db, a.args[0])}")
    db.commit()
    db.close()


if __name__ == "__main__":
    main()
