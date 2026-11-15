#!/usr/bin/env python3
"""Writes cards for chosen items of a state.db with the current prompts and models, without touching the DB.

For checking a prompt or model change against cards that went wrong before: it runs the same ladder as the worker
(write -> deterministic checks -> fact-check) and prints the card, the scores and each step's outcome.

Usage (from server/, with the provider keys available as in the worker):
  python3 tools/eval_cards.py STATE_DB "title words" ["more title words" ...]     # items whose title contains them
  python3 tools/eval_cards.py STATE_DB --recent 20                                  # the newest written items
Options: --model provider:id  --judge provider:id  (override config.yaml)
         --without PROVIDER   run as if that provider were down (no client for it), so every call goes to the
                              backup models: the replay test for an outage of the hub
         --min-pass 0.6       exit with status 1 when fewer than this share of the cards pass (the bar the backup
                              models must clear; run it after changing a prompt, a check or llm.backup_models)
"""
import concurrent.futures as cf
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import attribution, config, llm, sources as sources_mod, text as text_mod  # noqa: E402


def main():
    args = sys.argv[1:]
    cfg = config.load()
    for flag, key in (("--model", "model"), ("--judge", "judge_model")):
        if flag in args:
            i = args.index(flag)
            cfg["llm"][key] = args[i + 1]
            del args[i:i + 2]
    without, min_pass = None, None
    if "--without" in args:
        i = args.index("--without")
        without = args[i + 1]
        del args[i:i + 2]
    if "--min-pass" in args:
        i = args.index("--min-pass")
        min_pass = float(args[i + 1])
        del args[i:i + 2]
    recent = None
    if "--recent" in args:
        i = args.index("--recent")
        recent = int(args[i + 1])
        del args[i:i + 2]
    db = sqlite3.connect(f"file:{args[0]}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    srcs = sources_mod.load().by_id()
    rows = []
    if recent:
        rows = db.execute("SELECT i.*, w.text, w.headline old_headline, w.summary old_summary FROM items i JOIN writes w"
                          " USING (url_canonical) WHERE w.text_words >= 60 ORDER BY i.published_at DESC LIMIT ?",
                          (recent,)).fetchall()
    for needle in args[1:]:
        rows += db.execute("SELECT i.*, w.text, w.headline old_headline, w.summary old_summary FROM items i JOIN writes w"
                           " USING (url_canonical) WHERE i.title LIKE ? AND w.text_words >= 60 LIMIT 2",
                           (f"%{needle}%",)).fetchall()
    client = llm.make_router(cfg)
    if without:
        client.clients.pop(without, None)
    passed = []

    def one(r):
        src = srcs.get(r["source_id"])
        name = src.name if src else r["source_id"]
        text = text_mod.ArticleText(r["text"], "page", [])
        author = json.loads(r["extra"] or "{}").get("author")
        attr = attribution.detect(r["url"], r["title"], author, r["source_id"])
        started = time.time()
        res = llm.write_item(client, cfg, dict(r), name, text, attr, author)
        return r, res, time.time() - started

    with cf.ThreadPoolExecutor(4) as pool:
        for r, res, secs in pool.map(one, rows):
            o = res.output
            passed.append(res.status == "ok")
            print("=" * 100)
            print(f"SOURCE  [{r['source_id']}] {r['title']}")
            print(f"OLD     {r['old_headline']}")
            print(f"STATUS  {res.status} in {secs:.1f}s; steps: "
                  + "; ".join(f"{s.rung}:{s.outcome}" + (f" ({s.reason})" if s.reason else "") for s in res.steps))
            for s in res.steps:
                for fb in s.feedback:
                    print(f"   feedback: {fb[:300]}")
            if o is not None:
                print(f"SCORES  type={o.type} relevance={o.relevance} importance={o.importance} spice={o.spice} "
                      f"topics={o.topics} sensitive={o.sensitive}")
                print(f"HEAD    {o.headline}")
                print(f"SUMMARY {res.full or ' '.join([o.s1, o.s2, o.s3])}")
                print(f"ALERT   {o.notif_title} | {o.notif_line}")
    calls = client.drain()
    by = {}
    for kind, model, outcome, ms, tin, tout in calls:
        k = by.setdefault((kind, model), [0, 0, 0, 0, 0])
        k[0] += 1
        k[1] += ms or 0
        k[2] += tin or 0
        k[3] += tout or 0
        k[4] += outcome != "ok"
    print("=" * 100)
    share = sum(passed) / len(passed) if passed else 0.0
    print(f"PASSED  {sum(passed)} of {len(passed)} cards ({share:.0%})"
          + (f" with provider {without} down" if without else ""))
    for (kind, model), (n, ms, tin, tout, bad) in sorted(by.items()):
        print(f"{kind:8} {model:48} calls={n} errors={bad} avg={ms // max(n, 1)}ms tokens in/out per call="
              f"{tin // max(n, 1)}/{tout // max(n, 1)}")
    if min_pass is not None and share < min_pass:
        print(f"FAIL    pass rate {share:.0%} is under the bar of {min_pass:.0%}")
        sys.exit(1)


if __name__ == "__main__":
    main()
