# The worker on the public VPS: the live publisher since 2026-11-16

| | |
|---|---|
| Host | `root@213.210.36.218` (key `~/.ssh/techgist_vps`), Ubuntu 24.04. nginx there serves an unrelated site: never touch it. |
| Service | `techgist-worker.service` (system unit, user `techgist`, restarted for ever with no start limit, enabled at boot, logs in journald). See "Staying up" below. |
| Code | `/opt/techgist` (a copy of `server/`, owned by root), venv in `/opt/techgist/venv`, deployed revision in `/opt/techgist/REVISION` |
| State | `/var/lib/techgist` (`TECHGIST_STATE`): `state.db`, `health.json`, `editorial.yaml`, `bin/v1decode` |
| Edition | `/var/lib/techgist/edition/` (`edition.json`, `health.json`, `notifications.json`, `img/`): also a git checkout, force-pushed as one amended commit to the `edition` branch of `publish.git_remote` after a publish, at most every 2 min (`worker/mirror.py`). The app reads it at `https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json`. |
| Push access | The repository's deploy key (the one the old box uses; ED25519, fingerprint `SHA256:d61BnSsX...`), in `/var/lib/techgist/.ssh/techshorts_deploy` (`techgist:techgist`, 600), with `config` (that key for `github.com`, strict host checking) and `known_hosts` (GitHub's ED25519 host key, checked against GitHub's published fingerprint) beside it. Write access to this one repository. Only a process started with `TECHGIST_MIRROR=1` pushes; only `techgist-worker.service` sets it. |
| Keys | `/etc/techgist/` (`root:techgist`, directory 750, files 640). The worker reads `hub_key` (GPT and Gemini on the inference hub: card writing, fact-check, clustering judge, screening) through `HUB_API_KEY_FILE` and `nvidia_key` (embeddings and the backup chat model) through `NVIDIA_API_KEY_FILE`; keys are never in the unit, the environment or the journal. `/etc/techgist/env` holds `NTFY_TOPIC=...`, the ntfy topic alerts go to: a secret, never in the repository. |
| Firewall | ufw: OpenSSH, 80, 443 |
| 1.2 decoder | `/var/lib/techgist/bin/v1decode`, built by `setup_vps.sh` with the Swift 6.2 toolchain in `/opt/swift` (static stdlib; no Docker on this box). Every publish runs it. |

```bash
server/deploy/deploy.sh            # rsync server/ and restart the worker (commit first; REVISION records HEAD)
server/deploy/deploy.sh --setup    # first install, new requirements.txt, or a changed tools/v1decode

# On the VPS
systemctl status techgist-worker
journalctl -u techgist-worker -f
cd /opt/techgist && sudo -u techgist env TECHGIST_STATE=/var/lib/techgist venv/bin/python tools/run_report.py
cd /opt/techgist && sudo -u techgist env TECHGIST_STATE=/var/lib/techgist venv/bin/python tg status
```

Notes
- The provider's apt mirror list (`/etc/apt/sources.list.d/ubuntu-mirrors.list`) points at a mirror that no longer
  exists; `setup_vps.sh` passes options so its own apt calls use the official archive, and leaves the file alone.
- A source's first poll marks items older than `fetch.first_run_skip_hours` (36) as seen, so a fresh state starts with
  about a day and a half of news. To start over: stop the service, remove `/var/lib/techgist/state.db*`, start it.
- The `techgist` user has no shell. Its only credential is the deploy key above.
- Backups are in `/var/lib/techgist/backups/`: `state-<time>.db` is a SQLite backup taken before a risky change,
  and `run1-<time>/` holds the first test run's state (37 sources, Nemotron, no spice), set aside on 2026-11-15
  when the second run was seeded from scratch with the wide source list and the hub models.
- The review fixes of 2026-11-15 (briefs, tab sizes, traction floors, backup models) were deployed onto the
  second run's state, not a fresh one: `state-20261115T211500Z.db`, `state-20261115T213649Z.db` and
  `state-20261115T215422Z.db` are the SQLite backups taken before each of the three deploys, with the edition and
  health.json of 21:15 beside the first. After
  the first deploy `tools/requeue.py --hours 14 --no-text` put 27 paywalled tier-1 items that had been dropped for
  too little text back in the queue, so they could start events and get briefs.
- `deploy.sh` installs every `techgist-*` unit file that has changed before restarting, keeps the worker enabled
  and enables `techgist-watchdog.timer` and `techgist-backup.timer`.
- Image URLs in the edition point at `publish.base_url` (the GitHub raw URL of the `edition` branch), which is
  where this box pushes `img/`. Every 30 min the worker fetches three of them that the last push carried and
  alerts when one does not return 200 (`edition.last.imageUrls` in `health.json`).
