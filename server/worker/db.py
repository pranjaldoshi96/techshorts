"""SQLite state (~/.techshorts/state.db). One connection, used only from the worker's main thread.

Times are stored as ISO-8601 UTC strings ("2026-11-11T03:30:00Z"), which sort correctly as text.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  etag TEXT,
  last_modified TEXT,
  body_sha TEXT,
  last_polled_at TEXT,
  next_poll_at TEXT,
  last_status INTEGER,
  last_error TEXT,
  last_ok_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  check_ok INTEGER,          -- last check_sources result; tier-1 needs 1
  check_at TEXT,
  check_detail TEXT
);

-- One row per canonical URL. status: new (waiting for text + LLM), signal (from a signal source),
-- dropped (prefilter), stale (too old at first sight), seen (existed before the source's first poll).
CREATE TABLE IF NOT EXISTS items (
  url_canonical TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  source_id TEXT NOT NULL,
  title TEXT NOT NULL,
  feed_text TEXT,
  image TEXT,
  extra TEXT,
  feed_published_at TEXT,
  first_seen_at TEXT NOT NULL,
  published_at TEXT NOT NULL,
  status TEXT NOT NULL,
  drop_rule TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  retry_at TEXT
);
CREATE INDEX IF NOT EXISTS items_status ON items(status, published_at);

-- Every source that listed a URL; signal sources and shared feeds (TechCrunch + TechCrunch AI) attach here.
CREATE TABLE IF NOT EXISTS sightings (
  url_canonical TEXT NOT NULL,
  source_id TEXT NOT NULL,
  title TEXT,
  feed_published_at TEXT,
  first_seen_at TEXT NOT NULL,
  PRIMARY KEY (url_canonical, source_id)
);

CREATE TABLE IF NOT EXISTS prefilter_log (
  at TEXT NOT NULL, source_id TEXT, url TEXT, title TEXT, rule TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prefilter_at ON prefilter_log(at);

CREATE TABLE IF NOT EXISTS polls (
  at TEXT NOT NULL, source_id TEXT NOT NULL, status INTEGER, entries INTEGER, new_items INTEGER,
  ms INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS polls_at ON polls(at);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- LLM triage + card text per item (WP2). status: ok (validated text), thin (classification only, text too short
-- to lead), failed (last ladder run failed; the item is held or dropped in `items`).
CREATE TABLE IF NOT EXISTS writes (
  url_canonical TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  written_at TEXT NOT NULL,
  model TEXT,
  passed_at INTEGER,
  reason TEXT,
  text_origin TEXT,
  text_words INTEGER,
  text TEXT,
  attribution TEXT,
  type TEXT, is_news INTEGER, relevance INTEGER, importance INTEGER, breaking INTEGER, sensitive INTEGER,
  region TEXT, topics TEXT, entities TEXT, event TEXT, material_update INTEGER,
  headline TEXT, summary TEXT, summary_short TEXT, notif_line TEXT, s1 TEXT, s2 TEXT, s3 TEXT,
  entailed INTEGER,
  output TEXT,
  steps TEXT
);

-- One row per LLM call (kind write|entail|triage) and per item outcome (kind item), for health.json.
CREATE TABLE IF NOT EXISTS write_log (
  at TEXT NOT NULL, url_canonical TEXT, kind TEXT NOT NULL, model TEXT, outcome TEXT, reason TEXT, ms INTEGER
);
CREATE INDEX IF NOT EXISTS write_log_at ON write_log(at);
"""

# Columns added after a table first shipped; added in place so an existing state.db keeps working.
MIGRATIONS = {"items": [("hold_reason", "TEXT")]}


