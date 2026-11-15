#!/usr/bin/env python3
"""Applies to the cards a state already holds the rules that otherwise act only when an item is written or clustered.

A rule added to the write or clustering path changes new items. The events already in the tab windows keep what
the old rules gave them for days: after the third review's deploy "ULA launches Vulcan with 40 Amazon internet
satellites" still sat in Top beside the card reporting the rocket lost (the supersede rule fires when a follow-up
is clustered), "Apple starts selling HomeView" still read 90 minutes old (the writer is asked for `news_date`
only when it writes), and the FAA outage card still gave the first day's cause (a rewrite is queued by a report
published after the last one). Four passes, each the live rule applied to stored rows:

  superseded  every follow-up event's first report is put to the clustering judge again, against the older
              events near it, with the current prompt; a "supersedes" ruling, or a "followup" whose title
              reports a reversal, marks the older event (cluster.supersede), as it would have at clustering.
  dates       written cards with no news date get one: the first sentence's own statement (newsdate.lede_date),
              and with --ask-dates the writer's `news_date` for the leads of events under --ask-hours old
              (llm.date_news, one small call each). cluster.refresh then moves the event's clock back.
  rewrites    events with two or more publishers that hold a hard-news report hours newer than their lead are
              queued for a rewrite, which now gets each report's time and states the newest account.
  recheck     every card of an edition is put to the fact-checker again with the current rules (entail.PROMPT).
              A card that fails goes back: a lead's own card to the writer with the reason (as a hold retry), a
              rewrite to its lead's card and the rewrite queue, a brief to the brief queue. Nothing is loosened:
              a card that fails again is dropped by the usual rules.

Every model call is made before the first write, so the database is locked for milliseconds; all the same, stop
the worker while this runs on its state (a tick that loaded an event before the change would save its old row
back), and take a backup first (deploy/backup_state.py).

Run from server/:
  TECHGIST_STATE=<state dir> python3 -m tools.reexamine [--only superseded,dates,rewrites,recheck] [--hours 72]
      [--ask-dates] [--ask-hours 36] [--edition PATH/edition.json] [--dry-run] [--verbose]
`recheck` needs --edition (the cards it checks are the ones that edition shows).
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import cluster, config, entail, llm, newsdate, sources as sources_mod, text as text_mod  # noqa: E402
from worker.db import DB, from_iso, iso, utcnow  # noqa: E402

PASSES = ("superseded", "dates", "rewrites", "recheck")
WORKERS = 6
RECHECK_MEMBERS = 6


def _parallel(fn, jobs):
    if not jobs:
        return []
    with cf.ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(fn, jobs))


# superseded

def superseded_plan(store, since):
    """[(event, [(cosine, older event)])]: each event whose first report is hard news published since `since`,
    with the older events near it that the judge would have been shown (cluster.judged_candidates)."""
    s, jobs = store.s, []
    for ev in store.events.values():
        item = ev.anchor
        if item is None or not item.hard or item.thin or item.published_at < since:
            continue
        older = [(cos, o) for cos, o in store.ranked(item.vec)
                 if cos >= s.t_join and o is not ev and o.anchor.published_at < item.published_at
                 and not o.row.get("superseded_by") and o.row.get("headline")][:s.max_candidates]
        if older:
            jobs.append((ev, older))
    return jobs


def superseded(store, since, dry_run=False, rulings=None):
    """Marks the events a later event has overtaken. Returns [(old event, new event, relation)]; every ruling
    asked for is appended to `rulings` when given, for --verbose."""
    if store.judge is None:
        return []
    jobs = superseded_plan(store, since)

    def ask(job):
        ev, older = job
        item = ev.anchor
        try:
            return store.judge(item.title, item.text or item.headline or "",
                               [(o.id, o.row.get("headline"), o.anchor.title) for _, o in older])
        except llm.LLMError:
            return None
    out = []
    for (ev, older), got in zip(jobs, _parallel(ask, jobs)):
        store.judge_calls += 1
        for _, old in older:
            rel = (got or {}).get(old.id)
            if rulings is not None:
                rulings.append((old, ev, rel))
            if rel == cluster.SUPERSEDES or (rel == "followup" and cluster._overtakes_event(old, ev.anchor)):
                # Never both ways, and never an event that has itself been overtaken by the one it would replace.
                if ev.row.get("superseded_by") == old.id or old.row.get("superseded_by"):
                    continue
                out.append((old, ev, rel))
                if not dry_run:
                    cluster.supersede(store, old, ev.id)
    return out


# dates

DATE_SQL = """
SELECT w.url_canonical, w.text, w.headline, w.type, i.title, i.published_at,
       EXISTS (SELECT 1 FROM events e WHERE e.lead_url = w.url_canonical AND e.status = 'live'
               AND e.headline IS NOT NULL) AS leads
