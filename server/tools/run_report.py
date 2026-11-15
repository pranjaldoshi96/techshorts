"""A plain-text report on a running worker: tabs, Top, traction, fast-lane lag, LLM usage, alerts.

It rebuilds the edition from a private snapshot of state.db (SQLite backup), so it never writes to or locks the
worker's database, makes no network calls and publishes nothing.
Usage (as a user that can read the state dir):  TECHGIST_STATE=/var/lib/techgist python3 tools/run_report.py
"""
import json
import os
import sqlite3
import statistics
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import config, degraded, editorial as editorial_mod, health, publish_v1, rank, sources as sources_mod  # noqa: E402
from worker.db import DB, from_iso, iso, utcnow  # noqa: E402


def snapshot(state_db, tmpdir):
    dst_path = os.path.join(tmpdir, "state.db")
    src = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    src.backup(dst)
    src.close()
    dst.close()
    return dst_path


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))] if values else None


def fmt_age(h):
    return f"{h:5.1f}h"


def card_line(c, now, score=None):
    hn = c.data["hn"].get("points") or 0
    sig = f"{c.n_publishers} pub" + (" +Techmeme" if c.on_techmeme else "") + (f" +HN {hn}" if hn else "")
    head = f"{score:.3f} " if score is not None else ""
    return (f"{head}I{c.importance} {fmt_age(c.age_h(now))} [{sig}] {c.lead_publisher}: {c.title[:110]}")


