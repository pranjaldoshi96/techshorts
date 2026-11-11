"""Techgist worker: one long-running loop with a 60 s tick (spec section 2.1).

fetch (conditional GET) → prefilter → timestamps → article text → embed + candidate events → LLM write → validate →
entailment → cluster (serial assign, signals, merge, lead, event importance, rewrite) → rank → tabs → images →
v1 edition.json (checked by the 1.2 decoder) → health.json, with state in SQLite. If the LLM is out, the edition
is published in degraded form (degraded.py) rather than not at all.

Run from server/:
  python3 -m worker.main --out DIR              # loop forever, a tick every 60 s (the systemd service)
  python3 -m worker.main --once --out DIR       # one tick
  python3 -m worker.main --once --all           # one tick polling every source, due or not
DIR defaults to publish.out_dir (<state_dir>/edition). Set TECHGIST_STATE to keep a test copy's state apart from
the live one (e.g. ~/.techshorts-r1).
"""
import argparse
import concurrent.futures as cf
import logging
import os
import time
from collections import Counter
from datetime import timedelta

import requests

from . import cluster, config, degraded, embed, fetch, health, llm, mirror, publish_v1, sources as sources_mod
from .db import DB, from_iso, iso, utcnow

log = logging.getLogger("techgist")


def due_sources(db, srcs, now, poll_all=False, slack=timedelta(seconds=30)):
    # Half a tick of slack, so a 3 min poll happens on the 3rd tick rather than slipping to the 4th.
    out = []
    for s in srcs.sources:
        nxt = from_iso(db.source_state(s.id)["next_poll_at"])
        if poll_all or nxt is None or nxt <= now + slack:
            out.append(s)
    return out


def fetch_due(db, srcs, cfg, poll_all=False):
    """Fetches due sources in parallel, then records results serially. Returns per-tick counts."""
    now = utcnow()
    due = due_sources(db, srcs, now, poll_all)
    if not due:
        return Counter()
    states = {s.id: db.source_state(s.id) for s in due}
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=cfg["fetch"]["parallel"]))
    with cf.ThreadPoolExecutor(cfg["fetch"]["parallel"]) as pool:
        results = list(pool.map(lambda s: (s, fetch.fetch(s, states[s.id], cfg, session)), due))
    tick = Counter()
    for source, res in results:
        stats = fetch.record(db, source, srcs, res, now, cfg)
        tick["polled"] += 1
        tick[f"http_{res.status}"] += 1
        for k in ("new", "dropped", "stale", "seen"):
            tick[k] += getattr(stats, k)
        if res.error:
            log.info("%s: %s", source.id, res.error)
        fails = db.source_state(source.id)["consecutive_failures"]
        if fails >= cfg["fetch"]["failure_alert_after"]:
            health.notify(db, f"source_failing:{source.id}", f"{source.name} failed {fails} polls in a row ({res.error})")
    db.commit()
    return tick


def maybe_check_sources(db, srcs, cfg):
    last = from_iso(db.get_meta("source_check_at"))
    if last and utcnow() - last < timedelta(hours=cfg["check_every_hours"]):
        return
    from tools import check_sources
    results = check_sources.run(cfg, srcs, db)
    failed = [r["id"] for r in results if not r["ok"]]
    log.info("source check: %d/%d pass%s", len(results) - len(failed), len(results),
             f"; failed {failed}" if failed else "")


def make_client(cfg):
    key = llm.api_key()
    if not cfg["llm"].get("model") or not key:
        log.warning("no LLM model or NVIDIA_API_KEY; card writing is off")
        return None
    bucket = llm.TokenBucket(rate=cfg["llm"]["rate_per_min"], per=60.0)
    return llm.Client(cfg["llm"]["base_url"], key, bucket, timeout=cfg["llm"]["timeout_s"])


def probe(db, cfg, client):
    """One tiny chat call, to notice a revoked key coming back when there is nothing to write."""
    try:
        client.chat(cfg["llm"]["model"], [{"role": "user", "content": "Reply with {}"}], max_tokens=4, kind="probe",
                    tries=1)
    except llm.LLMError:
        pass
    now = utcnow()
    for kind, model, outcome, ms in client.drain():
        db.log_write(now, None, kind, model, outcome, None, ms)


