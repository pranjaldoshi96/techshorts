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
fetch (conditional GET) → strong signals imported as items → prefilter → timestamps → screening call → article text
→ embed → LLM write (card, importance, spice, push alert) → trim → validate → fact-check of every card line
→ cluster into events by development (judge) → signals → briefs for paywalled scoops → event importance + clamps
and traction floors → score → tabs → images → v1 edition.json + notifications.json → 1.2-decoder check → publish
→ health.json
```

- **Scope and spice** (owner's direction, 2026-11-15): anything with a tech angle that makes a good headline, not
  only product and funding news. `sources.yaml` lists about 135 feeds (trade and business press, leak- and
  gossip-driven outlets, security, gaming, crypto, space, EVs, regulators, India, startups, developers). One
  screening call per tick (`llm.triage_batch`) drops what has no tech angle before it costs a write. The writer
  scores **spice** 1-10 (how much a tech-curious reader would want to tap and forward the story) next to
  importance; `rank.py` multiplies the score by it, lets a juicy story into a tab up to two importance points under
  the tab's minimum, and keeps dry items out of Top.
- **Push alerts**: every written card carries `notification: {title, body}` (title at most 40 characters, body a
  hook of at most 110) and `spice` in `edition.json`; `notifications.json` beside it ranks the fresh cards for
  pushing by spice² x importance x traction x recency (`rank.notification_score`), with a lift for news under 2 h
  old. Only cards of spice 7 or more are candidates; commentary needs importance 8, and a site imported from a
  Hacker News link needs a second outlet. A title that is only a name ("Gary Marcus") gives way to the card's
  headline or its opening clause; with no usable title the card has no alert. The hook passes the same checks as
  the headline. Sending is the next app release's job.
- **Paywalled scoops** (`cluster.thin_can_seed`, `cluster.brief_due`, `llm.brief_event`): Bloomberg, the FT, the
  WSJ and the NYT give a title and a sentence. Such an item from an outlet that breaks news (authority 1.1 or
  more), or one Techmeme picked, starts an event of its own. Once the event has traction (a Techmeme pick, a
  second publisher, Hacker News at 150 or more, or the outlet's own report rated 7 or more) it gets a **brief**: a
  one- or two-sentence card written only from the lines available (the outlets' titles and feed sentences and
  Techmeme's summary), which must name whose reporting it is ("FT: ...", "Report: ...") and is fact-checked
  against those lines. The brief links to the best-placed outlet and is replaced by a full card as soon as an
  open outlet's report of the story validates. An event led by a thin item never ships as a raw title.
- **Short texts**: a card from under 150 words of text (a paywall's opening, a blurb) is two sentences, and the
  fact-check rejects a sentence that is scene-setting rather than a fact about the story.
- **Punchy and true**: `validate.py` rejects a headline or alert that states as done what the source title says
  is still to come ("Sebi to issue" must not become "Sebi issues"), an inference or superlative the source doesn't
  contain ("may have contributed to", "record"), and a card that drops the source's unnamed sourcing. A second
  model then fact-checks every line of the card (headline, three sentences, alert) against the whole text
  (`entail.py`). A card that fails is retried with the reason, then held, never shipped. The tense rule holds for every verb the
  title or text describes as still to come ("expected to price" must not become "priced"), in any capitalization.
  A short text that does not itself report its title's news (a paywall's scene-setting opening) is classified,
  not written (`validate.teaser_text`).
- **Models** (`config.yaml` `llm`): cards are written by GPT and fact-checked by Gemini on the inference hub;
  screening and the clustering judge use the smaller Gemini; the NVIDIA endpoint serves embeddings and the backup
  chat models (`llm.backup_models`, the large Nemotron first), which take over call by call when the hub fails
  (`llm.Router`). Two timeouts open a provider's circuit for 5 minutes, so an outage costs one timeout, not one
  per tick. Every call a backup answers is logged as a failover: `health.json` `llmBackup` and the `llm_backup`
  alert report the outage, a card that fails on a backup waits for the primary without using up its retries, and
  when the backup's cards fail more than they pass the edition is published degraded (`degraded.backup_state`).
  `tools/eval_cards.py STATE_DB --recent 30 --without hub --min-pass 0.6` replays cards with the hub taken away
  and fails when the backups pass fewer than 60%.
- **Embeddings** have no second provider (the clustering thresholds are calibrated for the one model). When the
  endpoint fails, written items get a placeholder vector and cluster by title words (`cluster.embed_fallback`),
  so a card still ships; real vectors replace the placeholders when it answers, and `embeddings_down` alerts
  after 5 failed ticks.
- **Signals**: Techmeme, Mediagazer, Hacker News (Algolia API), Lobsters and Reddit (the top posts of the day in
  r/technology and r/programming, one RSS request). A signal attaches to the event it is about (by URL, or by
  title when the judge agrees; a Reddit post only through the article it links) and adds to its traction, each
  platform with its own capped multiplier; points are refreshed on every poll. Reddit's feed has no score but is
  sorted by it, so a post's place is kept as its points and only the day's first ten count. A Techmeme pick, or an HN/Lobsters link past a points bar,
  whose article no source carries is imported and written like any item (`fetch.promote_signals`); when such an
  import cannot be written (a company page, a paywall) its points still attach to the event its title matches.
  A Techmeme pick also overrules a feed's category filter and the screening call's "off-topic" or "dull", and so
  does a Hacker News or Lobsters link past the import bar (`fetch.revive_shared`); when the screening prompt
  changes, the last 36 h of its drops are screened again, at most 4 batches of 40 a tick: an item due a
  screening is never written before it has had one. An item whose text could not be fetched stays
  clusterable for 48 h, and one that is itself a big Hacker News or Lobsters link (300+ / 100+) may start an
  event and get a brief, like a paywalled scoop.
- **When the news happened** (`newsdate.py`): a late write-up of older news ("Apple began selling its HomeView
  Friday", published on Sunday) is dated by the news: the writer's `news_date` when the text carries that day,
  else a plain statement in the first sentence. Only a report is dated this way: an exclusive, or a new
  development about an earlier incident, is its own news, and analysis, opinion and explainers keep their own
  date. A vague "last week" carries a date only in the article's opening and only for a day in that stretch. The event's clock, the card's date, its freshness and the 12 h
  alert window follow. The page's own publication time moves an item back when the feed carried a later one or
  none; a lead with no publisher's date at all is never an alert candidate.
- **Overtaken cards**: the judge may rule that a report *supersedes* a card (the outcome of what it reports as
  under way, a correction, a newer official account), and a follow-up whose title reports a reversal of its
  parent's does so by rule; the older event is left out while the newer one has a written card. Inside one event,
  a member a later member has overtaken ("says he will sign" once others report "signs") no longer leads, and
  the rewrite is given each report's time and told that the newest account stands. Its headline stays on the
  development the reports share: a detail only one report adds goes in the sentences (a how-to two days after
  a launch had turned the launch card into its side angle). The card may name the outlets whose reports it was
  given, and the fact-checker sees each report under its outlet.

- **One card per development.** Reports of the same development cluster into one event (`cluster.py`); its card
  is the lead article's validated headline and summary, or a rewrite from several members. A judge call rules
  whether a new item is the same development as a nearby event, a follow-up of its story (its own card, grouped
  with the story so a tab shows at most 3 of them) or something else: cosine plus a shared name chained every
  report of a running saga into one stale card.
- **Ranking** (`rank.py`, spec 2.11): `score = I × S × C × A × F` (importance, spice, coverage, authority, freshness). Freshness
  decays from the publisher's own timestamp, so an old post re-fetched after downtime never looks new; a
  well-covered story has a slower tail (`rank.STAY_TAIL`), so a 9-publisher story is still up a day later while
  Top's first card still changes within a day.
- **Importance** (`importance.py`): the writer's score, capped unless coverage backs it, and floored by coverage:
  5 publishers (or Techmeme plus 3) make a hard-news story at least a 7, 3 (or Techmeme plus 2) a 6, and Hacker
  News at 300/600 points a 5/6.
- **Tabs** (spec 2.12): Top (60), AI (40), Startups (40), India (30), Dev (30) and Reads (25), each with its own
  types, minimum importance and window; at most 2 cards from one publisher in the first 10 (4 per tab, an eighth
  of a long tab) and 3 per storyline in a tab's first 12 ranks (past them a development worth a rank of its own,
  up to 8 or a sixth of the tab); when the cap must cut, the storyline keeps its best cards by spice x importance
  x traction (`rank.storyline_cut`), not its freshest; never padded. Routine news (spice 3-4) from one outlet with
  no traction needs importance 7 for Top. A developer outlet's story (`desk: dev` in `sources.yaml`) is Dev news
  with `dev_tools` anywhere among its topics. Spice 7+ or traction (Techmeme, HN 300+, 4 publishers) lets a story
  in up to two importance points under a tab's minimum. A widely shared essay reaches Reads whoever published it,
  and Top from importance 5. Hard news from a site imported under the catch-all `web` source needs a second
  outlet or a Techmeme pick for Top. A
  duplicate guard drops a card whose headline is the same story as a better-covered card's (headline query
  cosine >= `dedup.t_headline` with a shared entity, or >= `dedup.t_headline_alone`), and the next clustering pass
  merges the two events; also a repeated picture in a tab's top 10.
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
  and `img/<hash>_thumb.webp` (360×225). Otherwise a generated topic gradient, in 24 looks a topic so that no two
  cards of an edition share one. Files are kept 14 days.
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
Each provider's key comes from the variable named in `config.yaml` (`HUB_API_KEY`, `NVIDIA_API_KEY`), else the file
named by `<NAME>_FILE`, else the last `export <NAME>=` line in `~/.bashrc`. Without the hub key the backup model
writes; with no key at all the worker still publishes, in degraded mode.

Three tools check a prompt, model or clustering change against a real run without touching it:
`tools/eval_cards.py STATE_DB "title words"` writes cards for chosen items and prints the scores, the news date
and each check's outcome; `tools/eval_events.py STATE_DB "headline words"` (or `--rewritten N`, `--briefs N`)
rewrites or briefs chosen events on a throwaway copy and prints the reports, the old card and the new one;
`tools/recluster.py STATE_DB --grep word` re-clusters a copy and prints the events.

A rule added to the write or clustering path acts on new items only. `tools/reexamine.py` applies such rules to
the cards a state already holds (take a backup and stop the worker first): follow-ups are put to the clustering
judge again and mark the events they overtook; written cards get their news date; events holding a report hours
newer than their lead are queued for a rewrite; and with `--edition` every card of that edition is fact-checked
again with the current rules, a card that fails going back to the writer with the reason.

The same worker also runs as a system service on the public VPS (a test run that publishes to a local directory
only); see [deploy/README.md](deploy/README.md). There the key comes from the file named by `NVIDIA_API_KEY_FILE`.

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
  - **freshness**: publisher→app lag p50/p95, counted from the earliest report of the story the engine saw
    (`story_first_report`: the earliest member or attached signal) to the first edition with its card;
  - **pipeline**: per-source status and check results, prefilter drops by rule, LLM calls/latency/429s,
    validation pass rate per item on its first run (and the eventual rate after hold retries), skip reasons;
  - **content**: events, raw vs clamped importance histogram; **spice**: its histogram per item and event, the
    last edition's median and count at 7+, and the push-alert candidates in rank order;
  - **freshness.live**: publisher→app lag for stories published while the worker was polling (by its poll log;
    the overall figure includes backlog after a seed or downtime), the feed's own share of it (`feedLagMin`:
    publisher → first seen) and both per source (`bySource`);
  - **llmBackup** (failovers to a backup model in the last 15 min) and **embedFailedTicks**.
- **Alerts** (ntfy, at most once per condition per 6 h; logged as `ALERT …` when `NTFY_TOPIC` is unset): no
  publish for 20 min, Top under 12, first-run validation pass rate under 80% over 1 h (at least 10 new items),
  more than 20 LLM 429s an hour or any 401/403, 10+ LLM timeouts/5xx an hour, the 1.2 decoder check failing, a
  source failing 6 polls in a row, more than 10% of stories (events) at raw importance ≥ 8, the primary LLM
  endpoint being answered by a backup (`llm_backup`), the embedding endpoint failing 5 ticks in a row, and the
  systemd unit failing (`techgist-alert@.service`), a nearly full disk, a corrupt `state.db` and ticks that keep
  failing. On the VPS `techgist-watchdog.timer` also checks the edition's age from outside the worker every
  minute and restarts it after 15 min without a publish (`deploy/README.md`, "Staying up"). A low pass rate is an alert only, never degraded mode; an endpoint that
  times out more than it answers for 15 min is (`degraded.py`).

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
