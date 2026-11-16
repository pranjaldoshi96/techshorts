"""The push service's own SQLite file (push.db, beside the engine's state.db): registered devices, the stories
taken from the engine's alert queue, and what each device was sent.

A device token is stored only in `devices`. What a device was sent is kept under `dkey`, a hash of the token, so
unregistering deletes the token at once while "never the same story twice" still holds if the device comes back
the same day; those rows are pruned after KEEP_DELIVERY_DAYS.

One Store per thread: the HTTP threads and the sender each open their own connection.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  token TEXT PRIMARY KEY,
  dkey TEXT NOT NULL,
  env TEXT NOT NULL,
  tz TEXT NOT NULL,
  locale TEXT,
  app_version TEXT,
  topics TEXT,
  owner INTEGER NOT NULL DEFAULT 0,
  held_event TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
  event_id TEXT PRIMARY KEY,
  collapse_id TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  score REAL,
  payload TEXT,
  received_at TEXT NOT NULL,
  not_before TEXT NOT NULL,
  killed INTEGER NOT NULL DEFAULT 0,
  queued_at TEXT                 -- when the engine queued it (about when the edition first carried it)
);
CREATE TABLE IF NOT EXISTS deliveries (
  dkey TEXT NOT NULL,
  event_id TEXT NOT NULL,
  status TEXT NOT NULL,          -- sent, or failed (APNs refused this story for this device for good)
  at TEXT NOT NULL,
  local_date TEXT,
  PRIMARY KEY (dkey, event_id)
);
CREATE INDEX IF NOT EXISTS deliveries_at ON deliveries(at);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)
"""
KEEP_ALERT_DAYS = 2
KEEP_DELIVERY_DAYS = 3


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def dkey(token):
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def short(token):
    """All of a token that may appear in a log line."""
    return (token or "")[:6] + "…"