- Checking a change against real data without touching the service: copy the state with a SQLite backup, then
  `tools/eval_cards.py COPY "title words"` (writes those cards with the current prompts and prints every check),
  `tools/eval_events.py COPY "headline words"` (rewrites or briefs those events on a throwaway copy) and
  `tools/recluster.py COPY --grep word` (re-clusters the copy and prints the events). A tool run as root from a
  copy of the code needs the service's key files in its environment and no `NTFY_TOPIC` (so nothing it does
  alerts the owner): `sudo -u techgist env TECHGIST_STATE=<copy dir> NVIDIA_API_KEY_FILE=/etc/techgist/nvidia_key
  HUB_API_KEY_FILE=/etc/techgist/hub_key /opt/techgist/venv/bin/python tools/...`. A whole tick on a copy
  (`-m worker.main --once --out <copy dir>/edition`) also needs `bin/v1decode` and `bin/v1decode.hash` copied
  into the copy's state directory, or the edition is not published. Such a tick never pushes: without
  `TECHGIST_MIRROR=1` in its environment `publish.git_remote` is ignored. Never add that variable to these commands.
- Applying a new rule to the cards the state already holds: `tools/reexamine.py` (see its docstring). With the
  worker and `techgist-watchdog.timer` stopped and a backup taken:
  `cd /opt/techgist && sudo -u techgist env TECHGIST_STATE=/var/lib/techgist NVIDIA_API_KEY_FILE=... HUB_API_KEY_FILE=...
  venv/bin/python -m tools.reexamine --hours 168 --ask-dates --edition /var/lib/techgist/edition/edition.json`,
  then start both. `--dry-run` first on a copy prints what it would do.
- The third review's fixes (bbadb79) were checked against live models on 2026-11-16 and five things were changed
  (revisions 2c627b6 to a4baadc). `tools/reexamine.py` then ran on the live state (5 min, worker stopped;
  `state-20261116T001859Z.db` is the backup taken before it, `state-20261115T233001Z.db` the one before the
  check began): 81 follow-ups put to the judge again and 2 older events marked as overtaken (the Vulcan launch
  card under the card reporting the rocket lost), 18 cards dated by their news (HomeView from Sunday 21:55 to
  Friday), 23 events queued for a rewrite with the newest account (the FAA card now gives the router, the Musk
  card "signed"), and 4 of the edition's 129 cards sent back by the fact-checker's new rules.

## Staying up

The aim: the worker keeps publishing without anyone watching, and when it cannot, the owner hears about it.

| What goes wrong | What happens | Where |
|---|---|---|
| The worker crashes or is killed | systemd restarts it: 5 s after the first failure, then 8, 14, 22, 36 and at most 60 s. There is no start limit, so it never gives up. An ntfy alert per crash, one per 15 min in a crash loop. | `techgist-worker.service`, `techgist-alert@.service`, `deploy/alert.py` |
| The machine reboots | The worker and both timers are enabled (`multi-user.target`, `timers.target`) and start by themselves. | `systemctl is-enabled` |
| A tick hangs (a feed, an LLM call or an image download that never returns; a stopped process) | The worker tells systemd it is alive between a tick's stages. After 10 min of silence systemd kills and restarts it (`WatchdogSec=600`). | `worker/resilience.py` `heartbeat()` |
| The worker is alive but has not published for 15 min, for any reason | `techgist-watchdog.timer` (root, every minute) reads the edition's `publishedAt`, restarts the worker and alerts. A worker started less than 15 min ago is left to publish; reminders every 30 min; one message when it publishes again. | `deploy/check_freshness.py` |
| `state.db` is corrupt | The worker exits at once; on the way up the file is set aside as `state.db.corrupt-<time>` and the newest backup that passes a check is restored (or an empty state started). Alert either way. | `resilience.recover_state()` |
| `state.db` is locked, or ticks keep failing | Each failed tick is rolled back and retried; after 5 in a row the worker alerts and exits for a clean start. | `resilience.TickGuard` |
| The disk is full | Alert at once (hourly while it lasts); the worker keeps trying every minute. Under 1 GB or 3% free it alerts before that (`disk_low`). | `resilience.TickGuard`, `disk_low()` |
| A memory leak | `MemoryMax=2G` ends the worker (and it restarts), not the machine. | the unit |
| The whole VPS is down, or its pushes stop reaching GitHub | Nothing on the VPS can say so. The old private box (`pranjald@10.40.138.81`) runs `~/techgist-freshness/freshness_cron.sh` from cron every 5 min: it reads the public URL and alerts when the edition is over 20 min old or unreadable, again every 30 min while it lasts, and once when it is fresh again. | `deploy/freshness_cron.sh`, below |
| A push to GitHub fails | Alert `mirror_push` from the worker (once per 6 h), retried after the next publish; the outside check above alerts if it lasts 20 min. | `worker/main.py` |

