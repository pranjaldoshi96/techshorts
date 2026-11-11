#!/usr/bin/env python3
"""WP3a calibration: labelled item pairs from the 2026-11-11 fixture, the precision/recall curve of embedding
cosine on them, and the clustering thresholds chosen from it (spec 2.9).

Labels come from tests/fixtures/events-2026-11-11.json, which groups the fixture items into real-world events by
hand. Same-event pairs are every pair inside a group. Different-event pairs are, in order: the spec's named
same-company pairs (IPO vs Glasswing / RAISE Act / Australia; Nvidia earnings vs the probe), every cross-event
pair sharing an org, the closest cross-event pairs by cosine (the hard negatives), then a seeded random sample.
Pairs touching a "related" item are left out both ways, and so are pairs published more than 48 h apart (the
join window: such items are never compared).

  python3 tools/label_pairs.py --data ~/.techshorts-r1/wp3/data.json --build     # writes tests/fixtures/pairs.jsonl
  python3 tools/label_pairs.py --data ~/.techshorts-r1/wp3/data.json --variants  # compares embedding inputs
  python3 tools/label_pairs.py --report [--write]   # PR table on the committed fixture; --write sets config.yaml

Thresholds (each the lowest cosine whose pairs at or above it, and at every higher cut, keep precision >= 0.97),
each measured on the rule it is used in:
  T_join   passage vs passage AND a shared non-org entity, which is the cosine+entity join of spec 2.9. Cosine
           alone needs about 0.525 for 0.97 (vendor posts like "ChatGPT Enterprise on AWS" vs "Businesses in
           ChatGPT" sit at 0.52) and then misses most of the Huang-probe pairs (0.43-0.51); the entity rule
           removes those look-alikes, so the joint rule reaches 0.97 lower down.
  T_signal query (a headline alone, as Techmeme/HN give it) vs passage, cosine alone (signals have no entities).
  T_cand   the candidate list in the write prompt, cosine alone (the LLM is shown candidates precisely when
           entities don't line up). Positives are "same event or related" pairs, so a candidate is a
           97%-precise "this is about the same story" hit; the join itself still needs T_join.
"""
import argparse
import itertools
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from datetime import timedelta  # noqa: E402

from worker import config, embed  # noqa: E402
from worker.db import from_iso  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures")
GOLD = os.path.join(FIX, "events-2026-11-11.json")
PAIRS = os.path.join(FIX, "pairs.jsonl")
ITEMS = os.path.join(FIX, "wp3_items.json")
PRECISION = 0.97
WINDOW_H = 48
# Synthetic items are placed relative to the edition's publish time.
EDITION_AT = from_iso(json.load(open(os.path.join(FIX, "edition-2026-11-11.json")))["publishedAt"])
N_HARD, N_RANDOM, SEED = 150, 60, 11


def key(row):
    return row.get("id") or row["url"]


def published(row):
    if row.get("published_at"):
        return from_iso(row["published_at"])
    return EDITION_AT + timedelta(hours=row["published_offset_h"])


def load_rows(data_path=None):
    """(rows, vectors by variant name). From the work data (all variants) or the committed fixture (one)."""
    if data_path:
        d = json.load(open(os.path.expanduser(data_path)))
        rows = d["rows"]
        vecs = {name: [np.asarray(v, dtype=np.float32) for v in vs] for name, vs in d["vecs"].items()}
        return rows, vecs
    d = json.load(open(ITEMS))
    rows = d["items"]
    return rows, {"passage": [embed.unpack_int8(r["emb"]) for r in rows],
                  "query": [embed.unpack_int8(r["emb_q"]) for r in rows]}


def gold():
    g = json.load(open(GOLD))
    event_of = {e["url"]: name for name, items in g["events"].items() for e in items}
    related = {e["url"]: name for name, items in g["related"].items() for e in items}
    return g, event_of, related


def _orgs(row):
    return {o.lower() for o in ((row.get("llm") or {}).get("entities") or {}).get("orgs") or []}


