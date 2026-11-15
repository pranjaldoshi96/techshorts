# The worker on the public VPS (test run until cutover)

| | |
|---|---|
| Host | `root@213.210.36.218` (key `~/.ssh/techgist_vps`), Ubuntu 24.04. nginx there serves an unrelated site: never touch it. |
| Service | `techgist-worker.service` (system unit, user `techgist`, `Restart=always`, logs in journald) |
| Code | `/opt/techgist` (a copy of `server/`, owned by root), venv in `/opt/techgist/venv`, deployed revision in `/opt/techgist/REVISION` |
| State | `/var/lib/techgist` (`TECHGIST_STATE`): `state.db`, `health.json`, `editorial.yaml`, `bin/v1decode` |
| Edition | `/var/lib/techgist/edition/` (`edition.json`, `health.json`, `img/`). Not served and not pushed: `publish.git_remote` is null and `deploy.sh` refuses to deploy otherwise. |
| Keys | `/etc/techgist/` (`root:techgist`, directory 750, files 640). The worker reads `hub_key` (GPT and Gemini on the inference hub: card writing, fact-check, clustering judge, screening) through `HUB_API_KEY_FILE` and `nvidia_key` (embeddings and the backup chat model) through `NVIDIA_API_KEY_FILE`; keys are never in the unit, the environment or the journal. Optional `/etc/techgist/env` with `NTFY_TOPIC=...`. |
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
- `deploy.sh` installs the unit files when they have changed (a new key file variable, say) before restarting,
  and enables `techgist-watchdog.timer`.
- `techgist-watchdog.timer` (root, every 5 min) runs `deploy/check_freshness.py` on the edition file: after 30 min
  without a publish it alerts (ntfy when `NTFY_TOPIC` is set, else the journal) and restarts the worker, at most
  once per 30 min. The worker unit itself restarts without a start limit. The same script takes a URL, for a cron
  on another machine after cutover: `check_freshness.py https://raw.githubusercontent.com/.../edition/edition.json`.
- Still to decide before cutover (owner): where `edition.json` and `img/` are served from. Image URLs in the
  edition point at `publish.base_url` (the GitHub raw URL), where nothing from this box is published, so they do
  not resolve; `health.json` reports this as `edition.last.imageUrls.served: false`. And `NTFY_TOPIC` is unset, so
  alerts are only logged.
- Checking a change against real data without touching the service: copy the state with a SQLite backup, then
  `tools/eval_cards.py COPY "title words"` (writes those cards with the current prompts and prints every check)
  and `tools/recluster.py COPY --grep word` (re-clusters the copy and prints the events).

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
refuses a non-null remote on purpose: remove that guard in the same commit), set `NTFY_TOPIC` in
`/etc/techgist/env`, deploy, and watch `journalctl -u techgist-worker -f` for `mirror_pushed`.

Rollback: on the VPS set `publish.git_remote: null` and deploy (or `systemctl stop techgist-worker`); on the old
box restore the two `publish.sh` cron lines and run `~/TechShorts/server/publish.sh --log` once. Its next push
force-replaces the branch with the old publisher's edition, within 30 minutes at the latest. Phones that fetched
the new edition keep it until then; 1.2 falls back to fetching feeds itself when an edition is over 8 hours old.