FROM writes w JOIN items i USING (url_canonical)
WHERE w.status = 'ok' AND w.news_at IS NULL AND w.text IS NOT NULL AND i.published_at >= ?
"""


def dates(db, since, ask=None, ask_since=None, dry_run=False):
    """Gives written cards with no news date one (see the module docstring). `ask(published, title, headline,
    text)` is the writer's `news_date` call, used for event leads published since `ask_since`.
    Returns [(url, title, article time, news time, "lede" | "writer")]."""
    rows = db.q(DATE_SQL, iso(since))
    found, to_ask = [], []
    for r in rows:
        published = from_iso(r["published_at"])
        at = newsdate.news_at("", published, r["title"], r["text"], r["type"])
        if at is not None:
            found.append((r["url_canonical"], r["title"], published, at, "lede"))
        elif ask is not None and r["leads"] and r["type"] in newsdate.DATED_TYPES \
                and ask_since is not None and published >= ask_since:
            to_ask.append(r)

    def one(r):
        try:
            return ask(r["published_at"], r["title"], r["headline"], r["text"])
        except llm.LLMError:
            return ""
    for r, day in zip(to_ask, _parallel(one, to_ask)):
        published = from_iso(r["published_at"])
        at = newsdate.news_at(day, published, r["title"], r["text"], r["type"]) if day else None
        if at is not None:
            found.append((r["url_canonical"], r["title"], published, at, "writer"))
    if not dry_run:
        for url, _, _, at, _ in found:
            db.conn.execute("UPDATE writes SET news_at = ? WHERE url_canonical = ?", (iso(at), url))
    return found


# rewrites

def rewrites(store, since, dry_run=False):
    """Queues a rewrite for each event that holds a hard-news report hours newer than its lead (the report the
    rewrite now leads with, llm.rewrite_event). Returns the events queued."""
    out = []
    for ev in store.events.values():
        row = ev.row
        if (row.get("n_publishers") or 0) < 2 or row.get("type") not in cluster.HARD_NEWS \
                or from_iso(row["published_at"]) < since or "newer_report" in (row.get("rewrite_reason") or ""):
            continue
        lead = next((m for m in ev.members if m.url == row.get("lead_url")), None)
        if lead is None or not any(m.hard and not m.thin and m is not lead
                                   and m.published_at - lead.published_at >= cluster.NEWER_REPORT
                                   for m in ev.members):
            continue
        out.append(ev)
        if not dry_run:
            cluster._queue_rewrite(row, "newer_report")
            store.db.conn.execute("UPDATE events SET rewrite_reason = ? WHERE id = ?",
                                  (row["rewrite_reason"], ev.id))
    return out


# recheck

def _rest(summary, s1):
    """The card's sentences after the first, as one line for the fact-checker."""
    summary, s1 = (summary or "").strip(), (s1 or "").strip()
    return summary[len(s1):].strip() if s1 and summary.startswith(s1) else ""


