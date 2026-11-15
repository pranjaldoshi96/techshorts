#!/usr/bin/env python3
"""Re-picks the card text of live events whose s1 restates the headline, from the versions the model already wrote.

validate.select() now checks repetition against the headline it actually picks, and new writes are gated by
validate.repeat_problems, but cards validated before the rule keep an s1 that repeats the headline ("Karnataka HC
rejects Razorpay directors' SFIO case plea" / s1 the same sentence). Each such event's lead write is re-selected
from its stored output (headline, headline_alt/short, s1/s1_alt/s1_long...), checked with validate.check against
its stored text and, when the headline or s1 changed and entailment applies, by the entailment judge. Only a
passing card with no repetition replaces the stored one; rewritten event text (text_from = rewrite) is left alone.

Run from server/ against a test copy's state only:
  TECHGIST_STATE=~/.techshorts-r1 python3 -m tools.reselect [--days 7] [--dry-run]
"""
import argparse
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import attribution as attribution_mod, config, entail, llm, sources as sources_mod, validate  # noqa: E402
from worker.db import DB, iso, utcnow  # noqa: E402
from worker.text import ArticleText  # noqa: E402

SQL = """
SELECT e.id, e.headline AS ev_headline, e.s1 AS ev_s1, e.text_from, w.url_canonical, w.output, w.text, w.headline,
       w.s1, w.s2, w.s3, i.url, i.title, i.source_id, i.extra
FROM events e JOIN writes w ON w.url_canonical = e.lead_url JOIN items i ON i.url_canonical = e.lead_url
WHERE e.status = 'live' AND e.published_at >= ? AND w.status = 'ok' AND w.output IS NOT NULL
"""


def reselect(row, srcs, judge=None):
    """(new output, verdict) when a better card passes, else (None, reason)."""
    if row["text_from"] == "rewrite" or not validate.repeat_problems(row["ev_headline"], row["ev_s1"], row["s2"],
                                                                      row["s3"]):
        return None, "fine"
    out = llm.WriteOutput.model_validate_json(row["output"])
    author = json.loads(row["extra"] or "{}").get("author")
    attr = attribution_mod.detect(row["url"], row["title"], author, row["source_id"])
    text = ArticleText(row["text"] or "", "stored")
    attribution_mod.apply(out, attr, row["url"], row["title"])
    validate.select(out, attr, hedge_source=validate.hedge_scope(row["title"], text.lead() or text.text))
    if out.headline == row["headline"] and out.s1 == row["s1"]:
        return None, "no_better_version"
    src = srcs.by_id().get(row["source_id"])
    v = validate.check(out, text.text, row["title"], src.name if src else row["source_id"], attr, author,
                       text.lead())
    if not v.ok:
        return None, v.reason
    if entail.needed(out):
        if judge is None:
            return None, "needs_entailment"
        e = judge(row["title"], entail.context(out, text, attr), out.headline, out.s1)
        if not e.ok:
            return None, "entail_fail"
    return (out, v), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = config.load()
    if os.path.realpath(os.path.expanduser(cfg["state_dir"])) == os.path.realpath(os.path.expanduser("~/.techshorts")):
        sys.exit("refusing to touch the live state; set TECHGIST_STATE to a test copy")
    db = DB(config.state_path(cfg, "state.db"))
    db.conn.execute("PRAGMA busy_timeout = 120000")
    srcs = sources_mod.load()
    now = utcnow()
    client = llm.Client(cfg["llm"]["base_url"], llm.api_key(), llm.TokenBucket(rate=10, per=60.0),
                        timeout=cfg["llm"]["timeout_s"])

    def judge(title, lead, headline, s1):
        return entail.check(client, cfg["llm"]["model"], title, lead, headline, s1)

    counts = {}
    for row in db.q(SQL, iso(now - timedelta(days=args.days))):
        try:
            res, why = reselect(row, srcs, judge)
        except (llm.LLMError, ValueError) as e:
            res, why = None, f"error:{type(e).__name__}"
        counts[why or "replaced"] = counts.get(why or "replaced", 0) + 1
        if res is None:
            if why not in ("fine",):
                print(f"keep {row['id']} ({why}): {row['ev_headline'][:60]}")
            continue
        out, v = res
        print(f"new  {row['id']}: {out.headline[:60]} | {out.s1[:80]}")
        if args.dry_run:
            continue
        db.conn.execute("UPDATE writes SET headline = ?, s1 = ?, s2 = ?, s3 = ?, summary = ?, summary_short = ?,"
                        " output = ? WHERE url_canonical = ?",
                        (out.headline, out.s1, out.s2, out.s3, v.full, v.short, out.model_dump_json(),
                         row["url_canonical"]))
        db.conn.execute("UPDATE events SET headline = ?, s1 = ?, summary = ?, summary_short = ? WHERE id = ?",
                        (out.headline, out.s1, v.full, v.short, row["id"]))
        db.commit()
    print(counts)


if __name__ == "__main__":
    main()
