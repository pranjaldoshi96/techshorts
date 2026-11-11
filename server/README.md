# Techgist server

The app reads a ranked **edition** that this server publishes, instead of every phone fetching the feeds itself.
The 1.2 app reads `edition.json` from the repo's `edition` branch; if it is more than 8 hours old, or can't be
downloaded or decoded, the app fetches the feeds itself until a fresh one appears.

```
server                                   GitHub (techshorts repo)            iPhone (1.2)
worker / publish.sh ──► edition branch ──► raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json
                                                                │ fresh (< 8 h) and decodable?  yes → show it
                                                                └────────────────────────────── no  → fetch feeds directly
```

The server is on a private network, so phones never connect to it. It only pushes finished files to GitHub, which
serves them.

There are two publishers in this directory. Only one owns the `edition` branch at a time.

| | Live today | R1 worker (`worker/`, built, not cut over) |
|---|---|---|
| What | `publish.sh`: the app's own Swift feed and ranking code (`main.swift`) in Docker, then `enrich.py` for LLM headlines and images | Python worker: sources, filtering, LLM writing and validation, clustering into events, ranking, tabs, images (docs/V2_SPEC.md sections 2, 3, 7) |
| Cadence | cron **every 30 min** (`*/30 * * * *`, plus `@reboot`) | a 60 s tick; sources polled every 3 or 10 min; `edition.json` rewritten when it changes and at least every 5 min; pushed to GitHub at most every 10 min |
| State | `~/.techshorts/` (`site/`, `enriched.json`, `publish.log`) | `~/.techshorts/state.db` (SQLite), `editorial.yaml`, `health.json`; the published directory is `~/.techshorts/edition/` |

At cutover (WP5) the worker takes over the branch and `publish.sh`/`enrich.py` retire. `main.swift` stays: it
becomes the safe-mode publisher (spec 6.4), which isn't built yet (see "Not built yet").

## The R1 worker

Each tick:

```
fetch (conditional GET) → prefilter → timestamps → article text → embed → LLM triage + write → trim → validate
→ entailment gate → cluster into events → event importance + clamps → score → tabs → images → v1 edition.json
→ 1.2-decoder check → publish → health.json
```

- **One card per event.** Reports of the same story cluster into one event (`cluster.py`); its card is the lead
  article's validated headline and summary, or a rewrite from several members.
- **Ranking** (`rank.py`, spec 2.11): `score = I × C × A × F` (importance, coverage, authority, freshness). Freshness
  decays from the publisher's own timestamp, so an old post re-fetched after downtime never looks new.
- **Tabs** (spec 2.12): Top, AI, Startups, India, Dev and Reads, each with its own types, minimum importance, window
  and cap; at most 2 cards from one publisher in the first 10 (4 per tab); never padded. A duplicate guard drops
  a card whose headline embedding is within the clustering threshold (`T_join`) of a higher-ranked card's, and
  a repeated picture in a tab's top 10.
- **v1 output** (`publish_v1.py`, spec 2.13): one article per event, in the exact shape the 1.2 app decodes.
  Categories are 1.2's five (`Top`, `AI`, `Startups`, `Dev`, `Blogs`); Reads files under Blogs, India under
  Startups (funding/acquisition) or Top. Top is "every article with a `topRank`" in 1.2.
- **Checked before publishing** (`tools/check_edition.py`): the 1.2 app's own decoder (frozen copies of
  `Article.swift` and `Edition.swift` in `tools/v1decode/`) decodes every edition before it replaces the last one.
  The decoder is built once into `<state_dir>/bin/v1decode`, with `swiftc` on a Mac or in the `swift:6.2-jammy`
  Docker image on the box (static stdlib, so it then runs without Docker). If the check fails, the previous
  edition stays and an alert fires.
- **Images** (`images.py`, spec 3): og:image, then feed media; pictures under 600 px wide, outside a 1.2–2.2
  aspect or under 8 KB are rejected. Each becomes `img/<hash>_std.jpg` (720×450 progressive JPEG, what 1.2 shows)
  and `img/<hash>_thumb.webp` (360×225). Otherwise a generated topic gradient. Files are kept 14 days.
- **Degraded mode** (`degraded.py`, spec 2.14): if the LLM is out (401/403, a 429 storm, or under 50% of items
  passing for 30 min) the edition still publishes. Validated text is reused; events with 2 or more publishers,
  or a tier-1 lead, show the publisher's title and a 55-word excerpt with `aiText: false` (1.2 then shows no AI
  label unless its on-device summaries are on); new tier-1 items publish alone; ranking fixes importance at 6. A tiny probe call every 10 min notices
  the key working again. Outside an outage, an event whose AI text failed still publishes this way when it has 2+
  publishers or a tier-1 lead.

### Running it

From `server/`, with Python 3.10+:

```bash
pip install --user -r requirements.txt
python3 -m pytest -q tests                                  # unit tests, no network

# Beside the live publisher, always with a separate state dir:
export TECHGIST_STATE=~/.techshorts-r1
python3 tools/check_sources.py                              # every feed must return 200 with items
python3 -m worker.main --once --out ~/.techshorts-r1/edition   # one tick, publishing into that directory
python3 -m worker.main --out ~/.techshorts-r1/edition          # loop: a tick every 60 s
python3 tools/check_edition.py ~/.techshorts-r1/edition/edition.json   # the 1.2 decoder check, by hand
```

`--out` defaults to `publish.out_dir`, else `<state_dir>/edition`. Nothing is pushed to GitHub unless
`publish.git_remote` is set in `config.yaml`; it is unset until cutover, so a test run can't touch the live branch.
The NVIDIA key comes from `NVIDIA_API_KEY`, else the last `export NVIDIA_API_KEY=` line in `~/.bashrc`. Without
a key the worker still publishes, in degraded mode.

### Owner controls: `server/tg`

`tg` edits `<state_dir>/editorial.yaml`, which the worker re-reads every tick, so changes show within a minute.

```bash
./tg status                  # edition age, LLM state, alerts, and the Top list with event ids
./tg find anthropic ipo      # look up an event id
./tg pin ev_3fa91c0b2d --hours 6        # head of Top for 6 h (--tab ai|startups|india|dev|reads)
./tg bury ev_…  /  ./tg boost ev_… 1.5  /  ./tg kill ev_…
./tg override ev_… --headline "Anthropic files for a record IPO" --summary "…"
./tg block "Engadget"        # a source (id, name or v1 name) or part of a URL
./tg clear ev_…              # undo everything for that id or block entry
```

Ids that were merged into another event are followed automatically. `tg push` is recorded but does nothing until
R3. Set `TECHGIST_STATE` for the test copy, as for the worker. (`editorial.json` with own pinned stories belongs
to `publish.sh` and is not read by the worker.)

### Checking on it

- `<state_dir>/health.json` every tick, and a copy next to `edition.json` on every publish:
  - **edition**: `editionAgeMin`, Top count, degraded state, rejected publishes; the last publish's per-tab
    counts, median age and most cards per publisher;
  - **freshness**: publisher→app lag p50/p95 (`event_published_at` → first edition);
  - **pipeline**: per-source status and check results, prefilter drops by rule, LLM calls/latency/429s,
    validation pass rate and skip reasons;
  - **content**: events, raw vs clamped importance histogram.
- **Alerts** (ntfy, at most once per condition per 6 h; logged as `ALERT …` when `NTFY_TOPIC` is unset): no
  publish for 20 min, Top under 12, validation pass rate under 80% over 1 h, more than 20 LLM 429s an hour or any
  401/403, the 1.2 decoder check failing, a source failing 6 polls in a row, more than 10% of raw importance ≥ 8,
  and the systemd unit failing (`techgist-alert@.service`).

### Cutover (WP5, owner)

1. Comment out the two `publish.sh` cron lines (`crontab -e`).
2. Put `NVIDIA_API_KEY=…` and `NTFY_TOPIC=…` in `~/.techshorts/env` (`chmod 600`).
3. In `config.yaml` set `publish.git_remote: git@github.com:pranjaldoshi96/techshorts.git` (it uses the existing
   deploy key `~/.ssh/techshorts_deploy`).
4. `cp techgist-worker.service techgist-alert@.service ~/.config/systemd/user/ && loginctl enable-linger $USER &&
   systemctl --user daemon-reload && systemctl --user enable --now techgist-worker`
5. `journalctl --user -u techgist-worker -f` and `./tg status`.

To roll back: `systemctl --user disable --now techgist-worker` and restore the cron lines.

### Not built yet

- Safe mode (`.github/workflows/safe-mode.yml`, spec 2.14): a GitHub Action that publishes a degraded edition from
  `main.swift` when the published `health.json` is over 30 min old. **TODO**, so a dead box is today noticed only
  through the systemd `OnFailure` alert and the app's own 8 h fallback.
- The v2 edition, edge, `hero` image variant and push (R2/R3), the daily owner digest, HN-only and Techmeme-only
  imports.

## The live publisher (`publish.sh`), until cutover

Cron runs `publish.sh` every 30 minutes:

```
*/30 * * * * $HOME/TechShorts/server/publish.sh --log
@reboot sleep 120 && $HOME/TechShorts/server/publish.sh --log
```

It builds `main.swift` with the app's own feed and ranking code in Docker (`swift:6.2-jammy`), so ranking,
duplicate merging and filtering match the app, then `enrich.py` adds LLM headlines and summaries and 720 px
images, and the result is force-pushed as a single amended commit to the `edition` branch with the deploy key
`~/.ssh/techshorts_deploy` (write access to this repo only). It also counts one Reddit request for the day's top
posts as extra coverage. Own stories and blocks go in `~/.techshorts/editorial.json` (see
`editorial.example.json`).

```bash
tail -20 ~/.techshorts/publish.log
curl -s https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json | head -c 200
```

The app's **Settings** screen shows where the stories came from: "TechShorts edition, 2 hours ago" or "Direct
from publishers".
