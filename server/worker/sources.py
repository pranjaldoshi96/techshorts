"""Reads sources.yaml (the single source list, spec section 2.2) into Source records."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from . import config

POLL_SECONDS = {"3m": 180, "10m": 600}
TIERS = {1, 2, "signal"}
FORMATS = {"rss", "hn_algolia", "lobsters"}
# The catch-all publisher for an article imported from a signal's link whose host no source lists; the card shows
# the host name (items.extra.publisher).
WEB_ID = "web"
REGIONS = {"in", "us", "global"}


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    v1_name: str
    url: str
    tier: object = 2            # 1, 2 or "signal"
    authority: float = 1.0
    reads: bool = False
    region: str = "global"
    poll: str = "10m"
    fetch_page: bool = True
    images: str = "source"
    include_tags: tuple = field(default_factory=tuple)
    # How the body is parsed: rss (RSS/Atom), hn_algolia (the HN search API's JSON) or lobsters (hottest.json).
    format: str = "rss"
    # What a signal source is, for the rules that differ: techmeme (an editor's pick that links to the article),
    # hn or lobsters (a link with points). Empty for news sources.
    kind: str = ""
    # Hosts whose articles are this publisher's, for articles imported from a signal's link (fetch.promote_signals).
    domains: tuple = field(default_factory=tuple)

    @property
    def poll_seconds(self):
        return POLL_SECONDS[self.poll]

    @property
    def polled(self):
        """False for a publisher that is listed only so linked articles carry its name and authority."""
        return bool(self.url)

    @property
    def is_signal(self):
        return self.tier == "signal"


@dataclass
class SourceList:
    sources: list
    domain_blocklist: list

    def by_id(self):
        return {s.id: s for s in self.sources}

    def blocked(self, url):
        host = (urlsplit(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.domain_blocklist)

    def for_host(self, url):
        """The news source that publishes `url`, by its `domains` (the longest match), or None."""
        host = (urlsplit(url).hostname or "").lower()
        best = None
        for s in self.sources:
            if s.is_signal:
                continue
            for d in s.domains:
                if (host == d or host.endswith("." + d)) and (best is None or len(d) > best[0]):
                    best = (len(d), s)
        return best[1] if best else None


def load(path=None):
    path = Path(path or os.environ.get("TECHGIST_SOURCES") or config.SOURCES_PATH)
    raw = yaml.safe_load(path.read_text())
    defaults = raw.get("defaults", {})
    out, seen = [], set()
    for entry in raw["sources"]:
        s = {**defaults, **entry}
        s["include_tags"] = tuple(s.get("include_tags") or ())
        s["domains"] = tuple(d.lower().lstrip(".") for d in s.get("domains") or ())
        s["url"] = s.get("url") or ""
        src = Source(**s)
        # Fail loudly on a bad edit: a typo here would silently drop a publisher.
        if src.id in seen:
            raise ValueError(f"duplicate source id {src.id}")
        if src.tier not in TIERS or src.region not in REGIONS or src.poll not in POLL_SECONDS:
            raise ValueError(f"bad tier/region/poll for {src.id}")
        if not 0.5 <= float(src.authority) <= 1.2 or src.images not in ("source", "generated"):
            raise ValueError(f"bad authority/images for {src.id}")
        if src.format not in FORMATS or (src.is_signal and not src.polled):
            raise ValueError(f"bad format/url for {src.id}")
        seen.add(src.id)
        out.append(src)
    blocklist = [d.lower().lstrip(".") for d in raw.get("domain_blocklist") or []]
    return SourceList(out, blocklist)


def effective_tier(source, check_ok):
    """Tier-1 applies only to sources that passed the last check_sources run (spec 2.2)."""
    # A publisher with no feed (named only for linked articles) has nothing to check.
    if source.tier == 1 and not check_ok and source.polled:
        return 2
    return source.tier