def recheck_plan(store, event_ids):
    """[(event, kind, job)] for the cards of `event_ids`: kind is "card" (the lead's own card, checked against
    its text), "rewrite" (against its members' texts) or "brief" (against the lines it was written from)."""
    jobs = []
    for eid in event_ids:
        ev = store.events.get(store.resolve(eid))
        if ev is None or not ev.row.get("headline"):
            continue
        row = ev.row
        lead = next((m for m in ev.members if m.url == row.get("lead_url")), None)
        if lead is None:
            continue
        src = store.by_id.get(lead.source_id)
        name = lead.publisher or (src.name if src else lead.source_id)
        if row.get("text_from") == cluster.BRIEF:
            lines = cluster.brief_lines(store, ev)
            outlets = [n for n, _, _ in lines if n != cluster.HN_LINE]
            block = "\n".join(f"{n.upper()}: {t}" + (f" | {x}" if x else "") for n, t, x in lines)
            jobs.append((ev, "brief", dict(title=lead.title, lead=block, headline=row["headline"], s1=row["s1"],
                                           s2=_rest(row["summary"], row["s1"]), notif_title=row.get("notif_title"),
                                           notif_line=row.get("notif_line"), publisher=outlets[0] if outlets else "")))
        elif row.get("text_from") == "rewrite":
            hard = sorted((m for m in ev.members if m is not lead and m.hard and not m.thin and m.text),
                          key=lambda m: m.published_at, reverse=True)
            picked = [lead] + hard[:RECHECK_MEMBERS - 1]
            # The titles are part of what a rewrite may state (llm.rewrite_event).
            texts = "\n\n".join(f"{m.title}. {text_mod.cap_words(m.text, llm.REWRITE_WORDS)}" for m in picked)
            jobs.append((ev, "rewrite", dict(title=lead.title, lead=texts, headline=row["headline"], s1=row["s1"],
                                             s2=_rest(row["summary"], row["s1"]),
                                             notif_title=row.get("notif_title"), notif_line=row.get("notif_line"))))
        elif lead.status == "ok" and lead.text:
            w = store.db.one("SELECT s2, s3, summary, summary_short FROM writes WHERE url_canonical = ?", lead.url)
            jobs.append((ev, "card", dict(title=lead.title, lead=lead.text, headline=lead.headline, s1=lead.s1,
                                          s2=w["s2"] or "", s3=(w["s3"] or "") if w["summary"] != w["summary_short"]
                                          else "", notif_title=lead.notif_title, notif_line=lead.notif_line,
                                          publisher=name)))
    return jobs


