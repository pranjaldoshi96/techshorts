# Techgist push (R3)

Real push notifications, sent from the VPS: a device registry the app talks to, and a sender that takes stories
from the engine's alert queue and delivers them through APNs.

| | |
|---|---|
| App-facing URL | `https://techgist.213-210-36-218.sslip.io` (`POST /v1/devices`, `DELETE /v1/devices/<token>`, `GET /v1/health`) |
| Service | `techgist-push.service` (user `techgist`, restarted for ever, no start limit, enabled at boot, systemd watchdog 300 s, `OnFailure=` the same ntfy alert as the worker) |
| Code | `/opt/techgist-push/push` (a copy of `server/push/`), venv `/opt/techgist-push/venv`, revision in `/opt/techgist-push/REVISION`. It imports `worker.alertq` from the engine's own `/opt/techgist`. |
| State | `/var/lib/techgist/push.db` (devices, stories taken, what each device got), `/var/lib/techgist/push_health.json` |
| Backup | `techgist-push-backup.timer`, nightly 03:50 UTC: `push/backup.py` writes a checked copy to `/var/lib/techgist/backups/nightly/push-<time>.db` and keeps 7. Restore: stop the service, copy one over `push.db`, remove `push.db-wal` and `push.db-shm`, start it. |
| Key | `/etc/techgist/apns/AuthKey_K2DXJ7NMGX.p8` (`root:techgist` 640, directory 750). Key id, team and topic are in `config.py`. |
| Settings | defaults in `config.py`; `/var/lib/techgist/push.json` overrides any of them (read at start) |
| TLS | Let's Encrypt certificate for the sslip.io name (it resolves to the VPS address; nothing was bought), HTTP-01 through the webroot `/var/www/techgist-acme`, renewed by `certbot.timer`, nginx reloaded by the certificate's deploy hook |
| nginx | one added file, `/etc/nginx/sites-available/techgist-push` (linked in `sites-enabled`). Only `/v1/` is proxied, to `127.0.0.1:8787`, at most 5 requests a second per address (burst 20). No version in answers; its own error log at `crit` only, so a device token in a request line is never logged. No other nginx file is touched. |

```bash
server/push/deploy/deploy_push.sh            # copy code, (re)build the venv if requirements changed, restart
server/push/deploy/deploy_push.sh --key      # also copy the APNs key from ~/Desktop (scp; never shown)
server/push/deploy/deploy_push.sh --https    # also (re)run setup_https.sh: nginx file + certificate

# On the VPS
/opt/techgist-push/pushctl status            # devices, sent, failed, held
/opt/techgist-push/pushctl devices           # first six characters of each token, env, time zone, owner flag
/opt/techgist-push/pushctl owner --latest    # the device that registered last is the owner's (or: owner PREFIX)
/opt/techgist-push/pushctl test PREFIX       # one labelled test notification to that device, now
/opt/techgist-push/pushctl test PREFIX --event ev_...   # that edition story as a real alert carries it, now
/opt/techgist-push/pushctl kill ev_...       # stop a story before it goes out
journalctl -u techgist-push -f
```

## The registry

`POST /v1/devices` with `{"token", "env": "sandbox"|"production", "tz", "locale", "appVersion", "topics"?}` answers
`{"ok": true}`; the same call again refreshes the device. `DELETE /v1/devices/<token>` removes it (same answer for
an unknown token). The token must be 64 to 200 hex characters; `env`, `tz` and `topics` have a length and character
limit and a value outside it is refused with 400 and the field's name; `locale` loses anything after `@` (iOS sends
`en_US@rg=inzzzz`) and, like `appVersion`, is stored as unknown when it does not fit. Bodies over 2 KB are refused. 60 requests a minute
per address, of which 10 registrations (anyone can register a well-formed token, and each costs APNs calls at the
next story), and 30 per token, then 429. There is no endpoint that lists or returns devices, answers never contain a
token, and the log shows a token's first six characters at most. `topics` is stored and not used yet.

Unregistering deletes the token at once. What a device was sent is kept for 3 days under a hash of the token, so a
device that comes back does not get the same story again.

### The hostname is in the app

The 2.0 binary carries `https://techgist.213-210-36-218.sslip.io`. That name spells the VPS's address and is
resolved by sslip.io, a free DNS service nobody here controls: while it ships, **the VPS must keep this address**,
and if sslip.io stops answering, installed apps cannot register or opt out until an app update (alerts to devices
already registered keep going; nothing in sending needs the name). To move to a name on a domain the owner
controls: point an A record at the VPS, run `TECHGIST_PUSH_HOST=<name> bash /opt/techgist-push/push/deploy/setup_https.sh`
with the sslip.io name kept as a second `server_name` and certificate (the script writes one name today; add the
second server block by hand or extend it), and ship that name in `PushConfig.registerURL` in the next app build.
The old name has to keep answering for as long as 2.0 builds with it are in use.

## The sender

Every 15 s:

1. **Intake.** `alertq.claim()` on the engine's `state.db`, each story copied into `push.db`, then
   `alertq.mark_sent()`. From there `push.db` is the record: a queue row marked `sent` means "handed to the push
   service". Nothing is claimed while no device is registered.
