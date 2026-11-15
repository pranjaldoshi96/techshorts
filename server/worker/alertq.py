"""The durable queue of stories waiting for a push alert, and the record of what was sent.

Push itself (spec section 6, worker/push.py) is not built. This is the part that must not lose or repeat anything
across a crash, so the sender can be added on top of it: every publish puts its push-alert candidates here
(publish_v1.notification_candidates), one row per story for good, in the worker's own SQLite state.

A row's life:   queued -> sending -> sent
                queued -> expired            (nobody sent it within its time: old news is not an alert)
                queued -> cancelled          (the owner's `tg kill`)
                sending -> queued -> ... -> dead   (the send failed; retried up to MAX_ATTEMPTS)

No loss: a row is written in the same transaction as the tick that found the story, and only ever leaves `queued`
through claim(), which commits before the sender is handed the row.
No duplicate: `event_id` is unique, merged events count as the story they were merged into (event_alias), and a
sent row is kept for KEEP_DAYS, longer than aliases live. The one case no queue can settle alone is a crash between
"APNs took it" and "marked sent": that row is still `sending` when the worker comes back. It is offered again once
its lease has run out, flagged `redelivery`, with the same `collapse_id`; the sender must pass that as
apns-collapse-id, so a second delivery replaces the first on the device instead of showing twice.
"""
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

from .db import from_iso, iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  collapse_id TEXT NOT NULL,
  status TEXT NOT NULL,
  title TEXT, body TEXT, score REAL, payload TEXT,
  queued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  claimed_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  sent_at TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS alert_queue_status ON alert_queue(status, expires_at);
"""

# A candidate is at most 12 h old when it is listed (publish_v1.NOTIFY_MAX_AGE_H); queued for longer than this
# without a sender taking it, it is no longer worth an alert.
TTL = timedelta(hours=6)
# How long a claimed row may stay `sending` before it is taken to be orphaned by a crash.
LEASE = timedelta(minutes=5)
MAX_ATTEMPTS = 5
# Finished rows are the dedupe record. Aliases live 30 days (cluster.alias_days); keep these longer.
KEEP_DAYS = 45
# Set by resilience.recover_state() after a corrupt state.db was replaced by a backup or an empty state: the record
# of what was sent since that backup is gone, so stories published before this time are never queued again.
FLOOR_KEY = "alertq_floor"
_NS = uuid.UUID("6f1d7c0a-5a51-4d0e-9a53-7d1c0f0e3b11")


@dataclass
class Alert:
    id: int
    event_id: str
    collapse_id: str
    title: str
    body: str
    score: float
    payload: dict
    attempts: int
    redelivery: bool


def ensure(db):
    """Creates the table when it is missing. Statement by statement: executescript would commit the caller's
    open transaction."""
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            db.conn.execute(stmt)


def family(db, event_id):
    """The ids that are this story: the event it was merged into (following aliases) and every id merged into
    that one. A story alerted on under an old id is not alerted on again under the survivor's."""
    canon, hops = event_id, 0
    while hops < 10:
        row = db.one("SELECT id FROM event_alias WHERE old_id = ?", canon)
        if row is None or row["id"] == canon:
            break
        canon, hops = row["id"], hops + 1
    ids, todo = {event_id, canon}, [canon]
    while todo:
        for r in db.q("SELECT old_id FROM event_alias WHERE id = ?", todo.pop()):
            if r["old_id"] not in ids:
                ids.add(r["old_id"])
                todo.append(r["old_id"])
    return ids


def _existing(db, event_id):
    ids = sorted(family(db, event_id))
    return db.one(f"SELECT * FROM alert_queue WHERE event_id IN ({','.join('?' * len(ids))})"
                  " ORDER BY (status IN ('sent', 'sending')) DESC, id LIMIT 1", *ids)


def enqueue(db, event_id, title, body, now, score=None, payload=None, published_at=None, ttl=TTL):
    """Queues a story once. Returns "queued", "duplicate" (this story, under any of its ids, already has a row:
    waiting, sent or finished some other way; a waiting row gets the newer text and score), or "before_restore".
    Does not commit: the caller's transaction (the tick's) carries it."""
    floor = db.get_meta(FLOOR_KEY)
    if floor and (published_at is None or iso(published_at) <= floor):
        return "before_restore"
    old = _existing(db, event_id)
    if old is not None:
        if old["status"] == "queued":
            db.conn.execute("UPDATE alert_queue SET title = ?, body = ?, score = ?, payload = ? WHERE id = ?",
                            (title, body, score, json.dumps(payload or {}), old["id"]))
        return "duplicate"
    db.conn.execute(
        "INSERT INTO alert_queue (event_id, collapse_id, status, title, body, score, payload, queued_at, expires_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (event_id, str(uuid.uuid5(_NS, event_id)), "queued", title, body, score, json.dumps(payload or {}),
         iso(now), iso(now + ttl)))
    return "queued"


