#!/usr/bin/env python3
"""Replays the 2026-11-11 edition fixture through WP2 (text → LLM write → validate → entailment) with the real
model, and checks the WP2 acceptance criteria that depend on model behaviour. Nothing is published or stored in
state.db; results go to a JSON-lines file so a long run can resume.

Run on the box from server/ (it calls the NVIDIA API and fetches the fixture's article pages):
  python3 tools/replay_wp2.py --out ~/.techshorts-r1/replay-wp2.jsonl
  python3 tools/replay_wp2.py --out ... --report-only      # just re-print the report
  python3 tools/replay_wp2.py --no-fetch ...               # feed text only, no page fetches
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from tools import fixture  # noqa: E402
from worker import attribution, config, llm, text, validate  # noqa: E402

# (title regex, check name, predicate on the result row) for the model-dependent WP2 AC.
AC = [
    (r"^Jim Cramer says Anthropic", "Cramer: type opinion + 'Cramer:' prefix",
     lambda r: r["type"] == "opinion" and (r.get("headline") or "Cramer:").startswith("Cramer:")),
    (r"^llm-gemini 0\.36", "llm-gemini 0.36: release_notes, importance <= 4",
     lambda r: r["type"] == "release_notes" and r["importance"] <= 4),
    (r"^Gilbert \+ Tobin", "Gilbert + Tobin: customer_story", lambda r: r["type"] == "customer_story"),
    (r"Far-Right Extremists Want to Seize Control of the GOP", "Wired GOP extremists: relevance <= 1",
     lambda r: r["relevance"] <= 1),
    (r"BlackBerries and Horseshoes", "NYT BlackBerries/Presidents: relevance <= 1", lambda r: r["relevance"] <= 1),
    (r"Trump backs embattled Hegseth", "Guardian Hegseth: relevance <= 1", lambda r: r["relevance"] <= 1),
    (r"^Anthropic targets valuation of up to \$1\.4tn", "IPO (Guardian): region global",
     lambda r: r["region"] == "global"),
    (r"^Anthropic.s Giant IPO", "IPO (NYT): region global", lambda r: r["region"] == "global"),
    (r"^CoreWeave beats on revenue", "CoreWeave earnings: region global", lambda r: r["region"] == "global"),
    (r"suspected Chinese spy at centre of the Nvidia investigation", "Nvidia probe (BBC): region global",
     lambda r: r["region"] == "global"),
]


def run(items, srcs, cfg, out_path, fetch_pages=True, workers=6):
    done = set()
    if os.path.exists(out_path):
        done = {json.loads(l)["url"] for l in open(out_path) if l.strip()}
    todo = [i for i in items if i["eligible"] and i["url"] not in done]
    print(f"{len(todo)} items to run ({len(done)} already in {out_path})", flush=True)
    key = llm.api_key()
    if not key:
        sys.exit("no NVIDIA_API_KEY")
    client = llm.Client(cfg["llm"]["base_url"], key, llm.TokenBucket(cfg["llm"]["rate_per_min"], 60.0),
                        timeout=cfg["llm"]["timeout_s"])
    session = requests.Session()
    robots = text.Robots(cfg["fetch"]["user_agent"], session)
    by_id = srcs.by_id()
    lock = threading.Lock()

    def job(item):
        src = by_id[item["source_id"]]
        attr = attribution.detect(item["url"], item["title"], None, src.id)
        started = time.time()
        if fetch_pages:
            t = text.article_text(item, src, cfg["fetch"]["user_agent"], robots, session)
        else:
            t = text.choose([], item["feed_text"], item["excerpt"])
        try:
            if t.thin:
                res = llm.triage_thin(client, cfg, item, src.name, t, attr)
            else:
                res = llm.write_item(client, cfg, item, src.name, t, attr)
        except llm.AuthError:
            sys.exit("LLM auth failed")
        o = res.output
        row = {"url": item["url"], "source": src.id, "title": item["title"], "text_origin": t.origin,
               "text_words": t.words, "thin": t.thin, "status": res.status, "passed_at": res.passed_at,
               "reason": res.reason, "model": res.model, "entailed": res.entailed,
               "attribution": attr.prefix if attr else None, "seconds": round(time.time() - started, 1),
               "steps": [{k: v for k, v in s.__dict__.items() if k != "feedback"} | {"feedback": s.feedback}
                         for s in res.steps]}
        if o is not None:
            row.update(type=o.type, is_news=o.is_news, relevance=o.relevance, importance=o.importance,
                       region=o.region, topics=o.topics, breaking=o.breaking, sensitive=o.sensitive)
            if res.status == "ok":
                row.update(headline=o.headline, summary=res.full, summary_short=res.short, notif_line=o.notif_line)
            elif res.status == "failed":     # the rejected text, so a held item's failure can be read, not guessed
                row.update(rejected={"headline": o.headline, "s1": o.s1, "s2": o.s2, "s3": o.s3,
                                     "notif_line": o.notif_line})
        with lock:
            with open(out_path, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{res.status}{'@' + str(res.passed_at) if res.passed_at else ''}] {src.id}: {item['title'][:70]}"
              f" -> {row.get('headline') or res.reason}", flush=True)

    with cf.ThreadPoolExecutor(workers) as pool:
        list(pool.map(job, todo))


def report(items, out_path):
    rows = [json.loads(l) for l in open(out_path) if l.strip()]
    eligible = [i for i in items if i["eligible"]]
    full = [r for r in rows if not r["thin"]]
    thin = [r for r in rows if r["thin"]]
    ok = [r for r in full if r["status"] == "ok"]
    within_one = [r for r in ok if r["passed_at"] <= 2]
    print(f"\nFixture: {len(items)} items, {len(eligible)} eligible (current non-signal source, kept by prefilter); "
          f"{len(rows)} run")
    print(f"Text: {len(full)} with >= 60 words, {len(thin)} thin (held as no_text, classified only)")
    by_origin = {}
    for r in rows:
        by_origin[r["text_origin"]] = by_origin.get(r["text_origin"], 0) + 1
    print(f"Text origin: {by_origin}")
    if full:
        print(f"Pass within one retry: {len(within_one)}/{len(full)} = {len(within_one) / len(full):.1%}; "
              f"with fallback: {len(ok)}/{len(full)} = {len(ok) / len(full):.1%}")
    fails = {}
    for r in full:
        if r["status"] != "ok":
            fails[r["reason"]] = fails.get(r["reason"], 0) + 1
    print(f"Held after the ladder, by reason: {fails}")
    first = {}
    for r in full:
        for s in r["steps"][:1]:
            first[s["reason"] or "ok"] = first.get(s["reason"] or "ok", 0) + 1
    print(f"First-attempt outcome: {first}")

    bad = []
    for r in ok:
        f, s = validate.word_count(r["summary"]), validate.word_count(r["summary_short"])
        if not (45 <= f <= 62 and 28 <= s <= 42 and len(r["headline"]) <= 72 and len(r["notif_line"]) <= 110):
            bad.append(r["url"])
    print(f"Shipped outputs out of range: {len(bad)} of {len(ok)} {bad[:5]}")
    strat = [r for r in ok if r["source"] == "stratechery"]
    print(f"Stratechery summaries containing 'subscriber': "
          f"{sum('subscriber' in (r['summary'] or '').lower() for r in strat)} of {len(strat)}")
    print("\nModel-dependent AC:")
    passed = 0
    for rx, name, pred in AC:
        r = next((r for r in rows if re.search(rx, r["title"]) and "type" in r), None)
        if r is None:
            print(f"  MISSING  {name}")
            continue
        good = pred(r)
        passed += good
        print(f"  {'PASS' if good else 'FAIL'}  {name}  (type={r['type']}, importance={r['importance']}, "
              f"relevance={r['relevance']}, region={r['region']}, status={r['status']}, "
              f"headline={r.get('headline')!r})")
    print(f"  {passed}/{len(AC)} pass")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--only", help="regex on titles")
    ap.add_argument("--model", help="override llm.model (to compare models)")
    ap.add_argument("--fallback", help="override llm.fallback_model ('none' for no third rung)")
    args = ap.parse_args()
    out = os.path.expanduser(args.out)
    cfg = config.load()
    if args.model:
        cfg["llm"]["model"] = args.model
    if args.fallback:
        cfg["llm"]["fallback_model"] = None if args.fallback == "none" else args.fallback
    items, srcs = fixture.load()
    if args.only:
        items = [i for i in items if re.search(args.only, i["title"])]
    if not args.report_only:
        run(items, srcs, cfg, out, fetch_pages=not args.no_fetch, workers=cfg["llm"]["concurrency"])
    report(fixture.load()[0] if args.only else items, out)


if __name__ == "__main__":
    main()