2. **Owner hold** (spec 6.2). A new story that some device could still get is announced on the ntfy topic
   ("Techgist push in 5 min", low priority) and waits 5 minutes; `pushctl kill <id>` in that time stops it.
3. **Per device**, `rules.decide()` picks at most one story:

   | Rule | Default (`rules` in config) | Source |
   |---|---|---|
   | spice at least | `min_spice` 7 | task |
   | story age by its news date (the edition's `publishedAt`) | `max_age_h` 3 | spec 6.2 |
   | never the same story twice on a device | always | |
   | never a story that was already out when the device registered (by the engine's `queued_at`): the reader is looking at it, and a reinstalled app with a new token is not sent old news | always | |
   | alerts per device per local day | `daily_cap` 2 | spec 6.2 |
   | importance that may exceed the cap, and the ceiling it stops at | `override_importance` 9, `daily_cap_hard` 3 | task, spec 6.3 |
   | minutes between two alerts on a device | `min_gap_min` 180 | spec 6.2 |
   | quiet hours, device-local | `quiet_start` 22:00, `quiet_end` 07:30 | spec 6.2 |
   | the best story of the night is held and sent when quiet hours end, up to this old | `held_max_age_h` 12 | task |

   The looser set that was also proposed (6 a day, 30 minutes apart, quiet 23:00 to 07:00) is this in `push.json`:
   `{"rules": {"daily_cap": 6, "daily_cap_hard": 0, "min_gap_min": 30, "quiet_start": "23:00", "quiet_end": "07:00"}}`
   then `systemctl restart techgist-push`.
4. **APNs.** HTTP/2 to `api.sandbox.push.apple.com` or `api.push.apple.com` by the device's `env`, one ES256
   provider token reused for 40 minutes, `apns-collapse-id` = the queue row's collapse id, `apns-expiration` +3 h.
   The payload is `{"aps": {"alert": {"title", "body"}, "sound": "techgist.caf", "mutable-content": 1, "thread-id":
   eventId}, "eventId", "url", "image"}`; title, body, link and picture are the edition's **at the moment of
   sending**: the card's `notification` text as the engine last wrote it, so a card corrected while its story
   waited (owner hold, the 3 h gap, the night) is announced with the corrected words. The queue row's text is used
   only for a card that carries no `notification`. A story that is not in the edition is not sent (a tap could
   not open it).
   - 410, `BadDeviceToken`, `Unregistered`, `DeviceTokenNotForTopic`: the device is removed. `BadDeviceToken` is
     first tried once on the other host; if that one takes it, the device's `env` is corrected.
   - 429, 5xx, network errors: three attempts with 0.5 s and 2 s between, then again on the next loop.
   - Sends go out in batches of 8. A round stops, and the rest wait for the next loop, when 8 sends in a row ended
     in a retry or a refused key (APNs is down: a loop then costs one batch, about 33 s, however many devices
     there are) or when the round has run 120 s (`apns.breaker_failures`, `apns.round_max_s`). systemd's watchdog
     and `/v1/health` are told after every batch.
   - 403: the provider token is renewed once; if APNs still refuses, an ntfy alert (hourly at most) and the story
     stays for the next loop.

**Crashes.** Killed after the claim: the queue offers the row again after its 5-minute lease. Killed between the
copy and `mark_sent`: the second copy is ignored. Killed between APNs' answer and the per-device record: that device
is sent the story once more with the same collapse id, which replaces the first on the phone.

## Owner alerts in the app

`pushctl owner --latest` (or `owner PREFIX`) marks a registered device as the owner's. Once a minute the service
reads the ntfy topic and sends every new message there (worker crashes, the watchdog, stale editions, this
service's own failures) to owner devices as "Server alert: <title>", outside quiet hours, caps and gaps. The
"push in 5 min" notices are not relayed. ntfy itself is unchanged, so both arrive.

## Health

`push_health.json`: devices (total, sandbox, production, owner, holding a story for the morning), sent (total, last
24 h, last time), failed (total and APNs' last answer), devices removed, stories in the owner hold, and the engine
queue's counts. A crash or watchdog kill raises the worker's kind of ntfy alert through `techgist-alert@`; an
unreadable key, APNs refusing the key, five rounds without one successful send, and a loop that fails ten times in
a row raise their own.

## Open decisions for the owner

- **Alert volume.** With 2 a day, 3 h apart and spice >= 7, a device's allowance is normally used by the story
  held overnight (07:30) and the next eligible one (about 10:30); later news that day alerts only at importance
  >= 9. Tighter choice of stories: `{"rules": {"min_spice": 8}}` (the app's HOT badge). More alerts: `daily_cap`.
- **Production APNs.** Delivery through `api.push.apple.com` to a real token has not been seen yet (the key is
  proven there: a made-up token gets 400 BadDeviceToken, not 403). After the first TestFlight or App Store install
  with alerts allowed: `pushctl devices` (the row says `production`), then `pushctl test PREFIX --event ev_...`
  must print `APNs: ok status=200 ... host=production`.

## Not built

The spec's morning gist (6.3), the region gate, the `STORY` category with Save and Share actions, and
`/v1/metric`. The registry is on the VPS, not a Cloudflare Worker.
