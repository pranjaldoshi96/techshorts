# TechShorts publisher

The app reads a ranked **edition** that this server publishes, instead of every phone fetching 22 feeds.
If the newest edition is more than 8 hours old, or can't be downloaded, the app fetches the feeds itself until a fresh one appears.

```
server (cron, every 2 h)                GitHub (techshorts repo)             iPhone
publish.sh ─ builds main.swift with ──► edition branch ──► raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json
             the app's own feed and                               │ fresh (< 8 h)?  yes → show it
             ranking code, in Docker                              └──────────────── no  → fetch feeds directly
```

The publisher is compiled from the same Swift files as the app (`ios/Techgist/Models` and `Services`), so ranking,
duplicate merging and filtering are identical on both sides. It also fills in missing images and excerpts from the
article pages, so phones don't have to.

**Reddit signal (server only):** each run makes one request for the day's top 100 posts across r/technology, r/programming, r/MachineLearning, r/LocalLLaMA, r/artificial, r/startups and r/webdev. A story whose exact link appears there counts as one more outlet covering it, so it ranks higher and its card shows "+N more". Reddit's open RSS allows about one anonymous request at a time, so this is one combined request with one retry; if Reddit is unreachable, the edition publishes without it (the log says so).

The server is on a private network, so phones never connect to it. It only pushes the finished file to GitHub, which serves it.

## Big story alerts

After each publish, `notify.py` may send one push alert through CloudKit (container `iCloud.com.pranjald.techgist`):
a Top story covered by 3+ outlets and under 6 hours old, or a pinned story with `"notify": true`. At most 1 per run
and 3 per 24 hours, never the same story twice. It needs `~/.techshorts/cloudkit_key.pem` (generated on the server)
and `~/.techshorts/cloudkit_key_id` (from CloudKit Console → Tokens & Keys → Server-to-Server Keys). Without them it
skips quietly. `python3 server/notify.py --test` sends a test alert to development builds.

## Your own stories and blocking

Create `~/.techshorts/editorial.json` on the server (see `editorial.example.json`):

- **`pinned`**: your own stories, shown first in their category until you remove them. `category` is one of
  `Top`, `AI`, `Startups`, `Dev`, `Blogs`, and `publishedAt` is an ISO date such as `2026-11-03T09:00:00Z`.
- **`blocked`**: leave stories out by source name (`"DEV Community"`) or any part of a link (`"example.com/some-story"`).

It's applied on the next run. To publish right away, run `~/TechShorts/server/publish.sh`.

## Setup (already done on the publishing server)

1. Docker and the `swift:6.2-jammy` image.
2. This repo at `~/TechShorts`. After changing the Swift code, update it there with `git pull`.
   The next run rebuilds the publisher automatically.
3. A deploy key at `~/.ssh/techshorts_deploy`. Add its `.pub` to GitHub under
   **Settings → Deploy keys → Add deploy key**, with **Allow write access** ticked. It can only push to this repo.
4. Nothing else on GitHub: the app reads `edition.json` straight from the `edition` branch. (To move to GitHub Pages later, turn on Pages for the `edition` branch and change `Edition.url` in the app.)
5. Cron, every 2 hours and 2 minutes after each restart (Docker and cron both start at boot):
   ```
   17 */2 * * * $HOME/TechShorts/server/publish.sh --log
   @reboot sleep 120 && $HOME/TechShorts/server/publish.sh --log
   ```

It's a plain script, not an AI agent, so every run does the same thing. If a run fails (network down, GitHub unreachable),
the next one 2 hours later tries again. The app tolerates up to 8 hours without a new edition, so 3 runs in a row can fail
before anyone notices, and even then the app keeps working by fetching the feeds itself.

## Checking on it

```bash
tail -20 ~/.techshorts/publish.log
curl -s https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json | head -c 200
```

The app's **Settings** screen shows where the stories came from: "TechShorts edition, 2 hours ago" or "Direct from publishers".
