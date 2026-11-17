"""The sending loop: engine queue -> push.db -> APNs.

One tick:
  intake    claims rows from the engine's alert_queue (worker/alertq.py: claim, mark_sent), writes each story into
            push.db and only then marks the queue row sent. A crash between the two leaves the row `sending`; the
            queue offers it again after its lease and the second write is ignored (the story is already here).
            From then on push.db is the record: a queue row marked sent means "handed to the push service".
  deliver   for every device, rules.decide() picks at most one story; it is sent with the queue row's collapse id
            and recorded per device as soon as APNs answers. A crash between "APNs took it" and the record sends
            it once more with the same apns-collapse-id, which replaces the first on the phone.
            The alert's words are the edition's at the moment of sending (the card's `notification`), so a card
            the engine corrected after it was queued is never announced with its old text.
            A round stops early when APNs is not answering or the round has run long; the rest go next loop.
  relay     operational alerts posted to the owner's ntfy topic are also sent to devices marked as the owner's.
  health    counts into push_health.json.

Nothing is claimed while no device is registered: the queue keeps its rows (and expires them itself).
"""
import json
import os
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import apns as apns_mod
from . import rules
from .store import from_iso, iso, short

HOLD_TAG = "techgist_push_hold"


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


class EngineDB:
    """The engine's state.db as worker/alertq.py needs it (conn, q, one, commit, get_meta), without the
    worker's DB class: that one runs the engine's schema migrations on open, which is the worker's business."""

    def __init__(self, path, timeout=5):
        self.conn = sqlite3.connect(path, timeout=timeout)
        self.conn.row_factory = sqlite3.Row

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def commit(self):
        self.conn.commit()

    def get_meta(self, key, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", key)
        return row["value"] if row else default

    def close(self):
        self.conn.close()


class Ntfy:
    """The owner's ntfy topic: where this service reports its own failures (like the worker and the units do),
    and where it reads operational alerts from to pass them on to the owner's devices. The topic is a secret."""

    def __init__(self, cfg, topic=None, opener=urllib.request.urlopen, clock=time.time):
        self.url = cfg["owner"]["ntfy_url"]
        self.topic = topic if topic is not None else os.environ.get("NTFY_TOPIC")
        self.opener, self.clock = opener, clock
        self.last = {}

    def send(self, title, message, key=None, every_s=3600, priority=None, tags=None, log=print):
        """True when ntfy accepted it. With `key`, at most one message per every_s."""
        if key is not None:
            if self.clock() - self.last.get(key, -1e18) < every_s:
                return False
        if not self.topic:
            log(f"ALERT {title}: {message} (NTFY_TOPIC unset; not sent)")
            return False
        headers = {"Title": title.encode("ascii", "replace").decode()}
        if priority:
            headers["Priority"] = str(priority)
        if tags:
            headers["Tags"] = tags
        try:
            req = urllib.request.Request(f"{self.url}/{self.topic}", data=message.encode(), headers=headers,
                                         method="POST")
            with self.opener(req, timeout=10) as r:
                r.read(2000)
        except (OSError, ValueError) as e:
            log(f"ALERT {title}: {message} (ntfy failed: {type(e).__name__})")
            return False
        if key is not None:
            self.last[key] = self.clock()
        log(f"ALERT {title}: {message} (ntfy accepted)")
        return True

    def poll(self, since):
        """Messages after `since` (a message id or a unix time), oldest first."""
        if not self.topic:
            return []
        req = urllib.request.Request(f"{self.url}/{self.topic}/json?poll=1&since={since}")
        with self.opener(req, timeout=15) as r:
            lines = r.read(400000).decode("utf-8", "replace").splitlines()
        out = []
        for line in lines:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if isinstance(m, dict) and m.get("event") == "message" and m.get("id"):
                out.append(m)
        return out


class Edition:
    """The published edition's articles by eventId, reread when the file changes. The edition is where a story's
    news date, link and picture come from, and a story that is not in it cannot be opened by a tap."""

    def __init__(self, path):
        self.path, self.stamp, self.by_id = path, None, {}

    def articles(self):
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self.stamp:
                with open(self.path) as f:
                    doc = json.load(f)
                self.by_id = {a["eventId"]: a for a in doc.get("articles", []) if a.get("eventId")}
                self.stamp = stamp
        except (OSError, ValueError):
            pass        # mid-write or missing: keep the last good copy
        return self.by_id


def alert_text(alert, article):
    """(title, body) for a story's alert: the edition card's notification text as it stands now, so a card the
    engine corrected after the story was queued is announced with the corrected words. The queue row's text is
    used only when the card carries none. Never a mix of the two."""
    note = (article or {}).get("notification")
    if isinstance(note, dict):
        title, body = note.get("title"), note.get("body")
        if isinstance(title, str) and isinstance(body, str) and title.strip() and body.strip():
            return title, body
    return alert["title"], alert["body"]


def _news_at(article):
    try:
        return from_iso(article["publishedAt"][:19] + "Z")
    except (KeyError, TypeError, ValueError):
        return None


class Sender:
    def __init__(self, cfg, store, apns, alertq=None, engine=None, edition=None, ntfy=None, log=print,
                 heartbeat=None, mono=time.monotonic):
        self.cfg, self.store, self.apns, self.alertq = cfg, store, apns, alertq
        self.engine = engine or (lambda: EngineDB(cfg["paths"]["engine_db"]))
        self.edition = edition or Edition(cfg["paths"]["edition"])
        self.ntfy = ntfy or Ntfy(cfg)
        self.log = log
        self.pool = ThreadPoolExecutor(max_workers=cfg["apns"]["concurrency"], thread_name_prefix="apns")
        self.last_relay = None
        self.relayed_at = []
        self.last_prune = None
        self.dry_loops = 0
        self.last_error = None
        self.busy_loops = 0
        self.heartbeat, self.mono = heartbeat, mono
        self.beat = time.monotonic()

    def _alive(self):
        """Called between batches of a long round: the loop is working, for systemd's watchdog and /v1/health."""
        self.beat = time.monotonic()
        if self.heartbeat is not None:
            self.heartbeat()

    # 1. Engine queue -> push.db

    def intake(self, now):
        if not self.cfg["sending_enabled"] or self.alertq is None or self.store.device_count() == 0:
            return 0
        try:
            db = self.engine()
        except sqlite3.Error as e:
            self.log(f"intake: engine state not readable ({type(e).__name__})")
            return 0
        taken = 0
        try:
            self.alertq.ensure(db)
            for alert in self.alertq.claim(db, now, limit=20):
                row = db.one("SELECT queued_at FROM alert_queue WHERE id = ?", alert.id)
                new = self.store.add_alert(alert.event_id, alert.collapse_id, alert.title, alert.body, alert.score,
                                           alert.payload, now, self.cfg["rules"]["owner_hold_s"],
                                           queued_at=row["queued_at"] if row else None)
                self.alertq.mark_sent(db, alert.id, now)
                taken += 1
                self.log(f"intake: {alert.event_id} {'taken' if new else 'already held (redelivery)'}")
                if new and self.cfg["rules"]["owner_hold_s"] > 0 and self._worth_announcing(alert, now):
                    self.ntfy.send(f"Techgist push in {self.cfg['rules']['owner_hold_s'] // 60} min",
                                   f"{alert.title}\n{alert.body}\nStop it: /opt/techgist-push/pushctl kill {alert.event_id}",
                                   priority="low", tags=HOLD_TAG, log=self.log)
            db.commit()
            self.busy_loops = 0
        except sqlite3.OperationalError as e:
            # The worker holds the write lock while it writes a tick; the row stays where it was. Normal for a
            # loop or two, so it is only worth a line once it has lasted a minute.
            self.busy_loops += 1
            if self.busy_loops % 4 == 0:
                self.log(f"intake: engine state busy for {self.busy_loops} loops ({e})")
        finally:
            db.close()
        return taken

    def _worth_announcing(self, alert, now):
        """Only stories some device could still get are announced to the owner: the queue also carries stories
        that are already too old to alert on."""
        art = self.edition.articles().get(alert.event_id)
        at = _news_at(art) if art else None
        return at is not None and now - at <= timedelta(hours=self.cfg["rules"]["max_age_h"])

    # 2. push.db -> devices

    def stories(self, now):
        r = self.cfg["rules"]
        arts = self.edition.articles()
        out, rows = [], {}
        for a in self.store.released(now, r["held_max_age_h"] + r["max_age_h"]):
            art = arts.get(a["event_id"])
            at = _news_at(art) if art else None
            if at is None:
                continue
            title, body = alert_text(a, art)
            if not title or not body:
                continue
            payload = json.loads(a["payload"] or "{}")
            spice = art.get("spice") if isinstance(art.get("spice"), int) else payload.get("spice", 0)
            known = from_iso(a["queued_at"] or a["received_at"])
            out.append(rules.Story(a["event_id"], a["score"] or 0.0, int(spice or 0),
                                   int(payload.get("importance") or 0), at, known))
            rows[a["event_id"]] = (dict(a, payload=payload, title=title, body=body), art)
        return out, rows

    def deliver(self, now):
        if not self.cfg["sending_enabled"]:
            return {}
        stories, rows = self.stories(now)
        if not stories:
            return {}
        hist_all = self.store.history(now)
        jobs = []
        for d in self.store.devices():
            h = rules.History(held=d["held_event"], registered_at=from_iso(d["created_at"]))
            for event_id, status, at, local_date in hist_all.get(d["dkey"], ()):
                h.got.add(event_id)
                if status == "sent":
                    h.sent_local_dates.append(local_date)
                    h.last_sent_at = at if h.last_sent_at is None else max(h.last_sent_at, at)
            dec = rules.decide(now, d["tz"], stories, h, self.cfg["rules"])
            if dec.action == "hold":
                self.store.set_held(d["token"], dec.event_id)
            elif dec.action == "send":
                jobs.append((d, dec.event_id))
        self.store.conn.commit()
        return self._send_all(jobs, rows, now)

    def _send_all(self, jobs, rows, now):
        tally = {}
        expiration = int(now.timestamp()) + self.cfg["apns"]["expiration_s"]

        def one(job):
            d, event_id = job
            alert, art = rows[event_id]
            payload = apns_mod.story_payload(alert, art, apns_mod.sound_for(d, self.cfg["apns"]["sound"]))
            return job, self.apns.send(d["token"], d["env"], payload, collapse_id=alert["collapse_id"],
                                       expiration=expiration)

        # In batches, so that a round can stop: with APNs unreachable every send takes its full timeouts and
        # retries, and a round over all devices would outlast the unit's watchdog.
        a = self.cfg["apns"]
        batch, started, streak = max(1, a["concurrency"]), self.mono(), 0
        for i in range(0, len(jobs), batch):
            for (d, event_id), res in self.pool.map(one, jobs[i:i + batch]):
                tally[res.outcome] = tally.get(res.outcome, 0) + 1
                self._settle(d, event_id, res, now)
                streak = streak + 1 if res.outcome in ("retry", "auth") else 0
            self._alive()
            left = len(jobs) - (i + batch)
            if left > 0 and (streak >= a["breaker_failures"] or self.mono() - started >= a["round_max_s"]):
                why = (f"APNs is not answering ({streak} sends in a row failed)" if streak >= a["breaker_failures"]
                       else f"the round has run {a['round_max_s']} s")
                self.log(f"deliver: stopped, {why}; {left} sends wait for the next loop")
                tally["deferred"] = left
                break
        if jobs:
            self.log("deliver: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
            ok = tally.get("ok", 0)
            self.dry_loops = 0 if ok or not (tally.get("retry") or tally.get("auth")) else self.dry_loops + 1
            if tally.get("auth"):
                self.ntfy.send("Techgist push: APNs refused our key",
                               f"APNs answered 403 ({self.last_error['reason']}) to {tally['auth']} sends. Check the "
                               "key file, key id, team id and topic. Nothing is lost: stories are tried again.",
                               key="apns_auth", priority="high", tags="warning", log=self.log)
            elif self.dry_loops >= 5:
                self.ntfy.send("Techgist push: sends are failing",
                               f"No send has succeeded in {self.dry_loops} rounds; last answer: "
                               f"{self.last_error['status']} {self.last_error['reason']}.",
                               key="apns_failing", priority="high", tags="warning", log=self.log)
        return tally

    def _settle(self, d, event_id, res, now):
        """Records one APNs answer. `event_id` is None for a server alert to an owner device."""
        tok = short(d["token"])
        if res.outcome == "ok":
            if event_id is not None:
                self.store.record(d["token"], event_id, "sent", now,
                                  rules.local(now, d["tz"]).strftime("%Y-%m-%d"))
            if res.env != d["env"]:
                self.store.set_env(d["token"], res.env)
            self.store.bump("sent_total")
            self.store.put("last_sent_at", iso(now))
            self.log(f"sent {event_id or 'server-alert'} to {tok} ({res.env}) apns-id {res.apns_id}")
            return
        self.last_error = {"at": iso(now), "status": res.status, "reason": res.reason, "outcome": res.outcome}
        self.store.put("last_error", json.dumps(self.last_error))
        if res.outcome == "gone":
            self.store.remove(d["token"])
            self.store.bump("removed_total")
            self.log(f"removed {tok}: APNs {res.status} {res.reason}")
            return
        self.store.bump("failed_total")
        if res.outcome == "bad" and event_id is not None:
            # APNs will never take this story for this device (a payload problem): not tried again.
            self.store.record(d["token"], event_id, "failed", now)
        self.log(f"failed {event_id or 'server-alert'} to {tok}: {res.outcome} {res.status} {res.reason}")

    # 3. Operational alerts -> owner devices

    def relay(self, now):
        o = self.cfg["owner"]
        if not o["relay_ntfy"] or not self.cfg["sending_enabled"]:
            return 0
        if self.last_relay is not None and (now - self.last_relay).total_seconds() < o["poll_s"]:
            return 0
        self.last_relay = now
        since = self.store.get("ntfy_since")
        if since is None:
            # First run: start from now, so old alerts are not replayed.
            self.store.put("ntfy_since", int(now.timestamp()))
            return 0
        try:
            messages = self.ntfy.poll(since)
        except (OSError, ValueError) as e:
            self.log(f"relay: ntfy not readable ({type(e).__name__})")
            return 0
        # ntfy can hand back a message it already gave (two posted in one second, a cursor it has forgotten), so
        # the cursor alone is not trusted: an id is relayed once, ever, and the phone gets a few an hour at most.
        seen = json.loads(self.store.get("ntfy_relayed") or "[]")
        sent = 0
        for m in messages:
            mid = str(m["id"])
            if mid not in seen:
                seen.append(mid)
                self.relayed_at = [t for t in self.relayed_at if (now - t).total_seconds() < 3600]
                if HOLD_TAG in (m.get("tags") or []) or int(m.get("priority") or 3) < o["relay_min_priority"]:
                    pass                                    # the owner's ntfy topic has it; not worth a phone alert
                elif len(self.relayed_at) >= o["relay_max_per_hour"]:
                    self.log(f"relay: hourly limit reached, not sent to phones: {m.get('title')}")
                else:
                    self.relayed_at.append(now)
                    payload = apns_mod.server_payload(m.get("title"), m.get("message"))
                    for d in self.store.owners():
                        res = self.apns.send(d["token"], d["env"], payload, collapse_id="ntfy-" + mid,
                                             expiration=int(now.timestamp()) + 86400)
                        self._settle(d, None, res, now)
                        sent += res.outcome == "ok"
                        self._alive()
            self.store.put("ntfy_since", m["id"])
        self.store.put("ntfy_relayed", json.dumps(seen[-300:]))
        return sent

    # 4. Health

    def health(self, now):
        s = self.store
        doc = {"at": iso(now), "sendingEnabled": bool(self.cfg["sending_enabled"]),
               "devices": s.device_stats(),
               "sent": {"total": s.counter("sent_total"), "last24h": s.sent_since(now - timedelta(hours=24)),
                        "lastAt": s.get("last_sent_at")},
               "failed": {"total": s.counter("failed_total"), "last": json.loads(s.get("last_error") or "null")},
               "removedDevices": s.counter("removed_total"),
               "held": {"devicesHoldingForMorning": s.device_stats()["holding"],
                        "storiesInOwnerHold": s.waiting_hold(now)},
               "engineQueue": self._queue_counts()}
        path = self.cfg["paths"]["health"]
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(doc, f, indent=1)
            os.replace(path + ".tmp", path)
        except OSError as e:
            self.log(f"health: not written ({type(e).__name__})")
        return doc

    def _queue_counts(self):
        """The engine queue's rows by status, read-only (None when state.db cannot be read)."""
        try:
            conn = sqlite3.connect(f"file:{self.cfg['paths']['engine_db']}?mode=ro", uri=True, timeout=2)
            try:
                return dict(conn.execute("SELECT status, COUNT(*) FROM alert_queue GROUP BY status").fetchall())
            finally:
                conn.close()
        except sqlite3.Error:
            return None

    def tick(self, now=None):
        now = now or utcnow()
        self.intake(now)
        self.deliver(now)
        self.relay(now)
        if self.last_prune is None or now - self.last_prune > timedelta(hours=6):
            self.store.prune(now)
            self.last_prune = now
        self.health(now)
        self.beat = time.monotonic()

    def alive(self):
        """For /v1/health: the loop has finished a tick recently."""
        return time.monotonic() - self.beat < max(120, self.cfg["loop_s"] * 6)