- **Alerts** go to the ntfy topic in `/etc/techgist/env` (`NTFY_TOPIC=...`, `root:techgist` 640). Subscribe to it
  in the ntfy app. Anyone who knows the topic can read and post to it, so it is not in the repository or the
  journal. `python3 /opt/techgist/deploy/alert.py --test` (as root) sends a labelled test message. The worker's
  own alerts (`health.notify`) are stamped only once ntfy accepted them, so one that could not be sent is tried
  again on the next tick.
- **Push-alert queue** (`worker/alertq.py`, table `alert_queue` in `state.db`): every publish puts its push-alert
  candidates in, one row per story for good. Rows survive a kill; a sent story is never queued again, under any of
  its merged ids. No sender exists yet (R3), so rows expire after 6 h. `tools/alertq.py` shows the queue;
  `health.json` has `alertQueue`. A sender must pass a row's `collapse_id` as `apns-collapse-id`: a send cut off
  by a crash is offered once more after 5 min, and that header is what keeps it to one alert on the phone.
- **Pruning** (`worker/maintenance.py`, once a day inside the worker): poll and prefilter logs over 7 days, the
  write log and publish rows over 14, embeddings and the stored article/feed text of items over 30. Item rows
  themselves stay (a known URL must stay known).
- **Backups**: `techgist-backup.timer` (03:40 UTC) runs `deploy/backup_state.py` as `techgist`: one consistent
  snapshot, integrity-checked, into `backups/nightly/`, newest 7 kept. Hand-made `backups/state-<time>.db` files
  are thinned to the newest 5 by the same run; `run1-*` and saved editions are never touched. By hand before a
  risky change: `sudo -u techgist python3 /opt/techgist/deploy/backup_state.py --state /var/lib/techgist --label x`.
- **Stopping the worker on purpose** for more than 15 min: `systemctl stop techgist-watchdog.timer` too, or the
  watchdog starts it again.
- **From another machine** (needs only Python 3 and `deploy/alert.py` beside it):
  `check_freshness.py https://raw.githubusercontent.com/<owner>/<repo>/edition/edition.json --max-age-min 20 --alert`
  exits 0 fresh, 1 stale, 2 unreachable. It also reads the `health.json` published beside the edition.
  Installed on the old box as `~/techgist-freshness/` (`freshness_cron.sh`, `check_freshness.py`, `alert.py`,
  `env` with the ntfy topic, mode 600, and `freshness.log`), crontab line
  `*/5 * * * * $HOME/techgist-freshness/freshness_cron.sh >/dev/null 2>&1`. To update it after a change here:
  `scp server/deploy/{check_freshness.py,alert.py,freshness_cron.sh} pranjald@10.40.138.81:techgist-freshness/`.
  That box is on a private network: if it is off or loses its connection, this check is silent.
  `.github/workflows/edition-freshness.yml` is the same check as a GitHub Action, still manual-trigger only (its
  schedule needs the repository secret `NTFY_TOPIC`, which only the owner can create).
- Not tested: a reboot (the unrelated site lives on this machine). Boot behaviour rests on the units being enabled.

Measured on the VPS on 2026-11-15 (22:38-23:15 UTC), on the second run's state, `git_remote` null throughout:

| Test | Result |
|---|---|
| `kill -9` of the worker | active again after 5.2 s, first tick done at 7.7 s, next publish 109 s after the kill (the edition was 103 s old; it is rewritten when it changes and at least every 5 min) |
| Six `kill -9` in 89 s (restart delays 5, 8, 14, 22, 36, 60 s) | active 60 s after the sixth kill, publishing 84 s after it; one alert sent, five held back by the rate limit and counted in the next |
| `SIGSTOP` (alive, silent), edition watchdog with the window shortened to 7 min for the test | restarted 462 s after the stop (42 s after the edition passed 7 min), published 23 s later; alert on the restart and a "publishing again" message. The window was put back to 15 min afterwards. |
| `SIGSTOP`, real settings | systemd's watchdog killed it at 600 s, active at 605 s, next publish at 675 s; alert "stopped answering and was killed by the watchdog" |
| Alert queue across the `kill -9` | 23 rows (21 waiting, one mid-send, one sent) identical before and after; the restarted worker's next publish queued 1 new story and no duplicate; re-queuing the sent story answers `duplicate` |
| Corrupt `state.db` (a copy, 200 pages overwritten) | set aside, the nightly backup restored in 0.1 s, opens with all rows |
| Nightly backup, run once by hand | 27.9 MB, integrity-checked, 0.1 s |

The restart delay does not go back to 5 s by itself: systemd counts restarts until the unit is started by hand, by
a deploy or by the watchdog timer, so after six crashes a seventh, days later, waits 60 s. `state-20261115T223826Z.db`
is the backup taken before this deploy.

## Cutover (done 2026-11-16) and rollback

