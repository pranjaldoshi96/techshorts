#!/usr/bin/env python3
"""Builds the WP3 clustering fixture from the 2026-11-11 edition: each item's text, its LLM triage + write
output (type, importance, entities, headline...) and its embeddings, so clustering tests run with no network.

Runs on the box from server/ (it calls the NVIDIA API and fetches pages):
  python3 tools/build_wp3_fixture.py --work ~/.techshorts-r1/wp3            # LLM + embeddings (resumable)
  python3 tools/build_wp3_fixture.py --work ~/.techshorts-r1/wp3 --picks    # event picks given candidates
  python3 tools/build_wp3_fixture.py --work ~/.techshorts-r1/wp3 --picks --retry   # thin items' retry pick
  python3 tools/build_wp3_fixture.py --work ~/.techshorts-r1/wp3 --export   # writes tests/fixtures/wp3_items.json

The work dir keeps every embedding variant (title + 0/50/120/300 words, 2048 and 1024 dims) for
tools/label_pairs.py to compare; the exported fixture keeps only the configured one, as int8. Article text is not
exported (it is the publishers' copy); only its word count is.

HN items with >= 250 points become items from their linked page when it yields >= 300 words (spec 2.9 HN-only
import); the rest of HN stays a signal (title only).
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from tools import fixture  # noqa: E402
from worker import attribution, config, embed, llm, text  # noqa: E402

FIXTURE_OUT = os.path.join(os.path.dirname(fixture.FIXTURE), "wp3_items.json")
VARIANTS = [0, 50, 120, 300]
DIMS = [2048, 1024]
HN_IMPORT_POINTS, HN_IMPORT_WORDS = 250, 300

# Synthetic items for the AC that the fixture has no item for. Their LLM fields are set by hand (the test is about
# clustering, not the model); only their embeddings come from the API.
SYNTHETIC = [
    {"id": "syn_huang_resigns", "source_id": "cnbc", "title": "Nvidia CEO Jensen Huang resigns after spy probe",
     "url": "https://www.cnbc.com/2026/11/13/nvidia-ceo-jensen-huang-resigns.html",
     "published_offset_h": 50,
     "text": ("Nvidia chief executive Jensen Huang resigned on Friday, days after the chipmaker's board opened an "
              "investigation into his relationship with a woman US officials suspect of spying for China. The board "
              "said Huang's departure was effective immediately and that chief financial officer Colette Kress would "
              "serve as interim CEO while it searches for a successor. Huang co-founded Nvidia in 1993 and led it to "
              "become the world's most valuable company on the back of demand for its AI chips. In a statement Huang "
              "said he was stepping down to spare the company further distraction. The FBI is still looking into the "
              "spy allegations, according to people familiar with the matter. Nvidia shares fell 6% in early "
              "trading. Analysts said the leadership change adds uncertainty for customers and suppliers at a time "
              "when export rules for AI chips to China are being tightened."),
     "llm": {"type": "news", "is_news": True, "relevance": 3, "importance": 9, "breaking": True, "sensitive": True,
             "topics": ["ai", "bigtech", "hardware"], "region": "global", "material_update": True,
             "event": "new",
             "entities": {"orgs": ["Nvidia", "FBI"], "people": ["Jensen Huang", "Colette Kress"], "products": [],
                          "amounts": ["6%"]},
             "headline": "Nvidia CEO Jensen Huang resigns after spy probe",
             "s1": "Nvidia CEO Jensen Huang resigned on Friday after the board opened a probe into his ties to a "
                   "suspected Chinese spy."}},
    {"id": "syn_nvidia_earnings", "source_id": "cnbc", "title": "Nvidia beats on revenue as data center sales jump 60%",
     "url": "https://www.cnbc.com/2026/11/10/nvidia-earnings-q3.html", "published_offset_h": -6,
     "text": ("Nvidia reported third-quarter revenue of $81 billion on Tuesday, ahead of analyst estimates, as sales "
              "of its data center chips jumped 60% from a year earlier. The chipmaker guided fourth-quarter revenue "
              "to about $88 billion, above the $85 billion Wall Street expected. Gross margin was 74.2%. Demand from "
              "cloud providers and AI labs for Blackwell and Rubin systems continues to outstrip supply, the company "
              "said, and it expects supply constraints to ease in the first half of next year. Data center revenue "
              "was $72 billion. Gaming revenue rose 12%. Nvidia shares rose 4% in extended trading after the "
              "report. Executives said on the earnings call that orders for the next generation of systems are "
              "already booked well into next year, and that sovereign AI projects in Europe and the Middle East "
              "added to growth in the quarter."),
     "llm": {"type": "earnings", "is_news": True, "relevance": 3, "importance": 8, "breaking": False,
             "sensitive": False, "topics": ["ai", "hardware", "bigtech"], "region": "global",
             "material_update": False, "event": "new",
             "entities": {"orgs": ["Nvidia"], "people": [], "products": ["Blackwell", "Rubin"],
                          "amounts": ["$81 billion", "$88 billion", "60%"]},
             "headline": "Nvidia beats on revenue as data center sales jump 60%",
             "s1": "Nvidia reported third-quarter revenue of $81 billion, ahead of estimates, as data center chip "
                   "sales jumped 60%."}},
    {"id": "syn_gadget", "source_id": "engadget", "title": "Anker launches a $79 magnetic power bank with a screen",
     "url": "https://www.engadget.com/2026/11/10/anker-magnetic-power-bank-screen.html", "published_offset_h": -3,
     "text": ("Anker has launched a new magnetic power bank with a small color screen that shows remaining charge "
              "and charging speed. The 10,000mAh battery snaps onto the back of recent iPhones and Pixel phones with "
              "Qi2 magnets and charges at up to 25 watts wirelessly. A USB-C port delivers 30 watts to a second "
              "device. The power bank costs $79 and goes on sale this week in the US and Europe in black, white and "
              "blue. Anker says the screen also shows an estimate of how long a full recharge will take. The company "
              "has sold several magnetic batteries before, but this is the first with a display. It weighs 230 grams "
              "and comes with a braided USB-C cable. Anker says it supports pass-through charging, so the power bank "
              "and a phone can charge from one wall adapter overnight."),
     "llm": {"type": "launch", "is_news": True, "relevance": 2, "importance": 7, "breaking": False,
             "sensitive": False, "topics": ["consumer", "hardware"], "region": "global", "material_update": False,
             "event": "new",
             "entities": {"orgs": ["Anker"], "people": [], "products": ["Qi2"], "amounts": ["$79"]},
             "headline": "Anker launches a $79 magnetic power bank with a screen",
             "s1": "Anker launched a $79 magnetic power bank with a color screen that shows remaining charge."}},
]


def load_items(srcs):
    items, _ = fixture.load(srcs=srcs)
    out = []
    for idx, it in enumerate(items):
        it["idx"] = idx
        pts = it["v1"].get("points") or 0
        if it["eligible"]:
            it["kind"] = "item"
        elif it["source_id"] in ("hn",) and it["drop_rule"] is None and pts >= HN_IMPORT_POINTS:
            it["kind"] = "hn_import"
        elif it["source_id"] in ("hn",):
            it["kind"] = "signal"
        else:
            continue
        it["hn_points"] = pts
        out.append(it)
    return out


def get_text(item, src, pages, cfg, robots, session):
    page = pages.get(item["url"])
    if page is None:
        if not robots.allowed(item["url"]):
            page = ""
        else:
            try:
                r = session.get(item["url"], headers={"User-Agent": cfg["fetch"]["user_agent"]}, timeout=15)
                page = r.text if r.ok and "html" in r.headers.get("content-type", "") else ""
            except requests.RequestException:
                page = ""
        pages[item["url"]] = page
    fetch_page = src.fetch_page if src else True
    paras = text.extract_paragraphs(page) if (page and fetch_page) else []
    return text.choose(paras, item["feed_text"], text.og_description(page) if page else None)


def run(work, cfg, srcs):
    os.makedirs(work, exist_ok=True)
    pages_path = os.path.join(work, "pages.json")
    shared = os.path.expanduser("~/.techshorts-r1/verify/pages.json")
    pages = json.load(open(pages_path)) if os.path.exists(pages_path) else (
        json.load(open(shared)) if os.path.exists(shared) else {})
    out_path = os.path.join(work, "llm.jsonl")
    done = {json.loads(l)["url"] for l in open(out_path)} if os.path.exists(out_path) else set()
    items = load_items(srcs)
    by_id = srcs.by_id()
    session = requests.Session()
    robots = text.Robots(cfg["fetch"]["user_agent"], session)
    key = llm.api_key()
    client = llm.Client(cfg["llm"]["base_url"], key, llm.TokenBucket(cfg["llm"]["rate_per_min"], 60.0),
                        timeout=cfg["llm"]["timeout_s"])
    lock = threading.Lock()

    def job(item):
        src = by_id.get(item["source_id"])
        t = get_text(item, src, pages, cfg, robots, session)
        row = {"url": item["url"], "idx": item["idx"], "kind": item["kind"], "source_id": item["source_id"],
               "title": item["title"], "published_at": item["published_at"], "hn_points": item["hn_points"],
               "text": t.text, "text_words": t.words, "text_origin": t.origin}
        if item["kind"] == "signal" or (item["kind"] == "hn_import" and t.words < HN_IMPORT_WORDS):
            row["kind"] = "signal"
        else:
            name = src.name if item["kind"] == "item" else "Hacker News"
            attr = attribution.detect(item["url"], item["title"], None, item["source_id"])
            res = (llm.triage_thin(client, cfg, item, name, t, attr) if t.thin else
                   llm.write_item(client, cfg, item, name, t, attr))
            o = res.output
            row["status"] = res.status
            if o is not None:
                row["llm"] = {"type": o.type, "is_news": o.is_news, "relevance": o.relevance,
                              "importance": o.importance, "breaking": o.breaking, "sensitive": o.sensitive,
                              "topics": o.topics, "region": o.region, "material_update": o.material_update,
                              "event": o.event, "entities": o.entities.model_dump(), "headline": o.headline,
                              "s1": o.s1, "s2": o.s2, "s3": o.s3, "notif_line": o.notif_line,
                              "summary": res.full, "summary_short": res.short}
        with lock:
            with open(out_path, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{row.get('status', row['kind'])}] {item['title'][:70]}", flush=True)

    todo = [i for i in items if i["url"] not in done]
    print(f"{len(todo)} items to run", flush=True)
    with cf.ThreadPoolExecutor(cfg["llm"]["concurrency"]) as pool:
        list(pool.map(job, todo))
    json.dump(pages, open(pages_path, "w"))

    rows = [json.loads(l) for l in open(out_path)]
    for s in SYNTHETIC:
        rows.append({"url": s["url"], "idx": None, "kind": "synthetic", "id": s["id"], "source_id": s["source_id"],
                     "title": s["title"], "published_offset_h": s["published_offset_h"], "text": s["text"],
                     "text_words": len(s["text"].split()), "text_origin": "synthetic", "status": "ok",
                     "llm": s["llm"], "hn_points": 0})
    jobs = [(f"p{n}_{d}", d, [embed.item_input(r["title"], r["text"], n) for r in rows], "passage")
            for d in DIMS for n in VARIANTS] + [(f"q_{d}", d, [r["title"] for r in rows], "query") for d in DIMS]
    calls = []

    def embed_job(job):
        name, dims, inputs, input_type = job
        emb = embed.Embedder(cfg["llm"]["base_url"], key, cfg["embed"]["model"], dims)
        out = [v.tolist() for v in emb.embed(inputs, input_type)]
        calls.append(emb.calls)
        print(f"embedded {name} ({emb.calls} calls)", flush=True)
        return name, out

    with cf.ThreadPoolExecutor(3) as pool:
        vecs = dict(pool.map(embed_job, jobs))
    print(f"{sum(calls)} embedding calls")
    json.dump({"rows": rows, "vecs": vecs}, open(os.path.join(work, "data.json"), "w"))
    print(f"wrote {os.path.join(work, 'data.json')}: {len(rows)} rows")


def picks(work, cfg, srcs, retry=False):
    """The write call's event pick (spec 2.6), which the first run didn't have candidates for. Offline there are no
    events yet, so each item is shown its nearest other fixture items (cosine >= T_cand, within 48 h, at most 5) as
    candidate events, with the id the item would give its event. The replay maps a pick to wherever that item is
    at the time; a pick of an item that hasn't arrived yet counts as "new"."""
    import hashlib
    import numpy as np
    from datetime import timedelta
    from worker.db import from_iso
    data = json.load(open(os.path.join(work, "data.json")))
    rows = data["rows"]
    vecs = [np.asarray(v, dtype=np.float32) for v in data["vecs"][f"p{cfg['embed']['lead_words']}_{cfg['embed']['dims']}"]]
    by_id = srcs.by_id()
    key = llm.api_key()
    client = llm.Client(cfg["llm"]["base_url"], key, llm.TokenBucket(cfg["llm"]["rate_per_min"], 60.0),
                        timeout=cfg["llm"]["timeout_s"])
    pool_rows = [i for i, r in enumerate(rows) if r.get("llm") and r["kind"] in ("item", "hn_import")]
    # --retry: a second, independent pick for thin items, used when the replay retries an unassigned thin item
    # (llm.write_pending re-asks those, since their event may not have existed at the first triage).
    out_path = os.path.join(work, "picks_retry.json" if retry else "picks.json")
    out = json.load(open(out_path)) if os.path.exists(out_path) else {}
    s = cfg["cluster"]

    def job(i):
        r = rows[i]
        pub = from_iso(r["published_at"])
        cands = []
        for j in pool_rows:
            o = rows[j]
            if j == i or abs(from_iso(o["published_at"]) - pub) > timedelta(hours=48):
                continue
            cos = float(np.dot(vecs[i], vecs[j]))
            if cos >= s["t_cand"]:
                cands.append((cos, o))
        cands = [o for _, o in sorted(cands, key=lambda x: -x[0])[:5]]
        if not cands:
            return r["url"], None
        ids = {"ev_" + hashlib.sha1(o["url"].encode()).hexdigest()[:10]: o["url"] for o in cands}
        cand = [{"id": eid, "headline": rows_by_url[u]["llm"].get("headline") or rows_by_url[u]["title"],
                 "s1": rows_by_url[u]["llm"].get("s1") or ""} for eid, u in ids.items()]
        src = by_id.get(r["source_id"])
        item = {"url": r["url"], "title": r["title"], "published_at": r["published_at"]}
        t = text.ArticleText(r["text"] or r["title"], r["text_origin"])
        attr = attribution.detect(r["url"], r["title"], None, r["source_id"])
        try:
            o = llm.parse(client.chat(cfg["llm"]["model"], llm.build_messages(
                item, src.name if src else "Hacker News", t.text, attr, cand), kind="triage"))
        except (llm.LLMError, llm.BadOutput) as e:
            print(f"pick failed for {r['title'][:50]}: {e}", flush=True)
            return r["url"], {"error": str(e)}
        kind, eid = (o.event.split(":", 1) if ":" in o.event else ("join", o.event))
        res = {"raw": o.event, "material_update": o.material_update, "candidates": list(ids.values()),
               "kind": kind if eid in ids else None, "url": ids.get(eid)}
        print(f"{r['title'][:50]!r} -> {o.event} {rows_by_url.get(ids.get(eid), {}).get('title', '')[:50]!r}",
              flush=True)
        return r["url"], res

    rows_by_url = {r["url"]: r for r in rows}
    todo = [i for i in pool_rows if rows[i]["url"] not in out and (not retry or rows[i].get("status") == "thin")]
    with cf.ThreadPoolExecutor(cfg["llm"]["concurrency"]) as pool:
        for u, res in pool.map(job, todo):
            out[u] = res
    json.dump(out, open(out_path, "w"), indent=1)
    print(f"wrote {out_path}: {sum(1 for v in out.values() if v and v.get('url'))} picks of {len(out)} items")