def build(rows, vecs):
    g, event_of, related = gold()
    idx = {key(r): i for i, r in enumerate(rows)}      # vectors are indexed like the full row list
    rows = [r for r in rows if r["kind"] in ("item", "hn_import", "synthetic", "signal")]
    by_key = {key(r): r for r in rows}
    # Hard negatives are the closest by any embedding variant, so every variant is tested on its own worst cases.
    names = [n for n in vecs if not n.startswith("q")]

    def cos_max(a, b):
        return max(float(np.dot(vecs[n][a], vecs[n][b])) for n in names)

    pairs, seen = [], set()

    def add(a, b, label, why):
        k = tuple(sorted((key(a), key(b))))
        if k in seen or k[0] == k[1]:
            return
        # Items published more than 48 h apart are never compared (an event older than that takes no joins),
        # so such pairs would only skew the curve with weekly-column look-alikes.
        if abs((published(a) - published(b)).total_seconds()) > WINDOW_H * 3600:
            return
        seen.add(k)
        pairs.append({"a": k[0], "b": k[1], "a_title": by_key[k[0]]["title"], "b_title": by_key[k[1]]["title"],
                      "label": label, "why": why})

    def group(name):
        return [by_key[e["url"]] for e in g["events"].get(name, []) if e["url"] in by_key]

    for name in g["events"]:
        for a, b in itertools.combinations(group(name), 2):
            add(a, b, "same", f"event:{name}")
    # Related pairs are labelled too (for T_cand) but never count as same or different for T_join.
    for name, items in g["related"].items():
        members = group(name) + [by_key[e["url"]] for e in items if e["url"] in by_key]
        for a, b in itertools.combinations(members, 2):
            if not (event_of.get(a["url"]) == name and event_of.get(b["url"]) == name):
                add(a, b, "related", f"related:{name}")
    for m in g["must_differ"]:
        left = group(m["a"])
        if "b" in m:
            right = group(m["b"])
        elif "b_url" in m:
            right = [by_key[m["b_url"]]] if m["b_url"] in by_key else []
        else:
            right = [by_key[m["b_synthetic"]]] if m["b_synthetic"] in by_key else []
        for a in left:
            for b in right:
                add(a, b, "different", f"named:{m['why']}")

    def different(a, b):
        ea, eb = event_of.get(a["url"]), event_of.get(b["url"])
        ra, rb = related.get(a["url"]), related.get(b["url"])
        if a["kind"] == "synthetic" or b["kind"] == "synthetic":
            return False
        if (ea and ea == eb) or (ra and ra in (rb, ea)) or (rb and rb in (ra, eb)):
            return False
        return True

    cross = [(a, b) for a, b in itertools.combinations(rows, 2) if different(a, b)]
    for a, b in cross:
        if _orgs(a) & _orgs(b) - {"ai"} and (event_of.get(a["url"]) or event_of.get(b["url"])):
            add(a, b, "different", "same_org")
    cross.sort(key=lambda p: -cos_max(idx[key(p[0])], idx[key(p[1])]))
    for a, b in cross[:N_HARD]:
        add(a, b, "different", "hard")
    rnd = random.Random(SEED)
    for a, b in rnd.sample(cross[N_HARD:], N_RANDOM):
        add(a, b, "different", "random")
    with open(PAIRS, "w") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    counts = {}
    for p in pairs:
        counts[p["label"]] = counts.get(p["label"], 0) + 1
    print(f"wrote {PAIRS}: {counts}")


def load_pairs():
    return [json.loads(l) for l in open(PAIRS) if l.strip()]


def scores(rows, vecs, pairs, pv, qv=None):
    """Cosine per pair. With `qv`, the query vector of one side against the passage of the other (the lower of the
    two directions, as a signal could be either item)."""
    idx = {key(r): i for i, r in enumerate(rows)}
    out = []
    for p in pairs:
        if p["a"] not in idx or p["b"] not in idx:
            continue
        a, b = idx[p["a"]], idx[p["b"]]
        if qv is None:
            s = float(np.dot(vecs[pv][a], vecs[pv][b]))
        else:
            s = min(float(np.dot(vecs[qv][a], vecs[pv][b])), float(np.dot(vecs[qv][b], vecs[pv][a])))
        out.append((s, p))
    return out


def curve(scored, positive=("same",), negative=("different",)):
    """(threshold, tp, fp, precision, recall) at each distinct score, high to low."""
    pts = [(s, p["label"]) for s, p in scored if p["label"] in positive + negative]
    pts.sort(key=lambda x: -x[0])
    n_pos = sum(lab in positive for _, lab in pts)
    out, tp, fp = [], 0, 0
    for s, lab in pts:
        tp += lab in positive
        fp += lab in negative
        out.append((s, tp, fp, tp / (tp + fp), tp / n_pos if n_pos else 0))
    return out


def choose(points, precision=PRECISION):
    """Lowest threshold such that every cut at or above it keeps precision >= target. Rounded up to 0.005 so the
    stored value sits on the safe side of the measured point."""
    best = None
    for s, tp, fp, p, r in points:
        if p < precision:
            break
        best = (s, p, r)
    if best is None:
        return None
    t = float(np.ceil(best[0] * 200) / 200)
    return t


def at(points, t):
    tp = sum(1 for s, *_ in points if s >= t)
    sel = [x for x in points if x[0] >= t]
    if not sel:
        return 0, 0, 0.0, 0.0
    s, tp, fp, p, r = sel[-1]
    return tp, fp, p, r


def gated(rows, scored):
    """Scores with the entity rule applied: a pair sharing no non-org entity can't join, whatever its cosine."""
    from worker import cluster
    keys = {key(r): cluster.entity_keys((r.get("llm") or {}).get("entities") or {}) for r in rows}
    return [(s if keys.get(p["a"], set()) & keys.get(p["b"], set()) else -1.0, p) for s, p in scored]