def recheck(store, event_ids, check, dry_run=False):
    """Fact-checks the cards of `event_ids` with `check(**fields) -> entail.Entailment` and sends each one that
    fails back (see the module docstring). Returns [(event, kind, Entailment)] for the failures; a check that
    could not be made counts as passed."""
    jobs = recheck_plan(store, event_ids)

    def one(job):
        try:
            return check(**{k: v or "" for k, v in job[2].items()})
        except (llm.LLMError, ValueError):
            return None
    failed = []
    now = iso(store.now)
    db = store.db
    for (ev, kind, _), res in zip(jobs, _parallel(one, jobs)):
        if res is None or res.ok:
            continue
        failed.append((ev, kind, res))
        if dry_run:
            continue
        if kind == "card":
            url = ev.row["lead_url"]
            w = db.one("SELECT steps, model FROM writes WHERE url_canonical = ?", url)
            steps = json.loads(w["steps"] or "[]")
            steps.append(llm.Step(2, w["model"], "entail_fail", "entail_fail", [res.feedback()]).__dict__)
            # The stored answer and this feedback are what the writer's next run continues from (llm.prior_attempt).
            # A failed write keeps its classification and never its text (as db.save_write stores one).
            db.conn.execute("UPDATE writes SET status = 'failed', reason = 'entail_fail', steps = ?, headline = NULL,"
                            " summary = NULL, summary_short = NULL, notif_line = NULL, notif_title = NULL,"
                            " s1 = NULL, s2 = NULL, s3 = NULL WHERE url_canonical = ?", (json.dumps(steps), url))
            db.conn.execute("UPDATE items SET status = 'held', retry_at = ?, attempts = 0, hold_reason = 'recheck'"
                            " WHERE url_canonical = ?", (now, url))
        elif kind == "rewrite":
            db.conn.execute("UPDATE events SET text_from = NULL, rewrite_importance = NULL, rewrite_spice = NULL,"
                            " rewrite_reason = 'recheck' WHERE id = ?", (ev.id,))
        else:
            db.conn.execute("UPDATE events SET headline = NULL, summary = NULL, summary_short = NULL, s1 = NULL,"
                            " notif_line = NULL, notif_title = NULL, text_from = NULL, rewritten_at = NULL"
                            " WHERE id = ?", (ev.id,))
    return failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=",".join(PASSES), help="comma-separated passes: " + ", ".join(PASSES))
    ap.add_argument("--hours", type=float, default=72, help="events and cards published this long ago or less")
    ap.add_argument("--ask-dates", action="store_true", help="ask the writer for the news date of event leads")
    ap.add_argument("--ask-hours", type=float, default=36)
    ap.add_argument("--edition", help="edition.json whose cards `recheck` checks")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="print every ruling of the superseded pass")
    args = ap.parse_args()
    only = [p for p in args.only.split(",") if p]
    if set(only) - set(PASSES):
        sys.exit(f"unknown pass in --only; choose from {', '.join(PASSES)}")
    cfg = config.load()
    if os.path.realpath(os.path.expanduser(cfg["state_dir"])) == os.path.realpath(os.path.expanduser("~/.techshorts")):
        sys.exit("refusing to touch the old publisher's state; set TECHGIST_STATE")
    db = DB(config.state_path(cfg, "state.db"))
    db.conn.execute("PRAGMA busy_timeout = 120000")
    srcs = sources_mod.load()
    now = utcnow()
    since = now - timedelta(hours=args.hours)
    client = llm.make_router(cfg)
    judge = lambda title, text, events: llm.judge_events(client, cfg, title, text, events)  # noqa: E731
    store = cluster.Store(db, srcs, cluster.Settings.from_cfg(cfg), now, judge)
    tag = "would be " if args.dry_run else ""

    if "superseded" in only:
        rulings = [] if args.verbose else None
        got = superseded(store, since, args.dry_run, rulings)
        print(f"superseded: {len(got)} {tag}marked ({store.judge_calls} judge calls)")
        for old, new, rel in rulings or []:
            print(f"    {rel or 'no answer':10} {(new.anchor.title)[:70]:70} | {(old.row['headline'] or '')[:70]}")
        for old, new, rel in got:
            print(f"  {old.id} {old.row['headline']}\n    <- {new.id} [{rel}] {new.row.get('headline') or new.anchor.title}")
    if "dates" in only:
        ask = (lambda published, title, headline, text: llm.date_news(client, cfg, published, title, headline, text)) \
            if args.ask_dates else None
        got = dates(db, since, ask, now - timedelta(hours=args.ask_hours), args.dry_run)
        print(f"dates: {len(got)} cards {tag}dated by their news")
        for url, title, published, at, how in got:
            print(f"  {iso(published)} -> {iso(at)} [{how}] {title[:90]}")
    if "rewrites" in only:
        got = rewrites(store, since, args.dry_run)
        print(f"rewrites: {len(got)} events {tag}queued")
        for ev in got:
            print(f"  {ev.id} {ev.row.get('headline')}")
    if "recheck" in only:
        if not args.edition:
            print("recheck: skipped, no --edition")
        else:
            ids = [a["eventId"] for a in json.load(open(args.edition))["articles"] if a.get("eventId")]
            model = llm.role_model(cfg, "judge")
            got = recheck(store, ids, lambda **f: entail.check(client, model, **f), args.dry_run)
            print(f"recheck: {len(got)} of {len(ids)} cards {tag}sent back")
            for ev, kind, res in got:
                print(f"  {ev.id} [{kind}] {ev.row['headline']}\n    line: {res.line} | {res.unsupported}")
    if args.dry_run:
        db.conn.rollback()
    else:
        db.commit()
    calls = client.drain()
    kinds = {}
    for kind, _, outcome, ms, _, _ in calls:
        k = kinds.setdefault(kind, [0, 0])
        k[0] += 1
        k[1] += outcome != "ok"
    print("calls: " + (", ".join(f"{k} {n} ({bad} failed)" for k, (n, bad) in sorted(kinds.items())) or "none"))


if __name__ == "__main__":
    main()
