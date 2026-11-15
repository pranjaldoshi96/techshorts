#!/usr/bin/env python3
"""Re-clusters the written items of a state.db copy with the current rules and prints the events.

For checking a clustering change against a real run: the events, members and signals of the copy are thrown away
and every classified item is assigned again, oldest first, as the worker would (with the judge when keys are
available). The original file is never written.

Usage (from server/): python3 tools/recluster.py STATE_DB [--no-judge] [--grep WORD] [--cluster-model provider:id]
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import cluster, config, llm, sources as sources_mod  # noqa: E402
from worker.db import DB, from_iso  # noqa: E402


def main():
    args = sys.argv[1:]
    cfg = config.load()
    grep = None
    if "--grep" in args:
        i = args.index("--grep")
        grep = args[i + 1].lower()
        del args[i:i + 2]
    if "--cluster-model" in args:
        i = args.index("--cluster-model")
        cfg["llm"]["cluster_model"] = args[i + 1]
        del args[i:i + 2]
    use_judge = "--no-judge" not in args
    tmp = os.path.join(tempfile.mkdtemp(), "state.db")
    shutil.copy(args[0], tmp)
    db = DB(tmp)
    for t in ("events", "event_members", "event_signals", "event_alias", "twins", "judgements"):
        db.conn.execute(f"DELETE FROM {t}")
    db.commit()
    srcs = sources_mod.load()
    now = from_iso(db.one("SELECT MAX(first_seen_at) m FROM items")["m"])
    client = llm.make_router(cfg) if use_judge else None
    judge = (lambda title, text, events: llm.judge_events(client, cfg, title, text, events)) if client else None
    started = time.time()
    stats = cluster.run(db, cfg, srcs, now, None, None, judge)
    print(f"pass: {dict(stats)} in {time.time() - started:.0f}s")
    events = db.q("SELECT * FROM events WHERE status = 'live' ORDER BY published_at")
    multi = 0
    for e in events:
        members = db.q("SELECT i.title, i.source_id, i.published_at, m.how, m.cosine FROM event_members m JOIN items i"
                       " USING (url_canonical) WHERE m.event_id = ? ORDER BY i.published_at", e["id"])
        multi += len(members) > 1
        text = " ".join(m["title"] for m in members).lower()
        if (grep and grep not in text) or (not grep and len(members) < 2):
            continue
        sig = db.q("SELECT source_id, how FROM event_signals WHERE event_id = ?", e["id"])
        print(f"\n{e['id']} parent={e['parent_id']} publishers={e['n_publishers']} importance={e['importance']} "
              f"signals={[(s['source_id'], s['how']) for s in sig]}")
        for m in members:
            print(f"   {m['published_at'][5:16]} {m['source_id']:12} {m['how']:14} {m['cosine']:.2f}  {m['title'][:90]}")
    print(f"\n{len(events)} events, {multi} with 2+ members")
    if client:
        calls = [c for c in client.drain() if c[0] == "cluster"]
        ms = sorted(c[3] for c in calls)
        print(f"judge calls: {len(calls)}, errors {sum(c[2] != 'ok' for c in calls)}, "
              f"p50 {ms[len(ms) // 2] if ms else 0} ms")


if __name__ == "__main__":
    main()
