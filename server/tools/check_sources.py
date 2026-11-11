#!/usr/bin/env python3
"""Checks every source in sources.yaml: each must return 200 and contain at least 1 item (spec 2.2).

The result is stored in state.db, where it gates tier-1 status (a failing tier-1 source ranks as tier 2 until it
passes again) and is listed in health.json. The worker runs this at start and every `check_every_hours`.

Usage (from server/): python3 tools/check_sources.py [--json]
Exit status is 1 if any source fails.
"""
import concurrent.futures as cf
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import config, fetch, sources as sources_mod  # noqa: E402
from worker.db import DB, iso, utcnow  # noqa: E402


def check_one(source, cfg):
    # Unconditional GET: a 304 would prove nothing about the feed's contents.
    res = fetch.fetch(source, None, cfg, conditional=False)
    if res.status != 200:
        return {"id": source.id, "ok": False, "status": res.status, "items": 0, "ms": res.ms,
                "detail": res.error or f"HTTP {res.status}"}
    entries, feed = fetch.parse_entries(res.body, source, 10_000)
    if source.include_tags:
        entries = [e for e in entries if set(e.tags) & set(source.include_tags)]
    ok = len(entries) >= 1
    return {"id": source.id, "ok": ok, "status": 200, "items": len(entries), "ms": res.ms,
            "conditional": bool(res.etag or res.last_modified),
            "detail": None if ok else ("parse error" if feed.bozo else "no items")}


def run(cfg, srcs, db):
    with cf.ThreadPoolExecutor(cfg["fetch"]["parallel"]) as pool:
        results = list(pool.map(lambda s: check_one(s, cfg), srcs.sources))
    now = iso(utcnow())
    for r in results:
        db.update_source(r["id"], check_ok=int(r["ok"]), check_at=now, check_detail=r["detail"])
    db.set_meta("source_check_at", now)
    db.commit()
    return results


def main():
    cfg = config.load()
    srcs = sources_mod.load()
    db = DB(config.state_path(cfg, "state.db"))
    results = run(cfg, srcs, db)
    by_id = srcs.by_id()
    if "--json" in sys.argv:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            s = by_id[r["id"]]
            tier = sources_mod.effective_tier(s, r["ok"])
            flag = "ok  " if r["ok"] else "FAIL"
            note = r["detail"] or ""
            if s.tier == 1 and not r["ok"]:
                note += " (excluded from tier-1)"
            print(f"{flag} {r['id']:<16} {r['status']:>3} {r['items']:>4} items {r['ms']:>5} ms  tier={tier}  {note}")
    failed = [r["id"] for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)}/{len(results)} sources pass" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
