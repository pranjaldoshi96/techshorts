"""Techgist worker: one long-running loop with a 60 s tick (spec section 2.1).

R1 so far: fetch (conditional GET) → prefilter → timestamps → article text → embed + candidate events → LLM write →
validate → entailment → cluster (serial assign, signals, merge, lead, event importance, rewrite), with state in
SQLite and health.json. Ranking and the v1 publish (WP4) plug in after `cluster.run`.

Run from server/:
  python3 -m worker.main              # loop forever (the systemd service)
  python3 -m worker.main --once       # one tick
  python3 -m worker.main --once --all # one tick polling every source, due or not
Set TECHGIST_STATE to keep a test copy's state apart from the live one (e.g. ~/.techshorts-r1).
"""
import argparse
import concurrent.futures as cf
import logging
import time
from collections import Counter
from datetime import timedelta

import requests

from . import cluster, config, embed, fetch, health, llm, sources as sources_mod
from .db import DB, from_iso, utcnow

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


def tick(db, cfg, poll_all=False, client=None, embedder=None):
    # Reloaded every tick so a sources.yaml edit applies without a restart.
    srcs = sources_mod.load()
    maybe_check_sources(db, srcs, cfg)
    counts = fetch_due(db, srcs, cfg, poll_all)
    if client is not None:
        cands = None
        if embedder is not None:
            def cands(prepared):
                now = utcnow()
                return cluster.candidates(db, cfg, cluster.embed_items(db, cfg, embedder, prepared, now), now)
        counts.update({f"write_{k}": v for k, v in llm.write_pending(db, srcs, cfg, client,
                                                                      candidates_for=cands).items()})
    if embedder is not None:
        rewriter = (lambda store, ev: llm.rewrite_event(client, cfg, store, ev)) if client is not None else None
        counts.update({f"cluster_{k}": v for k, v in cluster.run(db, cfg, srcs, utcnow(), embedder,
                                                                 rewriter).items() if v})
    # WP4: rank → tabs → publish v1.
    health.write(db, srcs, config.state_path(cfg, "health.json"))
    db.commit()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one tick and exit")
    ap.add_argument("--all", action="store_true", help="poll every source, not just due ones")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load()
    db = DB(config.state_path(cfg, "state.db"))
    log.info("worker start, state in %s", cfg["state_dir"])
    client = make_client(cfg)
    embedder = embed.make(cfg, llm.api_key())
    if embedder is None:
        log.warning("no embedding model or key; clustering is off")
    while True:
        started = time.monotonic()
        try:
            counts = tick(db, cfg, args.all, client, embedder)
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
