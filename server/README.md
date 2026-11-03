# TechShorts publisher

The app reads a ranked **edition** that this server publishes, instead of every phone fetching 22 feeds.
If the newest edition is more than 8 hours old, or can't be downloaded, the app fetches the feeds itself until a fresh one appears.

```
server (cron, every 2 h)                GitHub Pages                         iPhone
publish.sh ─ builds main.swift with ──► edition branch ──► pranjaldoshi96.github.io/TechShorts/edition.json
             the app's own feed and                               │ fresh (< 8 h)?  yes → show it
             ranking code, in Docker                              └──────────────── no  → fetch feeds directly
```

The publisher is compiled from the same Swift files as the app (`ios/TechShorts/Models` and `Services`), so ranking,
duplicate merging and filtering are identical on both sides. It also fills in missing images and excerpts from the
article pages, so phones don't have to.

The server is on a private network, so phones never connect to it. It only pushes the finished file to GitHub, which serves it.

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
4. GitHub Pages: **Settings → Pages → Deploy from a branch → `edition` / root**.
5. Cron: `17 */2 * * * $HOME/TechShorts/server/publish.sh >> $HOME/.techshorts/publish.log 2>&1`

## Checking on it

```bash
tail -20 ~/.techshorts/publish.log
curl -s https://pranjaldoshi96.github.io/TechShorts/edition.json | head -c 200
```

The app's **Settings** screen shows where the stories came from: "TechShorts edition, 2 hours ago" or "Direct from publishers".