def thresholds(rows, vecs, pv="passage", qv="query"):
    """{name: (threshold, curve)} for T_join, T_signal and T_cand (see the module docstring)."""
    pairs = load_pairs()
    pp = scores(rows, vecs, pairs, pv)
    curves = {"t_join": curve(gated(rows, pp)), "t_signal": curve(scores(rows, vecs, pairs, pv, qv)),
              "t_cand": curve(pp, positive=("same", "related"))}
    return {k: (choose(c), c) for k, c in curves.items()}, pp


def _table(title, c, lo, hi):
    print(f"\n{title}")
    print("| cosine >= | same kept | different kept | precision | recall |")
    print("|---|---|---|---|---|")
    for t in np.arange(lo, hi, 0.025):
        tp, fp, p, r = at(c, t)
        print(f"| {t:.3f} | {tp} | {fp} | {p:.3f} | {r:.3f} |")


def report(rows, vecs, pv="passage", qv="query", write=False):
    pairs = load_pairs()
    counts = {}
    for p in pairs:
        counts[p["label"]] = counts.get(p["label"], 0) + 1
    print(f"Pairs: {counts}  (fixture {PAIRS})")
    th, pp = thresholds(rows, vecs, pv, qv)
    _table("Join rule: passage cosine AND a shared non-org entity (same vs different):", th["t_join"][1], 0.40, 0.81)
    _table("Cosine alone, passage vs passage (same vs different), for comparison:", curve(pp), 0.40, 0.81)
    _table("Candidates: cosine alone, same-or-related vs different:", th["t_cand"][1], 0.40, 0.81)
    _table("Signals: headline query vs passage (same vs different):", th["t_signal"][1], 0.30, 0.71)
    print()
    for name, (t, c) in th.items():
        tp, fp, p, r = at(c, t) if t is not None else (0, 0, 0, 0)
        print(f"T_{name[2:]} = {t}  (precision {p:.3f}, recall {r:.3f}, "
              f"{tp} positive / {fp} different pairs at or above)")
    t_join = th["t_join"][0]
    missed = [(s, p) for s, p in gated(rows, pp) if p["label"] == "same" and t_join is not None and s < t_join]
    if missed:
        print(f"\nSame-event pairs the join rule misses ({len(missed)}; -1 = no shared entity):")
        for s, p in sorted(missed, key=lambda x: -x[0])[:20]:
            print(f"  {s:.3f}  {p['a_title'][:55]!r} ~ {p['b_title'][:55]!r}")
    top_fp = sorted([(s, p) for s, p in gated(rows, pp) if p["label"] == "different"], key=lambda x: -x[0])[:8]
    print("\nClosest different-event pairs under the join rule:")
    for s, p in top_fp:
        print(f"  {s:.3f}  {p['a_title'][:55]!r} ~ {p['b_title'][:55]!r}  [{p['why']}]")
    values = {k: v[0] for k, v in th.items()}
    if write and None not in values.values():
        write_config(**values)
    return values


def variants(rows, vecs):
    """Compares embedding inputs: recall at precision >= 0.97 for each (lead words, dims)."""
    pairs = load_pairs()
    print("| variant | T at P>=0.97 | recall | AUC-ish (mean same - mean different) |")
    print("|---|---|---|---|")
    for name in sorted(n for n in vecs if n.startswith("p")):
        sc = scores(rows, vecs, pairs, name)
        c = curve(sc)
        t = choose(c)
        _, _, p, r = at(c, t) if t else (0, 0, 0, 0)
        same = np.mean([s for s, p_ in sc if p_["label"] == "same"])
        diff = np.mean([s for s, p_ in sc if p_["label"] == "different"])
        print(f"| {name} | {t} | {r:.3f} | {same - diff:.3f} |")


def write_config(**values):
    """Rewrites the numbers in config.yaml's cluster block in place, keeping its comments."""
    path = config.CONFIG_PATH
    text = open(path).read()
    for k, v in values.items():
        text, n = re.subn(rf"(?m)^(  {k}:)\s*[\d.]+", rf"\g<1> {v}", text)
        if n != 1:
            sys.exit(f"config.yaml has no single '  {k}:' line")
    open(path, "w").write(text)
    print(f"wrote {values} to {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="work data.json from build_wp3_fixture.py (all variants)")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--variants", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    rows, vecs = load_rows(args.data)
    if args.build:
        build(rows, vecs)
    if args.variants:
        variants(rows, vecs)
    if args.report:
        if args.data:
            cfg = config.load()
            d = cfg["embed"]
            report(rows, vecs, f"p{d['lead_words']}_{d['dims']}", f"q_{d['dims']}", args.write)
        else:
            report(rows, vecs, write=args.write)


if __name__ == "__main__":
    main()
