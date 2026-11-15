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
- `deploy.sh` installs the unit file when it has changed (a new key file variable, say) before restarting.
- Still to decide before cutover (owner): where `edition.json` and `img/` are served from. Image URLs in the
  edition point at `publish.base_url` (the GitHub raw URL), where nothing from this box is published, so they do
  not resolve; `health.json` reports this as `edition.last.imageUrls.served: false`. And `NTFY_TOPIC` is unset, so
  alerts are only logged.
- Checking a change against real data without touching the service: copy the state with a SQLite backup, then
  `tools/eval_cards.py COPY "title words"` (writes those cards with the current prompts and prints every check)
  and `tools/recluster.py COPY --grep word` (re-clusters the copy and prints the events).
