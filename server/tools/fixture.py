"""Loads the live 2026-11-11 v1 edition fixture (tests/fixtures/edition-2026-11-11.json) as worker items.

The fixture is the edition the 1.2 app decoded that morning, unmodified, so it doubles as a v1-schema sample.
Its `content` is the feed text the old publisher kept, which stands in for `feed_text`.
"""
import json
import os

from worker import prefilter, sources as sources_mod

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures",
                       "edition-2026-11-11.json")
# v1 names that aren't a sources.yaml v1_name. Product Hunt, DEV Community and Changelog were removed (spec 2.2).
V1_ALIASES = {"TechCrunch Startups": "techcrunch", "Hacker News": "hn"}


def load(path=FIXTURE, srcs=None):
    """Returns (items, srcs). Each item has the worker's item fields plus `v1` (the original article) and
    `eligible` (a non-signal, current source and kept by the prefilter)."""
    srcs = srcs or sources_mod.load()
    by_v1 = {}
    for s in srcs.sources:
        by_v1.setdefault(s.v1_name, s.id)
    by_v1.update(V1_ALIASES)
    by_id = srcs.by_id()
    edition = json.load(open(path))
    items = []
    for a in edition["articles"]:
        sid = by_v1.get(a["source"])
        src = by_id.get(sid)
        rule = prefilter.check(a["title"], a["url"])
        items.append({
            "url": a["url"], "url_canonical": a["url"], "source_id": sid, "title": a["title"],
            "feed_text": a.get("content") or "", "excerpt": a.get("excerpt") or "",
            "published_at": a["publishedAt"], "extra": "{}", "v1": a, "drop_rule": rule,
            "eligible": src is not None and not src.is_signal and rule is None,
        })
    return items, srcs