The push path (`worker/mirror.py`) is the old publisher's scheme: one commit on the branch, amended and
force-pushed, so the first push **replaces** whatever is on the target branch and the branch never grows. The
local checkout is kept small the same way (no reflog, unreachable objects pruned after an hour).

What was done, in order:

1. The deploy key was piped from the old box to `/var/lib/techgist/.ssh/` (never displayed; same fingerprint on
   both machines) and checked with `git ls-remote` as the `techgist` user.
2. Rehearsal: `tools/rehearse_cutover.py --remote git@github.com:pranjaldoshi96/techshorts.git --branch
   edition-rehearsal --base-url https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition-rehearsal/`
   on the VPS: pushed twice, 1 commit on the branch, the 1.2 decoder accepted the pushed file, 125 of 125 image
   URLs returned 200. From the Mac the same file fetched over raw.githubusercontent.com passed the decoder check
   and all 125 image URLs returned 200. The branch was deleted afterwards.
3. Old box: crontab saved as `~/crontab.before-cutover-20261116`, the two `publish.sh` lines commented out, no
   `publish.sh` running.
4. `publish.git_remote` set in `config.yaml`, the `deploy.sh` guard changed to accept that remote or null and
   nothing else, `TECHGIST_MIRROR=1` added to the worker's unit, deployed. First push 03:29:03 UTC.

Measured after it (03:29-03:50 UTC, from the Mac, fetching the public URL about once a minute):

| | |
|---|---|
| Pushes | one about every 3 min (03:29, 03:32, 03:35, 03:38, 03:41, 03:44, 03:47), none failed |
| Public edition | the worker's from 03:33 on (126 articles, all with `rank`, `eventId`, `spice`); the 1.2 decoder check passed on every fetch |
| Age of the public edition | at most 6.1 min: GitHub's raw host caches a file for 5 min, so every second push is the one readers see |
| Images | 25 of the edition's image URLs sampled on every fetch: all 200 |
| The 1.2 app | built from the release commit `eafaa0b`, on a new simulator with no cache: it showed the worker's newest two Top cards with their pictures, and its cached `articles.json` held the public edition's 126 articles |
| The switch itself | for about 4 min (03:29-03:33) GitHub still served the old publisher's cached `edition.json` while its pictures were already gone from the branch: 1.2 users who refreshed then saw the old cards without pictures. A rollback has the same few minutes the other way. |
| A false alarm | the first publish checked image URLs before the first push and alerted on a 404; fixed (the check now expects only what the last push carried) |

`tools/rehearse_cutover.py` can be run again at any time against a scratch branch; it refuses the branch name
`edition` on a remote repository.

### Rollback

Back to the old publisher. The whole sequence, from the Mac; steps 1 and 2 are what matters, in that order:

```bash
# 1. The VPS stops pushing at once. The worker keeps running and publishing to its own disk, so nothing is lost.
ssh -i ~/.ssh/techgist_vps root@213.210.36.218 \
  "sed -i -E 's|^( +git_remote:).*|\1 null|' /opt/techgist/config.yaml && systemctl restart techgist-worker && grep -n 'git_remote:' /opt/techgist/config.yaml"

# 2. The old box publishes again: its saved crontab back (this also removes the outside freshness check, which
#    would alert on a 30-minute publisher), and one publish now. It force-replaces the branch (takes 1-2 min).
ssh pranjald@10.40.138.81 \
  'crontab ~/crontab.before-cutover-20261116 && ~/TechShorts/server/publish.sh --log; tail -2 ~/.techshorts/publish.log; crontab -l'

# 3. The same change in the repository, so the next deploy does not start the pushes again.
sed -i '' -E 's|^( +git_remote:).*|\1 null|' server/config.yaml && git commit -m "Rollback: the VPS stops pushing the edition" server/config.yaml
```

Then check: `curl -s https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json | head -c 60`
shows the old publisher's `publishedAt` within about 5 min (GitHub's cache). Phones that fetched the worker's
edition keep it until their next refresh; 1.2 falls back to fetching feeds itself when an edition is over 8 h old.

Why this works, checked on 2026-11-16 without publishing from the old box: the deploy key is still there (it was
copied, not moved; same fingerprint) and still reads the repository; `~/.techshorts/site` is still its git
checkout on branch `edition` and `publish.sh` pushes it with `-f`, so it does not matter what the worker pushed
in between; the Docker image `swift:6.2-jammy` and the built publisher are still on that box; the saved crontab
holds exactly the two lines that were live; and the `sed` in step 1, run on a copy of the deployed config, leaves
`git_remote` reading as null. Not checked: an actual publish from the old box after the cutover.

To cut over again after a rollback: set `publish.git_remote` back to the repository in `server/config.yaml`,
comment out the two cron lines on the old box and add the freshness line again, then `server/deploy/deploy.sh`.