def enqueue_candidates(db, candidates, now):
    """Queues a publish report's push-alert candidates (publish_v1.notification_candidates). Returns how many were
    new. A candidate's age (`ageH`) gives its publication time for the restore floor."""
    new = 0
    for c in candidates or ():
        published = now - timedelta(hours=float(c.get("ageH") or 0))
        got = enqueue(db, c["eventId"], c.get("title"), c.get("body"), now, score=c.get("score"),
                      payload={k: c[k] for k in ("headline", "url", "spice", "importance", "type") if k in c},
                      published_at=published)
        new += got == "queued"
    return new


def expire(db, now):
    cur = db.conn.execute("UPDATE alert_queue SET status = 'expired' WHERE status = 'queued' AND expires_at <= ?",
                          (iso(now),))
    return cur.rowcount


def claim(db, now, limit=1, lease=LEASE):
    """Hands the sender up to `limit` rows, best score first, and commits them as `sending` before returning, so a
    crash after this point cannot hand the same row out twice within the lease. Rows left `sending` by a process
    that died come back once the lease is over, flagged `redelivery` (see the module docstring)."""
    expire(db, now)
    rows = db.q("SELECT * FROM alert_queue WHERE status = 'queued' OR (status = 'sending' AND claimed_at <= ?)"
                " ORDER BY (status = 'sending') DESC, score DESC, id LIMIT ?", iso(now - lease), limit)
    out = []
    for r in rows:
        db.conn.execute("UPDATE alert_queue SET status = 'sending', claimed_at = ?, attempts = attempts + 1"
                        " WHERE id = ?", (iso(now), r["id"]))
        out.append(Alert(r["id"], r["event_id"], r["collapse_id"], r["title"], r["body"], r["score"],
                         json.loads(r["payload"] or "{}"), r["attempts"] + 1, r["status"] == "sending"))
    db.commit()
    return out


def mark_sent(db, alert_id, now):
    """The sender got a success from APNs. Committed at once."""
    db.conn.execute("UPDATE alert_queue SET status = 'sent', sent_at = ?, last_error = NULL WHERE id = ?"
                    " AND status = 'sending'", (iso(now), alert_id))
    db.commit()


def mark_failed(db, alert_id, now, error, max_attempts=MAX_ATTEMPTS):
    """The send failed for certain (nothing was delivered): back in the queue, or `dead` after max_attempts."""
    row = db.one("SELECT attempts FROM alert_queue WHERE id = ? AND status = 'sending'", alert_id)
    if row is None:
        return None
    status = "dead" if row["attempts"] >= max_attempts else "queued"
    db.conn.execute("UPDATE alert_queue SET status = ?, last_error = ?, claimed_at = NULL WHERE id = ?",
                    (status, str(error)[:300], alert_id))
    db.commit()
    return status


def cancel(db, event_id):
    """The owner killed the story before it was sent. A row that is sending or sent is left as it is."""
    ids = sorted(family(db, event_id))
    cur = db.conn.execute(f"UPDATE alert_queue SET status = 'cancelled' WHERE status = 'queued' AND event_id IN"
                          f" ({','.join('?' * len(ids))})", ids)
    return cur.rowcount


def prune(db, now, keep_days=KEEP_DAYS):
    """Drops finished rows older than keep_days. Waiting and in-flight rows are never dropped."""
    cur = db.conn.execute("DELETE FROM alert_queue WHERE status IN ('sent', 'expired', 'cancelled', 'dead')"
                          " AND queued_at < ?", (iso(now - timedelta(days=keep_days)),))
    return cur.rowcount


def stats(db, now):
    """For health.json: rows by status, and how long the oldest waiting one has waited."""
    by = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM alert_queue GROUP BY status")}
    oldest = db.one("SELECT MIN(queued_at) at FROM alert_queue WHERE status = 'queued'")["at"]
    return {"byStatus": by, "queued": by.get("queued", 0), "sending": by.get("sending", 0), "sent": by.get("sent", 0),
            "oldestQueuedMin": round((now - from_iso(oldest)).total_seconds() / 60, 1) if oldest else None,
            "restoreFloor": db.get_meta(FLOOR_KEY)}