# Held items retry at +30 min and +2 h, then drop (spec 2.5 and 2.8).
HOLD_RETRIES = (timedelta(minutes=30), timedelta(hours=2))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def from_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if s else None


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # WAL lets check_sources and `sqlite3` readers look in while the worker writes.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.q(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def commit(self):
        self.conn.commit()

    # Sources

    def source_state(self, source_id):
        row = self.one("SELECT * FROM sources WHERE id = ?", source_id)
        if row is None:
            self.conn.execute("INSERT INTO sources (id) VALUES (?)", (source_id,))
            row = self.one("SELECT * FROM sources WHERE id = ?", source_id)
        return row

    def update_source(self, source_id, **fields):
        self.source_state(source_id)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE sources SET {cols} WHERE id = ?", (*fields.values(), source_id))

    def check_ok(self, source_id):
        row = self.one("SELECT check_ok FROM sources WHERE id = ?", source_id)
        return bool(row and row["check_ok"])

    def log_poll(self, at, source_id, status, entries, new_items, ms, error):
        self.conn.execute("INSERT INTO polls VALUES (?,?,?,?,?,?,?)",
                          (iso(at), source_id, status, entries, new_items, ms, error))

    # Items

    def item(self, url_canonical):
        return self.one("SELECT * FROM items WHERE url_canonical = ?", url_canonical)

    def insert_item(self, it):
        self.conn.execute(
            "INSERT INTO items (url_canonical, url, source_id, title, feed_text, image, extra, feed_published_at,"
            " first_seen_at, published_at, status, drop_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (it["url_canonical"], it["url"], it["source_id"], it["title"], it.get("feed_text"), it.get("image"),
             json.dumps(it.get("extra") or {}), iso(it.get("feed_published_at")), iso(it["first_seen_at"]),
             iso(it["published_at"]), it["status"], it.get("drop_rule")))

    def replace_item(self, it):
        """A news source re-lists a URL first seen on a signal source: it becomes that source's item."""
        self.conn.execute(
            "UPDATE items SET url = ?, source_id = ?, title = ?, feed_text = ?, image = ?, extra = ?,"
            " feed_published_at = ?, published_at = ?, status = ?, drop_rule = ? WHERE url_canonical = ?",
            (it["url"], it["source_id"], it["title"], it.get("feed_text"), it.get("image"),
             json.dumps(it.get("extra") or {}), iso(it.get("feed_published_at")), iso(it["published_at"]),
             it["status"], it.get("drop_rule"), it["url_canonical"]))

    def add_sighting(self, url_canonical, source_id, title, feed_published_at, first_seen_at):
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO sightings VALUES (?,?,?,?,?)",
            (url_canonical, source_id, title, iso(feed_published_at), iso(first_seen_at)))
        return cur.rowcount == 1

    def log_drop(self, at, source_id, url, title, rule):
        self.conn.execute("INSERT INTO prefilter_log VALUES (?,?,?,?,?)", (iso(at), source_id, url, title, rule))

    def prefilter_counts(self, since):
        return {r["rule"]: r["n"] for r in self.q(
            "SELECT rule, COUNT(*) n FROM prefilter_log WHERE at >= ? GROUP BY rule ORDER BY n DESC", iso(since))}

    # Meta

    def get_meta(self, key, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", key)
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    # Writing (WP2)

    def pending_writes(self, now):
        """Items waiting for text + LLM: new ones, and held ones whose retry time has come."""
        return self.q("SELECT * FROM items WHERE status = 'new' OR (status = 'held' AND retry_at <= ?)", iso(now))

    def hold(self, url_canonical, reason, now):
        """Holds an item for a retry at +30 min, then +2 h; the third failure drops it. Never a permanent skip
        on the first failure, so a page that was briefly down or a model hiccup gets another chance."""
        row = self.item(url_canonical)
        attempts = (row["attempts"] or 0) + 1
        if attempts > len(HOLD_RETRIES):
            self.conn.execute("UPDATE items SET status = 'dropped', drop_rule = ?, attempts = ?, retry_at = NULL,"
                              " hold_reason = ? WHERE url_canonical = ?",
                              (f"held_out:{reason}", attempts, reason, url_canonical))
            return "dropped"
        self.conn.execute("UPDATE items SET status = 'held', attempts = ?, retry_at = ?, hold_reason = ?"
                          " WHERE url_canonical = ?",
                          (attempts, iso(now + HOLD_RETRIES[attempts - 1]), reason, url_canonical))
        return "held"

    def mark_written(self, url_canonical):
        self.conn.execute("UPDATE items SET status = 'written', retry_at = NULL, hold_reason = NULL"
                          " WHERE url_canonical = ?", (url_canonical,))

    def save_write(self, url_canonical, now, result, text, attribution):
        o = result.output
        row = dict(
            url_canonical=url_canonical, status=result.status, written_at=iso(now), model=result.model,
            passed_at=result.passed_at, reason=result.reason, text_origin=text.origin, text_words=text.words,
            text=text.text, attribution=attribution.prefix if attribution else None,
            entailed=None if result.entailed is None else int(result.entailed),
            steps=json.dumps([s.__dict__ for s in result.steps]))
        if o is not None:
            row.update(
                type=o.type, is_news=int(o.is_news), relevance=o.relevance, importance=o.importance,
                breaking=int(o.breaking), sensitive=int(o.sensitive), region=o.region, topics=json.dumps(o.topics),
                entities=o.entities.model_dump_json(), event=o.event, material_update=int(o.material_update),
                output=o.model_dump_json())
            if result.status == "ok":
                row.update(headline=o.headline, summary=result.full, summary_short=result.short,
                           notif_line=o.notif_line, s1=o.s1, s2=o.s2, s3=o.s3)
        old = self.one("SELECT status FROM writes WHERE url_canonical = ?", url_canonical)
        # A failed retry must not wipe an earlier thin item's classification.
        if old is not None and o is None and old["status"] == "thin":
            self.conn.execute("UPDATE writes SET written_at = ?, reason = ?, steps = ? WHERE url_canonical = ?",
                              (row["written_at"], row["reason"], row["steps"], url_canonical))
            return
        cols = ", ".join(row)
        self.conn.execute(f"INSERT OR REPLACE INTO writes ({cols}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))

    def write_row(self, url_canonical):
        return self.one("SELECT * FROM writes WHERE url_canonical = ?", url_canonical)

    def log_write(self, at, url_canonical, kind, model, outcome, reason=None, ms=None):
        self.conn.execute("INSERT INTO write_log VALUES (?,?,?,?,?,?,?)",
                          (iso(at), url_canonical, kind, model, outcome, reason, ms))

    def llm_calls_since(self, since):
        return self.one("SELECT COUNT(*) n FROM write_log WHERE at >= ? AND kind != 'item'", iso(since))["n"]