def export(work, cfg):
    """The committed fixture: no article text, one passage + one query vector per row, int8."""
    data = json.load(open(os.path.join(work, "data.json")))
    lead, dims = cfg["embed"]["lead_words"], cfg["embed"]["dims"]
    p, q = data["vecs"][f"p{lead}_{dims}"], data["vecs"][f"q_{dims}"]
    def load(name):
        path = os.path.join(work, name)
        return json.load(open(path)) if os.path.exists(path) else {}
    picked, retried = load("picks.json"), load("picks_retry.json")
    out = []
    for r, pv, qv in zip(data["rows"], p, q):
        row = {k: v for k, v in r.items() if k != "text"}
        pk = picked.get(r["url"])
        if pk and pk.get("url"):
            row["pick"] = {"kind": pk["kind"], "url": pk["url"], "material_update": pk["material_update"],
                           "candidates": pk["candidates"]}
        pk = retried.get(r["url"])
        if pk and pk.get("url"):
            row["pick_retry"] = {"kind": pk["kind"], "url": pk["url"], "material_update": pk["material_update"],
                                 "candidates": pk["candidates"]}
        row["emb"] = embed.pack_int8(pv)
        row["emb_q"] = embed.pack_int8(qv)
        out.append(row)
    meta = {"model": cfg["embed"]["model"], "dims": dims, "lead_words": lead,
            "note": "Built by tools/build_wp3_fixture.py from edition-2026-11-11.json; LLM fields from "
                    + cfg["llm"]["model"]}
    with open(FIXTURE_OUT, "w") as f:
        json.dump({"meta": meta, "items": out}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {FIXTURE_OUT}: {len(out)} rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--picks", action="store_true", help="record the LLM's event pick with candidates")
    ap.add_argument("--retry", action="store_true", help="with --picks: a second pick for thin items")
    args = ap.parse_args()
    cfg = config.load()
    work = os.path.expanduser(args.work)
    if args.export:
        export(work, cfg)
    elif args.picks:
        from worker import sources as sources_mod
        picks(work, cfg, sources_mod.load(), args.retry)
    else:
        from worker import sources as sources_mod
        run(work, cfg, sources_mod.load())


if __name__ == "__main__":
    main()