class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        for stmt in SCHEMA.split(";"):
            self.conn.execute(stmt)
        if "queued_at" not in {r["name"] for r in self.q("PRAGMA table_info(alerts)")}:
            try:
                self.conn.execute("ALTER TABLE alerts ADD COLUMN queued_at TEXT")     # a push.db from before it
            except sqlite3.OperationalError:
                pass                                                              # another thread added it first
        self.conn.commit()

    def close(self):
        self.conn.close()

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    # Devices

    def register(self, token, env, tz, locale, app_version, topics, now, max_devices=None):
        """Adds the device or refreshes it (owner flag and history are kept). False when the registry is full."""
        if self.q("SELECT 1 FROM devices WHERE token = ?", token):
            self.conn.execute("UPDATE devices SET env = ?, tz = ?, locale = ?, app_version = ?, topics = ?,"
                              " updated_at = ? WHERE token = ?",
                              (env, tz, locale, app_version, json.dumps(topics) if topics is not None else None,
                               iso(now), token))
        else:
            if max_devices and self.device_count() >= max_devices:
                return False
            self.conn.execute("INSERT INTO devices (token, dkey, env, tz, locale, app_version, topics, created_at,"
                              " updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                              (token, dkey(token), env, tz, locale, app_version,
                               json.dumps(topics) if topics is not None else None, iso(now), iso(now)))
        self.conn.commit()
        return True

    def remove(self, token):
        cur = self.conn.execute("DELETE FROM devices WHERE token = ?", (token,))
        self.conn.commit()
        return cur.rowcount

    def set_env(self, token, env):
        self.conn.execute("UPDATE devices SET env = ? WHERE token = ?", (env, token))
        self.conn.commit()

    def devices(self):
        return self.q("SELECT * FROM devices ORDER BY created_at, token")

    def owners(self):
        return self.q("SELECT * FROM devices WHERE owner = 1")

    def find(self, prefix):
        return self.q("SELECT * FROM devices WHERE token LIKE ? ORDER BY updated_at DESC", prefix.lower() + "%")

    def latest(self):
        return self.q("SELECT * FROM devices ORDER BY updated_at DESC, created_at DESC LIMIT 1")

    def set_owner(self, token, owner=True):
        self.conn.execute("UPDATE devices SET owner = ? WHERE token = ?", (1 if owner else 0, token))
        self.conn.commit()

    def device_count(self):
        return self.q("SELECT COUNT(*) n FROM devices")[0]["n"]

    def device_stats(self):
        r = self.q("SELECT COUNT(*) n, COALESCE(SUM(env = 'sandbox'), 0) sandbox,"
                   " COALESCE(SUM(env = 'production'), 0) production, COALESCE(SUM(owner), 0) owner,"
                   " COALESCE(SUM(held_event IS NOT NULL), 0) held FROM devices")[0]
        return {"total": r["n"], "sandbox": r["sandbox"], "production": r["production"], "owner": r["owner"],
                "holding": r["held"]}

    def set_held(self, token, event_id):
        self.conn.execute("UPDATE devices SET held_event = ? WHERE token = ?", (event_id, token))

    # Stories taken from the engine's queue

    def add_alert(self, event_id, collapse_id, title, body, score, payload, now, hold_s, queued_at=None):
        """True when the story is new here. A second claim of the same queue row (a crash between this and the
        engine's mark_sent) changes nothing."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO alerts (event_id, collapse_id, title, body, score, payload, received_at,"
            " not_before, queued_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id, collapse_id, title or "", body or "", score, json.dumps(payload or {}), iso(now),
             iso(now + timedelta(seconds=hold_s)), queued_at))
        self.conn.commit()
        return cur.rowcount == 1

    def kill(self, event_id):
        cur = self.conn.execute("UPDATE alerts SET killed = 1 WHERE event_id = ? AND killed = 0", (event_id,))
        self.conn.commit()
        return cur.rowcount

    def released(self, now, window_h):
        """Stories that may be sent: not killed, past the owner hold, taken in the last window_h hours."""
        return self.q("SELECT * FROM alerts WHERE killed = 0 AND not_before <= ? AND received_at >= ?"
                      " ORDER BY score DESC", iso(now), iso(now - timedelta(hours=window_h)))

    def waiting_hold(self, now):
        return self.q("SELECT COUNT(*) n FROM alerts WHERE killed = 0 AND not_before > ?", iso(now))[0]["n"]

    # What each device got

    def record(self, token, event_id, status, now, local_date=None):
        self.conn.execute("INSERT OR REPLACE INTO deliveries (dkey, event_id, status, at, local_date)"
                          " VALUES (?,?,?,?,?)", (dkey(token), event_id, status, iso(now), local_date))
        self.conn.execute("UPDATE devices SET held_event = NULL WHERE token = ? AND held_event = ?",
                          (token, event_id))
        self.conn.commit()

    def history(self, now):
        """dkey -> [(event_id, status, at, local_date)] for everything still remembered."""
        out = {}
        for r in self.q("SELECT * FROM deliveries ORDER BY at"):
            out.setdefault(r["dkey"], []).append((r["event_id"], r["status"], from_iso(r["at"]), r["local_date"]))
        return out

    def sent_since(self, since):
        return self.q("SELECT COUNT(*) n FROM deliveries WHERE status = 'sent' AND at >= ?", iso(since))[0]["n"]

    def prune(self, now):
        self.conn.execute("DELETE FROM alerts WHERE received_at < ?", (iso(now - timedelta(days=KEEP_ALERT_DAYS)),))
        self.conn.execute("DELETE FROM deliveries WHERE at < ?", (iso(now - timedelta(days=KEEP_DELIVERY_DAYS)),))
        self.conn.commit()

    # Counters and cursors

    def get(self, key, default=None):
        r = self.q("SELECT value FROM kv WHERE key = ?", key)
        return r[0]["value"] if r else default

    def put(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    def bump(self, key, n=1):
        self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                          " value = CAST(value AS INTEGER) + ?", (key, str(n), n))
        self.conn.commit()

    def counter(self, key):
        return int(self.get(key, 0) or 0)
