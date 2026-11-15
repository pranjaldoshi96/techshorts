#!/usr/bin/env python3
"""Re-scores stored items with the current write prompt: type, importance and topics only, never their card text.

For a prompt recalibration (the importance anchors, the dev_tools and ai topic rules), so the events still in the
tab windows are ranked by the new scale instead of waiting days for fresh items. Each item gets one classification
call on its stored text, with its own event as the candidate when it joined an event someone else started (so the
follow-up rule applies as it would have live). Events' rewrite re-scores are cleared, so the next clustering pass
takes the member median until the next rewrite (with the same anchors) replaces it.

Run from server/ against a test copy's state only, with the worker running or not:
  TECHGIST_STATE=~/.techshorts-r1 python3 -m tools.rescore --hours 72 [--rate 15] [--dry-run]
  ... --hours 168 --from-hours 72 --topics dev_tools,security     # the rest of Dev's 7-day window
Old values are saved to <state_dir>/rescore-<time>.json first.
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import attribution as attribution_mod, config, llm, sources as sources_mod  # noqa: E402
from worker.db import DB, iso, utcnow  # noqa: E402
from worker import rank  # noqa: E402
from worker.importance import HARD_NEWS  # noqa: E402

SQL = """
SELECT w.url_canonical, w.status, w.importance, w.topics, w.relevance, w.type, w.text, i.url, i.title,
       i.published_at, i.source_id, i.extra, em.event_id, e.anchor_url, e.headline AS ev_headline, e.s1 AS ev_s1
FROM writes w JOIN items i USING (url_canonical)
LEFT JOIN event_members em USING (url_canonical)
LEFT JOIN events e ON e.id = em.event_id
WHERE w.type IS NOT NULL AND w.text IS NOT NULL AND i.published_at >= ?
"""


def histogram(rows, key="importance"):
    hard = [r[key] for r in rows if r["type"] in HARD_NEWS and (r["relevance"] or 0) >= 2 and r[key]]
    return {"hardNews": len(hard), "hist": {i: hard.count(i) for i in range(1, 11)},
            "share>=6": round(sum(v >= 6 for v in hard) / len(hard), 3) if hard else None,
            "share>=8": round(sum(v >= 8 for v in hard) / len(hard), 3) if hard else None}


def new_type(row, res):
    """A new type is taken only when it takes the item out of every tab (explainer, podcast...): a stored card was
    validated for its old type, so turning news into analysis would ship it without its "Analysis:" label."""
    return res.type if res.type in rank.NEVER_TYPES else row["type"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--rate", type=int, default=15, help="calls per minute (the worker has its own 30)")
    ap.add_argument("--min-importance", type=int, default=1, help="only items currently scored at least this")
    ap.add_argument("--from-hours", type=float, default=0, help="only items published at least this long ago")
    ap.add_argument("--topics", default="", help="only items with one of these comma-separated topics")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = config.load()
    if os.path.realpath(os.path.expanduser(cfg["state_dir"])) == os.path.realpath(os.path.expanduser("~/.techshorts")):
        sys.exit("refusing to re-score the live state; set TECHGIST_STATE to a test copy")
    db = DB(config.state_path(cfg, "state.db"))
    db.conn.execute("PRAGMA busy_timeout = 120000")
    srcs = sources_mod.load()
    by_id = srcs.by_id()
    now = utcnow()
    want = set(filter(None, args.topics.split(",")))
    rows = [dict(r) for r in db.q(SQL, iso(now - timedelta(hours=args.hours)))
            if (r["importance"] or 0) >= args.min_importance
            and r["published_at"] <= iso(now - timedelta(hours=args.from_hours))
            and (not want or want & set(json.loads(r["topics"] or "[]")))]
    print(f"{len(rows)} items; before: {histogram(rows)}")
    if args.dry_run:
        return
    backup = config.state_path(cfg, f"rescore-{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump([{k: r[k] for k in ("url_canonical", "importance", "topics", "type")} for r in rows], open(backup, "w"))
    key = llm.api_key()
    client = llm.Client(cfg["llm"]["base_url"], key, llm.TokenBucket(rate=args.rate, per=60.0),
                        timeout=cfg["llm"]["timeout_s"])

    def job(r):
        src = by_id.get(r["source_id"])
        author = json.loads(r["extra"] or "{}").get("author")
        attr = attribution_mod.detect(r["url"], r["title"], author, r["source_id"])
        cands = ()
        if r["event_id"] and r["anchor_url"] != r["url_canonical"] and r["ev_headline"]:
            cands = [{"id": r["event_id"], "headline": r["ev_headline"], "s1": r["ev_s1"] or ""}]
        item = {"url": r["url"], "title": r["title"], "published_at": r["published_at"]}
        try:
            reply = client.chat(cfg["llm"]["model"], llm.build_messages(item, src.name if src else r["source_id"],
                                                                        r["text"], attr, cands), kind="triage",
                                tries=2)
            return r, llm.parse(reply)
        except (llm.LLMError, llm.BadOutput) as e:
            return r, e

    done, failed, after = 0, 0, []
    with cf.ThreadPoolExecutor(3) as pool:
        for r, res in pool.map(job, rows):
            if isinstance(res, Exception):
                failed += 1
                after.append(r)
                continue
            db.conn.execute("UPDATE writes SET importance = ?, topics = ?, type = ? WHERE url_canonical = ?",
                            (res.importance, json.dumps(res.topics), new_type(r, res), r["url_canonical"]))
            db.commit()
            after.append({**r, "importance": res.importance, "topics": json.dumps(res.topics),
                          "type": new_type(r, res)})
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(rows)}", flush=True)
    db.conn.execute("UPDATE events SET rewrite_importance = NULL WHERE status = 'live' AND published_at >= ?",
                    (iso(now - timedelta(hours=args.hours)),))
    db.commit()
    print(f"re-scored {done}, failed {failed}; backup {backup}")
    print(f"after: {histogram(after)}")
    topics = lambda rs, t: sum(t in json.loads(x["topics"] or "[]") for x in rs)  # noqa: E731
    for t in ("ai", "dev_tools"):
        print(f"  topic {t}: {topics(rows, t)} -> {topics(after, t)}")
    first = lambda rs: sum((json.loads(x["topics"] or "[]") or [None])[0] == "dev_tools" for x in rs)  # noqa: E731
    print(f"  dev_tools first: {first(rows)} -> {first(after)}")


if __name__ == "__main__":
    main()