def tick(db, cfg, poll_all=False, client=None, embedder=None, out_dir=None, started_at=None):
    has_client = client is not None
    # Reloaded every tick so a sources.yaml edit applies without a restart.
    srcs = sources_mod.load()
    maybe_check_sources(db, srcs, cfg)
    counts = fetch_due(db, srcs, cfg, poll_all)
    if client is not None:
        degraded.key_check(db, cfg, client.key, utcnow())
    # A revoked key (401/403) would fail every call; the LLM stages wait for a probe to see it work again.
    if client is not None and degraded.llm_outage(db, utcnow()) == "auth":
        if degraded.needs_probe(db, utcnow()):
            probe(db, cfg, client)
        if degraded.llm_outage(db, utcnow()) == "auth":
            client, embedder = None, None
            counts["llm_auth_outage"] += 1
    if client is not None:
        cands = None
        if embedder is not None:
            def cands(prepared):
                now = utcnow()
                return cluster.candidates(db, cfg, cluster.embed_items(db, cfg, embedder, prepared, now), now)
        counts.update({f"write_{k}": v for k, v in llm.write_pending(db, srcs, cfg, client,
                                                                      candidates_for=cands).items()})
    # Runs without embeddings too: it then refreshes events (signals, coverage, importance) from what is stored.
    rewriter = (lambda store, ev: llm.rewrite_event(client, cfg, store, ev)) if client is not None else None
    counts.update({f"cluster_{k}": v for k, v in cluster.run(db, cfg, srcs, utcnow(), embedder,
                                                             rewriter).items() if v})
    now = utcnow()
    outage = degraded.llm_outage(db, now, have_client=has_client)
    report = None
    if out_dir:
        report = publish_v1.publish(db, cfg, srcs, now, out_dir, embedder, outage)
        counts["published"] += bool(report.get("published"))
        if report.get("published"):
            pushed = mirror.push(db, cfg, out_dir, now)
            if pushed and pushed != "waiting":
                counts[f"mirror_{pushed.split(':')[0]}"] += 1
        if outage:
            log.warning("degraded publish (%s): %d cards, %d without AI text", outage, report["articles"],
                        report["degradedCards"])
    alerts = health.alerts(db, now, started_at)
    if report and report.get("published") and report.get("emptyCategories"):
        empty = ", ".join(report["emptyCategories"])
        log.warning("v1 edition has no %s articles; 1.2 shows those tabs empty", empty)
        health.notify(db, "v1_empty_category", f"1.2 tabs with no articles: {empty}")
        alerts.append("v1_empty_category")
    extra = {"llmOutage": outage, "alerts": alerts,
             "llmBudget": {"used24h": db.llm_calls_since(now - timedelta(hours=24)),
                           "daily": cfg["llm"].get("daily_budget")}}
    health.write(db, srcs, config.state_path(cfg, "health.json"), report, now, extra)
    if out_dir and report and report.get("published"):
        health.write(db, srcs, os.path.join(out_dir, "health.json"), report, now, extra)
    db.commit()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one tick and exit")
    ap.add_argument("--all", action="store_true", help="poll every source, not just due ones")
    ap.add_argument("--out", help="directory to publish edition.json, health.json and img/ into "
                                  "(default publish.out_dir)")
    ap.add_argument("--no-publish", action="store_true", help="fetch, write and cluster only")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load()
    db = DB(config.state_path(cfg, "state.db"))
    out_dir = None if args.no_publish else os.path.expanduser(
        args.out or (cfg.get("publish") or {}).get("out_dir") or os.path.join(cfg["state_dir"], "edition"))
    log.info("worker start, state in %s, publishing to %s", cfg["state_dir"], out_dir)
    client = make_client(cfg)
    embedder = embed.make(cfg, llm.api_key())
    if embedder is None:
        log.warning("no embedding model or key; clustering is off")
    started_at = utcnow()
    db.set_meta("worker_started_at", iso(started_at))
    while True:
        started = time.monotonic()
        try:
            counts = tick(db, cfg, args.all, client, embedder, out_dir, started_at)
            if counts:
                log.info("tick: %s", dict(sorted(counts.items())))
        except Exception:
            # One bad tick (a malformed sources.yaml edit, a disk hiccup) must not kill the loop.
            log.exception("tick failed")
            if args.once:
                raise
        if args.once:
            break
        time.sleep(max(1.0, cfg["tick_seconds"] - (time.monotonic() - started)))
    db.close()


if __name__ == "__main__":
    main()
