# The worker on the public VPS (test run until cutover)

| | |
|---|---|
| Host | `root@213.210.36.218` (key `~/.ssh/techgist_vps`), Ubuntu 24.04. nginx there serves an unrelated site: never touch it. |
| Service | `techgist-worker.service` (system unit, user `techgist`, restarted for ever with no start limit, enabled at boot, logs in journald). See "Staying up" below. |
| Code | `/opt/techgist` (a copy of `server/`, owned by root), venv in `/opt/techgist/venv`, deployed revision in `/opt/techgist/REVISION` |
| State | `/var/lib/techgist` (`TECHGIST_STATE`): `state.db`, `health.json`, `editorial.yaml`, `bin/v1decode` |
| Edition | `/var/lib/techgist/edition/` (`edition.json`, `health.json`, `img/`). Not served and not pushed: `publish.git_remote` is null and `deploy.sh` refuses to deploy otherwise. |
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
- The `techgist` user has no shell, no SSH key and no git remote, so this box cannot push to the `edition` branch.
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
- Still to decide before cutover (owner): where `edition.json` and `img/` are served from. Image URLs in the
  edition point at `publish.base_url` (the GitHub raw URL), where nothing from this box is published, so they do
  not resolve; `health.json` reports this as `edition.last.imageUrls.served: false`.
- Checking a change against real data without touching the service: copy the state with a SQLite backup, then
  `tools/eval_cards.py COPY "title words"` (writes those cards with the current prompts and prints every check),
  `tools/eval_events.py COPY "headline words"` (rewrites or briefs those events on a throwaway copy) and
  `tools/recluster.py COPY --grep word` (re-clusters the copy and prints the events). A tool run as root from a
  copy of the code needs the service's key files in its environment and no `NTFY_TOPIC` (so nothing it does
  alerts the owner): `sudo -u techgist env TECHGIST_STATE=<copy dir> NVIDIA_API_KEY_FILE=/etc/techgist/nvidia_key
  HUB_API_KEY_FILE=/etc/techgist/hub_key /opt/techgist/venv/bin/python tools/...`. A whole tick on a copy
  (`-m worker.main --once --out <copy dir>/edition`) also needs `bin/v1decode` and `bin/v1decode.hash` copied
  into the copy's state directory, or the edition is not published.
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
| The whole VPS is down | Nothing on the VPS can say so. `deploy/check_freshness.py URL` from another machine exits non-zero; `.github/workflows/edition-freshness.yml` runs it, **manual trigger only** until the owner enables its schedule. | below |

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
  `check_freshness.py https://raw.githubusercontent.com/<owner>/<repo>/edition/edition.json --max-age-min 25 --alert`
  exits 0 fresh, 1 stale, 2 unreachable. It also reads the `health.json` published beside the edition. Until
  cutover that URL is the old publisher's edition, and nothing from the VPS is reachable from outside.
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

## Cutover rehearsal and rollback

The push path (`worker/mirror.py`) is the old publisher's scheme: one commit on the branch, amended and
force-pushed, so the first push **replaces** whatever is on the target branch. It is off while
`publish.git_remote` is null.

`tools/rehearse_cutover.py` runs that same push against a scratch target from a copy of the edition directory,
clones the result and checks it with the 1.2 decoder and against every article's image path. It refuses the live
branch name on a remote repository.

```bash
cd /opt/techgist
# 1. Mechanics only, on local disk (no GitHub, no key):
sudo -u techgist git init --bare /var/lib/techgist/rehearsal.git
sudo -u techgist env TECHGIST_STATE=/var/lib/techgist venv/bin/python tools/rehearse_cutover.py \
    --remote /var/lib/techgist/rehearsal.git --branch rehearsal

# 2. End to end, before the real cutover (owner: needs a scratch repository and a deploy key with write access
#    to it, readable by the techgist user, e.g. /etc/techgist/deploy_key, root:techgist 640):
sudo -u techgist env TECHGIST_STATE=/var/lib/techgist venv/bin/python tools/rehearse_cutover.py \
    --remote git@github.com:OWNER/techgist-rehearsal.git --branch rehearsal --deploy-key /etc/techgist/deploy_key \
    --base-url https://raw.githubusercontent.com/OWNER/techgist-rehearsal/rehearsal/
#    It fetches edition.json and every image URL and prints how many return 200. Then point a 1.2 simulator
#    build's edition URL at that base URL and open the app.
```

Cutover, once step 2 passes: on the old box comment out the two `publish.sh` cron lines; on the VPS install the
real repository's deploy key, set `publish.git_remote` and `publish.deploy_key` in `config.yaml` (`deploy.sh`
refuses a non-null remote on purpose: remove that guard in the same commit), deploy, and watch `journalctl -u techgist-worker -f` for `mirror_pushed`.

Rollback: on the VPS set `publish.git_remote: null` and deploy (or `systemctl stop techgist-worker`); on the old
box restore the two `publish.sh` cron lines and run `~/TechShorts/server/publish.sh --log` once. Its next push
force-replaces the branch with the old publisher's edition, within 30 minutes at the latest. Phones that fetched
the new edition keep it until then; 1.2 falls back to fetching feeds itself when an edition is over 8 hours old.