def main():
    cfg = config.load()
    srcs = sources_mod.load()
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        db = DB(snapshot(config.state_path(cfg, "state.db"), tmp))
        last = db.one("SELECT at FROM publishes WHERE ok = 1 ORDER BY at DESC LIMIT 1")
        now = from_iso(last["at"]) if last else utcnow()
        ed = editorial_mod.load(cfg, now, resolve=lambda e: publish_v1._resolve(db, e))
        ctx = publish_v1.Ctx(db, cfg, srcs, now, degraded.llm_outage(db, now), ed)
        _, report, _, tabs = publish_v1.build(ctx)
        started = from_iso(db.get_meta("worker_started_at"))
        first_pub = db.one("SELECT MIN(at) at FROM publishes WHERE ok = 1")["at"]
        out.append(f"Report as of the last publish {iso(now)} (worker started {iso(started) if started else '?'}, "
                   f"first publish {first_pub})")
        pubs = db.one("SELECT COUNT(*) n, SUM(ok) ok FROM publishes")
        out.append(f"Publishes: {pubs['ok'] or 0} ok of {pubs['n']} (each passed the 1.2 decoder check); "
                   f"cards in edition: {report['articles']}; candidates {report['candidates']}")

        out.append("\n== Tabs ==")
        for tid in rank.TAB_ORDER:
            ages = sorted(c.age_h(now) for c, _ in tabs[tid])
            if not ages:
                out.append(f"{tid:9} 0 cards   filtered: {report['filtered'].get(tid)}")
                continue
            out.append(f"{tid:9} {len(ages):2} cards  newest {fmt_age(ages[0])}  median "
                       f"{fmt_age(statistics.median(ages))}  oldest {fmt_age(ages[-1])}  under 24h: "
                       f"{sum(a < 24 for a in ages)}  filtered: {report['filtered'].get(tid)}")

        out.append("\n== Top ==")
        for i, (c, s) in enumerate(tabs["top"]):
            out.append(f"{i + 1:2}. {card_line(c, now, s)}")

        out.append("\n== Highest traction (coverage x Techmeme x HN), all candidates in any tab ==")
        shown = {c.id: c for ranked in tabs.values() for c, _ in ranked}
        trac = sorted(shown.values(), key=lambda c: -rank.coverage(c.n_publishers, c.on_techmeme, c.hn_points))
        for c in trac[:10]:
            cov = rank.coverage(c.n_publishers, c.on_techmeme, c.hn_points)
            out.append(f"x{cov:.2f} {card_line(c, now)}")
            out.append(f"       publishers: {', '.join(c.data['publishers'][:8])}")

        out.append("\n== Fast lane (stories first seen after the first publish) ==")
        rows = db.q("SELECT s.event_id, s.published_at, s.first_at, s.first_seen_at, e.headline FROM shipped s"
                    " LEFT JOIN events e ON e.id = s.event_id WHERE s.first_seen_at > ?"
                    " AND s.event_id NOT IN (SELECT twin_id FROM twins) ORDER BY s.first_at", first_pub)
        lag_pub = [(from_iso(r["first_at"]) - from_iso(r["published_at"])).total_seconds() / 60 for r in rows]
        lag_seen = [(from_iso(r["first_at"]) - from_iso(r["first_seen_at"])).total_seconds() / 60 for r in rows]
        if rows:
            out.append(f"{len(rows)} stories. publisher -> edition: p50 {pct(lag_pub, .5):.1f} min, p95 "
                       f"{pct(lag_pub, .95):.1f} min. first seen -> edition: p50 {pct(lag_seen, .5):.1f} min, p95 "
                       f"{pct(lag_seen, .95):.1f} min")
            for r, a, b in list(zip(rows, lag_pub, lag_seen))[-12:]:
                out.append(f"  {a:7.1f} min after the publisher, {b:5.1f} after first seen: {(r['headline'] or '')[:90]}")
        else:
            out.append("none yet")

        out.append("\n== LLM usage ==")
        since = now - timedelta(hours=24)
        calls = db.q("SELECT kind, model, outcome, ms, tok_in, tok_out, at FROM write_log WHERE at >= ?"
                     " AND kind != 'item'", iso(since))
        by = {}
        for r in calls:
            k = (r["model"], r["kind"])
            b = by.setdefault(k, {"n": 0, "ok": 0, "in": 0, "out": 0, "counted": 0, "ms": []})
            b["n"] += 1
            b["ok"] += r["outcome"] == "ok"
            if r["tok_in"] is not None:
                b["counted"] += 1
                b["in"] += r["tok_in"]
                b["out"] += r["tok_out"] or 0
            if r["outcome"] == "ok" and r["ms"] is not None:
                b["ms"].append(r["ms"])
        for (model, kind), b in sorted(by.items()):
            avg = (f"avg {b['in'] // b['counted']} in / {b['out'] // b['counted']} out tokens over {b['counted']} "
                   f"metered calls") if b["counted"] else "no token counts"
            out.append(f"{model} {kind:8} {b['n']:4} calls ({b['ok']} ok), p50 {pct(b['ms'], .5)} ms, "
                       f"p95 {pct(b['ms'], .95)} ms, {avg}")
        if started and first_pub:
            # Steady state = after the seed backlog cleared: calls per written item, scaled to items per day.
            items = db.one("SELECT COUNT(*) n FROM write_log WHERE kind = 'item' AND at >= ? AND"
                           " COALESCE(attempt, 1) = 1", iso(since))["n"]
            if items:
                out.append(f"{len(calls)} chat calls for {items} items on their first run = "
                           f"{len(calls) / items:.2f} calls per item")
        out.append("validation: " + json.dumps(health.validation_stats(db, since)))

        out.append("\n== Sources ==")
        bad = db.q("SELECT id, last_status, last_error, consecutive_failures FROM sources"
                   " WHERE consecutive_failures > 0 OR check_ok = 0")
        out.append(f"{len(srcs.sources)} sources; failing now: {[dict(r) for r in bad] or 'none'}")
        per = db.q("SELECT source_id, COUNT(*) n FROM items WHERE first_seen_at >= ? AND status = 'written'"
                   " GROUP BY source_id ORDER BY n DESC", iso(since))
        out.append("written items by source (24h): " + ", ".join(f"{r['source_id']} {r['n']}" for r in per))

        out.append("\n== Alerts ==")
        # From the worker's own health.json: health.alerts() would notify again.
        try:
            h = json.load(open(config.state_path(cfg, "health.json")))
            out.append(f"active at {h.get('generatedAt')}: {', '.join(h.get('alerts') or []) or 'none'}; "
                       f"llmOutage: {h.get('llmOutage')}; empty 1.2 tabs: "
                       f"{((h.get('edition') or {}).get('last') or {}).get('emptyCategories')}")
        except (OSError, ValueError) as e:
            out.append(f"health.json unreadable: {e}")
        db.close()
    print("\n".join(out))


if __name__ == "__main__":
    main()
