#!/usr/bin/env python3
"""Rewrites (or briefs) chosen events of a state.db with the current prompts and models, on a throwaway copy.

The event-level companion of tools/eval_cards.py, for checking a change to the rewrite or brief prompt, or to
the rules that pick the reports a rewrite sees, against real events: it prints each event's reports with their
times, the card it has, and the card the rewrite (llm.rewrite_event) or brief (llm.brief_event) writes now, with
the reason when a try fails its checks. The original file is never written.

Usage (from server/, with the provider keys available as in the worker):
  python3 tools/eval_events.py STATE_DB "headline or title words" [...]    # events matching any of them
  python3 tools/eval_events.py STATE_DB --rewritten 10                       # the newest rewritten events
  python3 tools/eval_events.py STATE_DB --briefs 10                          # the newest events with a brief
"""
import logging
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import cluster, config, llm, sources as sources_mod  # noqa: E402
from worker.db import DB, from_iso, iso  # noqa: E402


def _take(args, flag):
    if flag not in args:
        return None
    i = args.index(flag)
    value = int(args[i + 1])
    del args[i:i + 2]
    return value


def main():
    args = sys.argv[1:]
    rewritten, briefs = _take(args, "--rewritten"), _take(args, "--briefs")
    logging.basicConfig(level=logging.INFO, format="   log: %(message)s", stream=sys.stdout)
    cfg = config.load()
    tmp = os.path.join(tempfile.mkdtemp(), "state.db")
    shutil.copy(args[0], tmp)
    db = DB(tmp)
    srcs = sources_mod.load()
    now = from_iso(db.one("SELECT MAX(first_seen_at) m FROM items")["m"])
    client = llm.make_router(cfg)
    store = cluster.Store(db, srcs, cluster.Settings.from_cfg(cfg), now,
                          lambda title, text, events: llm.judge_events(client, cfg, title, text, events))
    picked = []
    events = sorted(store.events.values(), key=lambda e: e.row.get("rewritten_at") or "", reverse=True)
    if rewritten:
        picked += [e for e in events if e.row.get("text_from") == "rewrite"][:rewritten]
    if briefs:
        picked += [e for e in events if e.row.get("text_from") == cluster.BRIEF][:briefs]
    for needle in (a.lower() for a in args[1:]):
        picked += [e for e in events if e not in picked and (
            needle in (e.row.get("headline") or "").lower() or any(needle in m.title.lower() for m in e.members)
            or needle == e.id)]
    done = 0
    for ev in picked:
        row = ev.row
        lead = next((m for m in ev.members if m.url == row.get("lead_url")), None)
        hard = [m for m in ev.members if m.hard and not m.thin and m.text]
        print("=" * 100)
        print(f"EVENT   {ev.id} publishers={row.get('n_publishers')} text_from={(row.get('text_from') or '')[-40:]}")
        for m in sorted(ev.members, key=lambda m: m.published_at):
            mark = "*" if m is lead else " "
            print(f"  {mark} {iso(m.published_at)[5:16]} {m.source_id[:16]:16} {m.status}/{m.type}"
                  f"{' update' if m.material_update else ''} | {m.title[:110]}")
        print(f"OLD     {row.get('headline')}\n        {row.get('summary')}\n        ALERT {row.get('notif_title')} | "
              f"{row.get('notif_line')}")
        if row.get("type") not in cluster.HARD_NEWS:
            print("SKIPPED commentary leads this event: the worker neither rewrites nor briefs it")
            continue
        as_brief = row.get("text_from") == cluster.BRIEF or len(hard) < 1 or lead is None or not lead.text
        if as_brief:
            for name, title, text in cluster.brief_lines(store, ev):
                print(f"  LINE  {name}: {title} | {text[:300]}")
            ok = llm.brief_event(client, cfg, store, ev)
        else:
            ok = llm.rewrite_event(client, cfg, store, ev)
        done += ok
        print(f"{'BRIEF  ' if as_brief else 'REWRITE'} {'ok' if ok else 'FAILED (the old card stays)'}")
        if ok:
            print(f"NEW     {row.get('headline')}\n        {row.get('summary')}\n        ALERT {row.get('notif_title')}"
                  f" | {row.get('notif_line')}\n        importance {row.get('rewrite_importance')} spice "
                  f"{row.get('rewrite_spice')}")
    print("=" * 100)
    print(f"WRITTEN {done} of {len(picked)} events")
    by = {}
    for kind, model, outcome, ms, tin, tout in client.drain():
        k = by.setdefault((kind, model), [0, 0, 0])
        k[0] += 1
        k[1] += ms or 0
        k[2] += outcome != "ok"
    for (kind, model), (n, ms, bad) in sorted(by.items()):
        print(f"{kind:8} {model:48} calls={n} errors={bad} avg={ms // max(n, 1)}ms")


if __name__ == "__main__":
    main()
