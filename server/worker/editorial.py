"""The owner's editorial file (spec section 7): <state_dir>/editorial.yaml, reloaded every tick. `server/tg` edits it.

  pin:      [{event: ev_x, tab: top, until: "2026-11-12T09:00:00Z"}]   head of the tab (tab defaults to top)
  bury:     [ev_x]                      score x 0.1
  boost:    {ev_x: 1.5}                 score multiplier
  kill:     [ev_x]                      never published
  override: {ev_x: {headline: "...", summary: "..."}}   the owner's text replaces the AI text
  block:    ["DEV Community", "example.com/a-story"]    a source (id, name or v1 name) or part of a URL
  push:     []                          R3 (server push); read but unused until then

Event ids go through the alias table, so an id copied before a merge still works. A broken file is logged and
ignored (the last good one is not kept: an empty editorial is always safe).
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml

from .db import from_iso

log = logging.getLogger("techgist")
BURY = 0.1


@dataclass
class Editorial:
    pins: dict = field(default_factory=dict)        # event id -> set of tab ids
    boost: dict = field(default_factory=dict)       # event id -> multiplier
    kill: set = field(default_factory=set)
    override: dict = field(default_factory=dict)    # event id -> {headline, summary}
    block: list = field(default_factory=list)

    def blocked(self, source, url):
        """`source` is a sources.Source or None."""
        names = {source.id, source.name, source.v1_name} if source else set()
        return any(b in names or (b and b in (url or "")) for b in self.block)


def _when(v):
    # YAML turns an unquoted ISO time into a datetime; a quoted one stays a string.
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return from_iso(str(v).replace("+00:00", "Z"))


def path(cfg):
    return os.path.join(cfg["state_dir"], "editorial.yaml")


def load(cfg, now, resolve=lambda eid: eid):
    p = path(cfg)
    if not os.path.exists(p):
        return Editorial()
    try:
        raw = yaml.safe_load(open(p)) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("editorial.yaml ignored: %s", e)
        return Editorial()
    ed = Editorial()
    for pin in raw.get("pin") or []:
        if isinstance(pin, str):
            pin = {"event": pin}
        until = pin.get("until")
        if until and _when(until) < now:
            continue
        ed.pins.setdefault(resolve(pin["event"]), set()).add(pin.get("tab") or "top")
    for eid in raw.get("bury") or []:
        ed.boost[resolve(eid)] = BURY
    for eid, mult in (raw.get("boost") or {}).items():
        ed.boost[resolve(eid)] = float(mult)
    ed.kill = {resolve(e) for e in raw.get("kill") or []}
    ed.override = {resolve(e): v for e, v in (raw.get("override") or {}).items() if isinstance(v, dict)}
    ed.block = [str(b) for b in raw.get("block") or []]
    return ed
