"""Staying up (worker/resilience.py, alertq.py, maintenance.py and deploy/): the alert queue across a kill, a corrupt
or locked database, a full disk, pruning, backups, the watchdog's decisions and the unit files. No network."""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(SERVER, "deploy")
sys.path.insert(0, DEPLOY)

import alert as alert_cli  # noqa: E402
import backup_state  # noqa: E402
import check_freshness  # noqa: E402

from worker import alertq, health, maintenance, resilience  # noqa: E402
from worker.db import DB, iso  # noqa: E402

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def queue_db(path=":memory:"):
    db = DB(str(path))
    alertq.ensure(db)
    return db


def rows(db):
    return [(r["event_id"], r["status"]) for r in db.q("SELECT event_id, status FROM alert_queue ORDER BY id")]


# The alert queue

def test_a_story_is_queued_once_and_survives_reopening(tmp_path):
    db = queue_db(tmp_path / "s.db")
    assert alertq.enqueue(db, "ev1", "Title", "Body", NOW, score=0.5) == "queued"
    assert alertq.enqueue(db, "ev1", "Title 2", "Body", NOW, score=0.7) == "duplicate"
    db.commit()
    db.close()
    db = queue_db(tmp_path / "s.db")
    assert rows(db) == [("ev1", "queued")]
    # The waiting row took the newer text and score.
    r = db.one("SELECT title, score FROM alert_queue")
    assert (r["title"], r["score"]) == ("Title 2", 0.7)


def test_a_sent_story_is_never_queued_or_handed_out_again():
    db = queue_db()
    alertq.enqueue(db, "ev1", "T", "B", NOW)
    (a,) = alertq.claim(db, NOW)
    alertq.mark_sent(db, a.id, NOW)
    assert alertq.enqueue(db, "ev1", "T", "B", NOW + timedelta(hours=1)) == "duplicate"
    assert alertq.claim(db, NOW + timedelta(days=1)) == []
    assert rows(db) == [("ev1", "sent")]


def test_a_merged_event_counts_as_the_story_it_was_merged_into():
    db = queue_db()
    alertq.enqueue(db, "old", "T", "B", NOW)
    (a,) = alertq.claim(db, NOW)
    alertq.mark_sent(db, a.id, NOW)
    # "old" was merged into "new", and "older" had been merged into "old".
    db.conn.execute("INSERT INTO event_alias VALUES ('old', 'new', ?)", (iso(NOW),))
    db.conn.execute("INSERT INTO event_alias VALUES ('older', 'old', ?)", (iso(NOW),))
    assert alertq.family(db, "older") == {"older", "old", "new"}
    for eid in ("new", "old", "older"):
        assert alertq.enqueue(db, eid, "T", "B", NOW) == "duplicate"
    assert rows(db) == [("old", "sent")]


def test_claim_is_committed_before_the_sender_gets_the_row(tmp_path):
    db = queue_db(tmp_path / "s.db")
    alertq.enqueue(db, "ev1", "T", "B", NOW)
    db.commit()
    (a,) = alertq.claim(db, NOW)
    # A second connection (what a restarted worker would see) already has it as sending.
    other = sqlite3.connect(str(tmp_path / "s.db"))
    assert other.execute("SELECT status, attempts FROM alert_queue").fetchone() == ("sending", 1)
    assert not a.redelivery and a.attempts == 1


def test_a_send_cut_off_by_a_crash_is_offered_once_more_with_the_same_collapse_id():
    db = queue_db()
    alertq.enqueue(db, "ev1", "T", "B", NOW)
    (first,) = alertq.claim(db, NOW)
    # The process died here: never marked sent. Within the lease nothing is handed out.
    assert alertq.claim(db, NOW + timedelta(minutes=1)) == []
    (again,) = alertq.claim(db, NOW + alertq.LEASE + timedelta(seconds=1))
    assert again.redelivery and again.collapse_id == first.collapse_id and again.attempts == 2
    alertq.mark_sent(db, again.id, NOW)
    assert alertq.claim(db, NOW + timedelta(hours=1)) == []


def test_a_failed_send_is_retried_then_given_up_on():
    db = queue_db()
    alertq.enqueue(db, "ev1", "T", "B", NOW)
    for n in range(1, alertq.MAX_ATTEMPTS + 1):
        (a,) = alertq.claim(db, NOW)
        assert a.attempts == n
        status = alertq.mark_failed(db, a.id, NOW, "503")
    assert status == "dead" and alertq.claim(db, NOW) == []


def test_an_unsent_row_expires_and_stays_as_the_dedupe_record():
    db = queue_db()
    alertq.enqueue(db, "ev1", "T", "B", NOW)
    assert alertq.claim(db, NOW + alertq.TTL + timedelta(minutes=1)) == []
    assert rows(db) == [("ev1", "expired")]
    assert alertq.enqueue(db, "ev1", "T", "B", NOW + timedelta(hours=7)) == "duplicate"


def test_the_best_score_is_claimed_first_and_cancel_drops_a_waiting_row():
    db = queue_db()
    alertq.enqueue(db, "low", "T", "B", NOW, score=0.1)
    alertq.enqueue(db, "high", "T", "B", NOW, score=0.9)
    alertq.enqueue(db, "killed", "T", "B", NOW, score=1.0)
    assert alertq.cancel(db, "killed") == 1
    assert [a.event_id for a in alertq.claim(db, NOW, limit=5)] == ["high", "low"]


def test_candidates_from_a_publish_report_are_queued_once():
    db = queue_db()
    cands = [{"eventId": "e1", "score": 0.4, "title": "A", "body": "a", "ageH": 1.0, "headline": "H", "url": "u"},
             {"eventId": "e2", "score": 0.2, "title": "B", "body": "b", "ageH": 0.5}]
    assert alertq.enqueue_candidates(db, cands, NOW) == 2
    assert alertq.enqueue_candidates(db, cands, NOW + timedelta(minutes=5)) == 0
    s = alertq.stats(db, NOW + timedelta(minutes=30))
    assert s["queued"] == 2 and s["oldestQueuedMin"] == 30.0


def test_after_a_restore_older_stories_are_not_queued_again():
    db = queue_db()
    db.set_meta(alertq.FLOOR_KEY, iso(NOW))
    assert alertq.enqueue(db, "old", "T", "B", NOW, published_at=NOW - timedelta(hours=1)) == "before_restore"
    assert alertq.enqueue(db, "new", "T", "B", NOW, published_at=NOW + timedelta(minutes=5)) == "queued"
    assert alertq.enqueue_candidates(db, [{"eventId": "e", "title": "T", "body": "B", "ageH": 2}],
                                     NOW + timedelta(hours=1)) == 0


def test_the_queue_survives_kill_9_with_no_loss_and_no_duplicate(tmp_path):
    """A real process is killed (SIGKILL: no cleanup, no commit) after queuing three stories, sending one and
    being half-way through sending another."""
    path = str(tmp_path / "state.db")
    child = textwrap.dedent(f"""
        import os, signal, sys
        sys.path.insert(0, {SERVER!r})
        from datetime import datetime, timezone
        from worker import alertq
        from worker.db import DB
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        db = DB({path!r})
        alertq.ensure(db)
        for i, score in enumerate((0.9, 0.8, 0.1)):
            alertq.enqueue(db, f"ev{{i}}", "T", "B", now, score=score)
        db.commit()
        (a,) = alertq.claim(db, now)
        alertq.mark_sent(db, a.id, now)             # ev0: sent
        alertq.claim(db, now)                        # ev1: handed to the sender, which never finishes
        alertq.enqueue(db, "uncommitted", "T", "B", now)   # the tick that queued this never committed
        os.kill(os.getpid(), signal.SIGKILL)
    """)
    r = subprocess.run([sys.executable, "-c", child])
    assert r.returncode == -9
    assert resilience.quick_check(path) is None
    db = queue_db(path)
    assert rows(db) == [("ev0", "sent"), ("ev1", "sending"), ("ev2", "queued")]
    # The restarted worker's publish lists the same stories again: nothing is queued twice.
    assert [alertq.enqueue(db, f"ev{i}", "T", "B", NOW) for i in range(3)] == ["duplicate"] * 3
    # The waiting story goes out once; the interrupted one is offered again only after its lease, as a redelivery.
    (a,) = alertq.claim(db, NOW + timedelta(minutes=1))
    assert (a.event_id, a.redelivery) == ("ev2", False)
    alertq.mark_sent(db, a.id, NOW)
    (b,) = alertq.claim(db, NOW + alertq.LEASE + timedelta(minutes=1))
    assert (b.event_id, b.redelivery) == ("ev1", True)
    alertq.mark_sent(db, b.id, NOW)
    assert alertq.claim(db, NOW + timedelta(hours=1)) == []
    assert db.one("SELECT COUNT(*) n FROM alert_queue")["n"] == 3


# Alerts

class FakeResponse:
    def __init__(self, body=b'{"id":"abc123"}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body


def test_ntfy_reports_whether_the_message_was_accepted(monkeypatch):
    seen = []

    def ok(req, timeout):
        seen.append((req.full_url, dict(req.header_items()), req.data))
        return FakeResponse()

    def down(req, timeout):
        raise OSError("unreachable")

    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert resilience.ntfy("T", "m", opener=ok) is False and not seen
    monkeypatch.setenv("NTFY_TOPIC", "topic-x")
    assert resilience.ntfy("Techgist: café — down", "m", priority="high", opener=ok) is True
    url, headers, data = seen[0]
    assert url == "https://ntfy.sh/topic-x" and headers["Priority"] == "high" and data == b"m"
    assert headers["Title"].isascii()
    assert resilience.ntfy("T", "m", opener=down) is False


def test_the_topic_is_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("NTFY_TOPIC", "very-secret-topic")
    with caplog.at_level("WARNING"):
        resilience.ntfy("T", "m", opener=lambda req, timeout: FakeResponse())
        resilience.ntfy("T", "m", opener=lambda req, timeout: (_ for _ in ()).throw(OSError("x")))
    assert "very-secret-topic" not in caplog.text and "accepted" in caplog.text


def test_alert_is_rate_limited_and_only_stamped_when_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path))
    sent = []
    fail = lambda title, message, priority=None: False                         # noqa: E731
    ok = lambda title, message, priority=None: sent.append(title) or True      # noqa: E731
    assert resilience.alert("k", "T", "m", send=fail, now=1000) is False
    assert resilience.alert("k", "T", "m", send=ok, now=1001) is True          # the failed one did not start the clock
    assert resilience.alert("k", "T", "m", send=ok, now=1100) is False
    assert resilience.alert("other", "T", "m", send=ok, now=1100) is True
    assert resilience.alert("k", "T", "m", send=ok, now=1001 + 3600) is True
    assert len(sent) == 3


def test_health_notify_retries_an_alert_that_could_not_be_sent(monkeypatch):
    db = DB(":memory:")
    monkeypatch.setenv("NTFY_TOPIC", "t")
    monkeypatch.setattr(resilience, "ntfy", lambda *a, **k: False)
    assert health.notify(db, "no_publish", "m") is False
    assert db.get_meta("alert:no_publish") is None
    calls = []
    monkeypatch.setattr(resilience, "ntfy", lambda title, message, priority=None: calls.append(priority) or True)
    assert health.notify(db, "no_publish", "m") is True and calls == ["high"]
    assert health.notify(db, "no_publish", "m") is False and len(calls) == 1       # once per 6 h
    assert health.notify(db, "source_failing:x", "m") is True and calls[-1] is None


def test_health_notify_without_a_topic_logs_once(monkeypatch):
    db = DB(":memory:")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert health.notify(db, "top_low", "m") is True
    assert health.notify(db, "top_low", "m") is False


def test_crash_loop_alerts_once_per_interval_and_counts_the_rest(tmp_path, monkeypatch):
    d = str(tmp_path)
    assert alert_cli.limited("unit-x", 900, now=1000, directory=d) == (True, 0)
    assert alert_cli.limited("unit-x", 900, now=1010, directory=d) == (False, 0)
    assert alert_cli.limited("unit-x", 900, now=1020, directory=d) == (False, 1)
    assert alert_cli.limited("unit-x", 900, now=1000 + 900, directory=d) == (True, 2)
    alert_cli.forget("unit-x", directory=d)
    assert alert_cli.limited("unit-x", 900, now=1901, directory=d) == (True, 0)


def test_unit_failure_message_says_what_happened(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("MONITOR_SERVICE_RESULT", "signal")
    monkeypatch.setenv("MONITOR_EXIT_STATUS", "KILL")
    monkeypatch.setattr(alert_cli, "send", lambda title, msg, priority=None, tags=None: sent.append((title, msg)) or True)
    assert alert_cli.unit_failed("techgist-worker.service", 900) is True
    assert "was killed by KILL" in sent[0][1] and "restarts it automatically" in sent[0][1]
    assert alert_cli.unit_failed("techgist-worker.service", 900) is True and len(sent) == 1
    monkeypatch.setenv("MONITOR_SERVICE_RESULT", "watchdog")
    later = time.time() + 1000
    monkeypatch.setattr(alert_cli.time, "time", lambda: later)
    alert_cli.unit_failed("techgist-worker.service", 900)
    assert "stopped answering" in sent[1][1] and "1 more failures" in sent[1][1]


# The systemd heartbeat

def test_heartbeat_reaches_the_notify_socket(monkeypatch):
    # A short path: a unix socket's is limited to about 100 bytes and pytest's tmp_path is longer on macOS.
    tmp_path = Path(tempfile.mkdtemp(prefix="tg", dir="/tmp"))
    path = str(tmp_path / "notify")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    server.settimeout(2)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert resilience.heartbeat() is True
        assert server.recv(100) == b"WATCHDOG=1"
        monkeypatch.delenv("NOTIFY_SOCKET")
        assert resilience.heartbeat() is False
        monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone"))
        assert resilience.heartbeat() is False                   # never raises into the tick
    finally:
        server.close()
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_every_tick_stage_is_followed_by_a_heartbeat():
    src = open(os.path.join(SERVER, "worker", "main.py")).read()
    tick = src[src.index("def tick("):src.index("def main(")]
    for stage in ("maybe_check_sources(db, srcs, cfg)", "fetch_due(db, srcs, cfg, poll_all)", "llm.write_pending(",
                  "cluster.run(", "publish_v1.publish("):
        after = tick[tick.index(stage):]
        assert "resilience.heartbeat()" in after[:900], stage


# A corrupt, locked or full database

def good_db(path, n=3):
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE t (x)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n)])
    c.commit()
    c.close()


def corrupt(path):
    with open(path, "wb") as f:
        f.write(b"SQLite format 3\x00" + os.urandom(8192))


def test_quick_check_tells_a_sound_file_from_a_broken_one(tmp_path):
    p = tmp_path / "state.db"
    assert resilience.quick_check(str(p)) is None                 # missing: a fresh state
    good_db(p)
    assert resilience.quick_check(str(p)) is None
    corrupt(p)
    assert resilience.quick_check(str(p))


def test_a_corrupt_state_is_set_aside_and_the_newest_good_backup_restored(tmp_path):
    state = tmp_path
    (state / "backups" / "nightly").mkdir(parents=True)
    good_db(state / "backups" / "nightly" / "state-20261114T034000Z.db", n=5)
    good_db(state / "backups" / "state-20261115T211500Z.db", n=7)             # hand-made, newer: preferred
    corrupt(state / "backups" / "nightly" / "state-20261116T034000Z.db")      # newest, but broken: skipped
    p = state / "state.db"
    corrupt(p)
    told = []
    done = resilience.recover_state(str(p), str(state), stamp="X",
                                    notify=lambda key, title, msg, **k: told.append((key, msg)))
    assert done == "restored:state-20261115T211500Z.db"
    c = sqlite3.connect(str(p))
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 7
    assert c.execute("SELECT value FROM meta WHERE key = 'alertq_floor'").fetchone()[0]
    assert (state / "state.db.corrupt-X").exists()
    assert told and told[0][0] == "db_corrupt" and "state-20261115T211500Z.db" in told[0][1]
    # A second start finds a sound file and does nothing.
    assert resilience.recover_state(str(p), str(state), notify=lambda *a, **k: told.append("again")) is None
    assert len(told) == 1


def test_a_corrupt_state_with_no_backup_starts_fresh_and_the_worker_schema_opens(tmp_path):
    p = tmp_path / "state.db"
    corrupt(p)
    told = []
    assert resilience.recover_state(str(p), str(tmp_path), stamp="X",
                                    notify=lambda key, title, msg, **k: told.append(msg)) == "fresh"
    assert "empty state" in told[0]
    db = DB(str(p))
    assert db.get_meta(alertq.FLOOR_KEY)
    db.close()


class FakeDB:
    class conn:
        rolled_back = 0

        @classmethod
        def rollback(cls):
            cls.rolled_back += 1


def guard(told, **kw):
    return resilience.TickGuard("/nonexistent-state", notify=lambda key, title, msg, **k: told.append(key), **kw)


def test_one_failed_tick_is_retried_and_five_end_the_process():
    told = []
    g = guard(told)
    for _ in range(resilience.MAX_FAILED_TICKS - 1):
        assert g.failure(FakeDB, ValueError("bad sources.yaml")) is None
    g.ok()                                                        # a good tick starts the count again
    for _ in range(resilience.MAX_FAILED_TICKS - 1):
        assert g.failure(FakeDB, sqlite3.OperationalError("database is locked")) is None
    assert g.failure(FakeDB, sqlite3.OperationalError("database is locked")) == resilience.EXIT_TICKS_FAILING
    assert told == ["ticks_failing"] and FakeDB.conn.rolled_back >= 9


def test_a_corrupt_database_ends_the_process_at_once():
    told = []
    assert guard(told).failure(FakeDB, sqlite3.DatabaseError("database disk image is malformed")) \
        == resilience.EXIT_DB_CORRUPT
    assert told == ["db_corrupt_seen"]


def test_a_full_disk_alerts_at_once_and_keeps_trying():
    told = []
    g = guard(told)
    for exc in (OSError(28, "No space left on device"), sqlite3.OperationalError("database or disk is full")):
        for _ in range(4):
            assert g.failure(FakeDB, exc) is None
    assert set(told) == {"disk_full"} and len(told) == 8          # alert() itself limits these to one an hour


def test_failure_kinds_are_told_apart():
    full = sqlite3.OperationalError("database or disk is full")
    locked = sqlite3.OperationalError("database is locked")
    bad = sqlite3.DatabaseError("file is not a database")
    assert resilience.is_disk_full(full) and not resilience.is_corruption(full) and not resilience.is_locked(full)
    assert resilience.is_locked(locked) and not resilience.is_corruption(locked)
    assert resilience.is_corruption(bad) and not resilience.is_disk_full(bad)
    assert not resilience.is_disk_full(OSError(13, "denied")) and not resilience.is_corruption(ValueError("x"))


def test_a_real_locked_database_raises_what_the_guard_calls_locked(tmp_path):
    p = str(tmp_path / "s.db")
    a = sqlite3.connect(p, isolation_level=None)
    a.execute("CREATE TABLE t (x)")
    a.execute("BEGIN IMMEDIATE")
    b = sqlite3.connect(p, timeout=0.1)
    with pytest.raises(sqlite3.OperationalError) as e:
        b.execute("INSERT INTO t VALUES (1)")
    assert resilience.is_locked(e.value)


def test_a_failed_tick_rolls_back_its_partial_writes(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    db.set_meta("kept", "1")
    db.commit()
    db.set_meta("half", "1")                                      # the tick died after this
    assert resilience.TickGuard(str(tmp_path), notify=lambda *a, **k: True).failure(db, ValueError("x")) is None
    db.commit()                                                   # the next tick's commit
    assert db.get_meta("kept") == "1" and db.get_meta("half") is None


def test_disk_low_names_the_free_space(monkeypatch):
    monkeypatch.setattr(resilience, "disk_free", lambda path: (200 << 20, 0.002))
    assert "200 MB free" in resilience.disk_low("/x")
    monkeypatch.setattr(resilience, "disk_free", lambda path: (80 << 30, 0.9))
    assert resilience.disk_low("/x") is None


# Pruning

def test_prune_drops_old_logs_and_heavy_text_but_keeps_what_is_known(tmp_path):
    db = queue_db(tmp_path / "s.db")
    old, mid, new = NOW - timedelta(days=40), NOW - timedelta(days=10), NOW - timedelta(hours=1)
    for at in (old, mid, new):
        db.log_poll(at, "src", 200, 1, 0, 5, None)
        db.log_drop(at, "src", "u", "t", "rule")
        db.log_write(at, "u", "write", "m", "ok")
        db.conn.execute("INSERT INTO publishes VALUES (?,?,?,?,?,?,?)", (iso(at), 1, None, 10, 5, "sha", None))
    for name, at in (("old", old), ("new", new)):
        db.insert_item({"url_canonical": name, "url": name, "source_id": "s", "title": "T", "feed_text": "long text",
                        "first_seen_at": at, "published_at": at, "status": "written"})
        db.conn.execute("INSERT INTO writes (url_canonical, status, written_at, text, output, steps, headline)"
                        " VALUES (?,?,?,?,?,?,?)", (name, "ok", iso(at), "article", "{}", "[]", "H"))
        db.conn.execute("INSERT INTO embeddings VALUES (?,?,?,?,?,?,?)", (name, "passage", "m", 1, "sha", b"v", iso(at)))
    alertq.enqueue(db, "waiting", "T", "B", NOW - timedelta(days=60))
    alertq.enqueue(db, "done", "T", "B", NOW - timedelta(days=60))
    db.conn.execute("UPDATE alert_queue SET status = 'sent' WHERE event_id = 'done'")
    done, more = maintenance.prune(db, {}, NOW)
    assert not more
    assert done == {"polls": 2, "prefilter_log": 2, "write_log": 1, "publishes": 1, "embeddings": 1,
                    "items_text": 1, "writes_text": 1, "alert_queue": 1}
    # The old item is still known (its URL is what "already seen" means) and keeps its card, without the bulk.
    r = db.one("SELECT i.feed_text, w.text, w.output, w.steps, w.headline FROM items i JOIN writes w USING"
               " (url_canonical) WHERE i.url_canonical = 'old'")
    assert tuple(r) == (None, None, None, None, "H")
    assert db.one("SELECT feed_text FROM items WHERE url_canonical = 'new'")["feed_text"] == "long text"
    assert rows(db) == [("waiting", "queued")]
    assert maintenance.prune(db, {}, NOW) == ({}, False)          # nothing left to do


def test_prune_never_drops_the_newest_good_publish():
    db = queue_db()
    db.conn.execute("INSERT INTO publishes VALUES (?,?,?,?,?,?,?)",
                    (iso(NOW - timedelta(days=90)), 1, None, 10, 5, "sha", None))
    db.conn.execute("INSERT INTO publishes VALUES (?,?,?,?,?,?,?)",
                    (iso(NOW - timedelta(days=80)), 0, None, 10, 5, "sha", "rejected"))
    maintenance.prune(db, {}, NOW)
    assert [r["ok"] for r in db.q("SELECT ok FROM publishes")] == [1]
    assert health.edition_stats(db, NOW, None)["lastPublishAt"] == iso(NOW - timedelta(days=90))


def test_prune_is_bounded_per_pass_and_finishes_on_later_ticks(monkeypatch):
    db = queue_db()
    monkeypatch.setattr(maintenance, "BATCH", 10)
    for i in range(25):
        db.log_poll(NOW - timedelta(days=30), f"s{i}", 200, 1, 0, 5, None)
    assert maintenance.run(db, {}, NOW) == {"polls": 10}
    assert db.get_meta(maintenance.META_KEY) is None              # not finished: runs again next tick
    assert maintenance.run(db, {}, NOW) == {"polls": 10}
    assert maintenance.run(db, {}, NOW) == {"polls": 5}
    assert db.get_meta(maintenance.META_KEY) == iso(NOW)
    db.log_poll(NOW - timedelta(days=30), "late", 200, 1, 0, 5, None)
    assert maintenance.run(db, {}, NOW + timedelta(hours=1)) == {}                # once a day
    assert maintenance.run(db, {}, NOW + timedelta(hours=25)) == {"polls": 1}


# Backups

def test_backup_is_a_complete_checked_copy_taken_while_the_worker_writes(tmp_path):
    state = tmp_path
    db = DB(str(state / "state.db"))
    db.set_meta("committed", "1")
    db.commit()
    db.set_meta("in_flight", "1")                                 # the worker is mid-tick: an open transaction
    assert backup_state.main(["--state", str(state)]) == 0
    (name,) = os.listdir(state / "backups" / "nightly")
    copy = sqlite3.connect(str(state / "backups" / "nightly" / name))
    assert dict(copy.execute("SELECT key, value FROM meta").fetchall()) == {"committed": "1"}
    assert backup_state.check(str(state / "backups" / "nightly" / name)) is None
    assert not [n for n in os.listdir(state / "backups" / "nightly") if n.endswith((".tmp", "-wal"))]
    # It is what recover_state() would restore from.
    assert resilience.backups(str(state)) == [str(state / "backups" / "nightly" / name)]


def test_backup_rotation_keeps_the_newest_and_leaves_other_files_alone(tmp_path):
    root = tmp_path / "backups"
    (root / "nightly").mkdir(parents=True)
    (root / "run1-20261115T180145Z").mkdir()
    (root / "edition-20261115T211500Z.json").write_text("{}")
    for d in range(1, 11):
        (root / "nightly" / f"state-202611{d:02d}T034000Z.db").write_text("x")
    for d in range(1, 8):
        (root / f"state-202610{d:02d}T000000Z.db").write_text("x")
    (root / "state-20261001T000000Z.db-wal").write_text("x")
    assert len(backup_state.rotate(str(root / "nightly"), 7)) == 3
    assert sorted(os.listdir(root / "nightly"))[0] == "state-20261104T034000Z.db"
    assert backup_state.rotate(str(root), 5) == ["state-20261001T000000Z.db", "state-20261002T000000Z.db"]
    left = set(os.listdir(root))
    assert {"run1-20261115T180145Z", "edition-20261115T211500Z.json", "nightly"} <= left
    assert "state-20261001T000000Z.db-wal" not in left and len(left) == 8


def test_a_backup_that_cannot_be_made_exits_nonzero(tmp_path):
    corrupt(tmp_path / "state.db")
    assert backup_state.main(["--state", str(tmp_path)]) == 1
    assert os.listdir(tmp_path / "backups" / "nightly") == []


# The freshness check and the watchdog

def edition(tmp_path, minutes_ago, name="edition.json", health_shape=False):
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = tmp_path / name
    p.write_text(json.dumps({"edition": {"lastPublishAt": at}} if health_shape else {"publishedAt": at}))
    return str(p)


def test_exit_status_is_0_fresh_1_stale_2_unreadable(tmp_path, capsys):
    assert check_freshness.main([edition(tmp_path, 3)]) == 0
    assert check_freshness.main([edition(tmp_path, 3, "health.json", health_shape=True)]) == 0
    assert check_freshness.main([edition(tmp_path, 40)]) == 1
    assert check_freshness.main([edition(tmp_path, 40, "health.json", health_shape=True)]) == 1
    assert check_freshness.main([str(tmp_path / "missing.json")]) == 2
    (tmp_path / "bad.json").write_text("<html>502</html>")
    assert check_freshness.main([str(tmp_path / "bad.json")]) == 2
    (tmp_path / "list.json").write_text("[]")
    assert check_freshness.main([str(tmp_path / "list.json")]) == 2
    assert check_freshness.main([edition(tmp_path, 3), "--quiet"]) == 0
    assert capsys.readouterr().out.count("fresh") == 2


class Ran:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_watchdog_restarts_a_silent_worker_and_tells_the_owner_once(tmp_path):
    state = str(tmp_path / "wd.json")
    cmds, sent = [], []
    run = lambda cmd, **k: cmds.append(cmd) or Ran()                                 # noqa: E731
    send = lambda title, msg, priority=None, tags=None: sent.append(title) or True   # noqa: E731
    kw = dict(state_path=state, run=run, send=send)
    # Alive for an hour, nothing published for 16 min: restarted.
    assert check_freshness.watchdog("16 min old", "techgist-worker", 15, now=1000, uptime=lambda u: 3600, **kw) \
        == "restarted"
    assert cmds == [["systemctl", "restart", "techgist-worker"]] and len(sent) == 1
    # The next minutes: the worker is young, so it is left to publish and nobody is told again.
    for t in (1060, 1120, 1180):
        assert check_freshness.watchdog("stale", "techgist-worker", 15, now=t, uptime=lambda u: t - 1000, **kw) \
            == "waiting"
    assert len(cmds) == 1 and len(sent) == 1
    # Still silent 15 min after that restart: restarted again, and said so.
    assert check_freshness.watchdog("stale", "techgist-worker", 15, now=1960, uptime=lambda u: 960, **kw) == "restarted"
    assert len(cmds) == 2 and len(sent) == 2
    assert json.load(open(state))["restarts"] == 2
    # Fresh again: one "publishing again" message, and the state is cleared.
    assert check_freshness.recovered(2, state, now=2500, send=send) is True
    assert sent[-1] == "Techgist: publishing again" and not os.path.exists(state)
    assert check_freshness.recovered(2, state, send=send) is False and len(sent) == 3


def test_watchdog_starts_a_worker_that_is_not_running_and_reports_a_failed_restart(tmp_path):
    sent = []
    send = lambda title, msg, priority=None, tags=None: sent.append((msg, priority)) or True     # noqa: E731
    done = check_freshness.watchdog("stale", "techgist-worker", 15, state_path=str(tmp_path / "wd.json"), now=1,
                                    uptime=lambda u: None, run=lambda cmd, **k: Ran(1, stderr="unit masked"),
                                    send=send)
    assert done == "restart_failed"
    assert "was not running" in sent[0][0] and "FAILED: unit masked" in sent[0][0] and sent[0][1] == "urgent"


def test_watchdog_reminds_while_a_young_worker_stays_stale(tmp_path):
    sent = []
    kw = dict(state_path=str(tmp_path / "wd.json"), uptime=lambda u: 60, run=lambda cmd, **k: Ran(),
              send=lambda title, msg, priority=None, tags=None: sent.append(title) or True)
    # After a boot the edition is old and the worker has only just started: told once, not restarted.
    assert check_freshness.watchdog("stale", "u", 15, now=10_000, **kw) == "waiting"
    assert check_freshness.watchdog("stale", "u", 15, now=10_060, **kw) == "waiting"
    assert len(sent) == 1
    assert check_freshness.watchdog("stale", "u", 15, now=10_000 + check_freshness.REMIND_S, **kw) == "waiting"
    assert len(sent) == 2


def test_unit_uptime_reads_systemctl():
    out = f"ActiveState=active\nActiveEnterTimestampMonotonic={int((time.monotonic() - 120) * 1e6)}\n"
    assert 119 <= check_freshness.unit_uptime_s("u", run=lambda cmd, **k: Ran(stdout=out)) <= 122
    out = "ActiveState=activating\nActiveEnterTimestampMonotonic=5\n"
    assert check_freshness.unit_uptime_s("u", run=lambda cmd, **k: Ran(stdout=out)) is None


# The unit files and the workflow

def unit(name):
    lines = [ln.strip() for ln in open(os.path.join(DEPLOY, name)) if ln.strip() and not ln.startswith("#")]
    return dict(ln.split("=", 1) for ln in lines if "=" in ln)


def test_the_worker_unit_never_gives_up_and_starts_at_boot():
    u = unit("techgist-worker.service")
    assert u["Restart"] == "always" and u["StartLimitIntervalSec"] == "0"
    assert int(u["RestartSec"]) <= 15 and int(u["RestartMaxDelaySec"]) <= 120 and "RestartSteps" in u
    assert u["WantedBy"] == "multi-user.target"
    assert int(u["WatchdogSec"]) <= 900 and u["NotifyAccess"] == "main"
    assert u["OnFailure"] == "techgist-alert@%n.service"
    assert u["EnvironmentFile"] == "-/etc/techgist/env"


def test_the_watchdog_checks_every_minute_with_a_15_minute_window():
    assert "--max-age-min 15 --restart techgist-worker" in unit("techgist-watchdog.service")["ExecStart"]
    t = unit("techgist-watchdog.timer")
    assert t["OnUnitActiveSec"] == "1min" and t["WantedBy"] == "timers.target" and "OnBootSec" in t
    b = unit("techgist-backup.timer")
    assert b["Persistent"] == "true" and unit("techgist-backup.service")["OnFailure"] == "techgist-alert@%n.service"


def test_no_topic_or_key_is_in_the_repository_files():
    for name in os.listdir(DEPLOY):
        text = open(os.path.join(DEPLOY, name)).read()
        assert "NTFY_TOPIC=tg" not in text and "ntfy.sh/tg" not in text, name


def test_the_github_workflow_is_manual_only():
    import yaml
    path = os.path.join(os.path.dirname(SERVER), ".github", "workflows", "edition-freshness.yml")
    wf = yaml.safe_load(open(path))
    triggers = wf.get("on", wf.get(True))                         # YAML reads the bare key `on` as True
    assert set(triggers) == {"workflow_dispatch"}
    assert "check_freshness.py" in wf["jobs"]["check"]["steps"][-1]["run"]


# The main loop

def run_main(monkeypatch, tmp_path, ticks):
    """Runs worker.main.main() with `ticks` as the tick function, no sleeping and no models, until it exits."""
    from worker import config, embed, main
    cfg = config.load()
    cfg["state_dir"] = str(tmp_path)
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(main, "make_client", lambda cfg: None)
    monkeypatch.setattr(embed, "make", lambda cfg, key: None)
    monkeypatch.setattr(main, "tick", ticks)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["worker.main", "--no-publish"])
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(tmp_path / "run"))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    with pytest.raises(SystemExit) as e:
        main.main()
    return e.value.code


def test_the_loop_survives_bad_ticks_and_exits_for_a_clean_restart_when_they_do_not_stop(monkeypatch, tmp_path):
    calls = []

    def ticks(db, *a, **k):
        calls.append(1)
        n = len(calls)
        if n in (1, 2):
            raise ValueError("a bad sources.yaml edit")           # two bad ticks, then a good one: carries on
        if n == 3:
            return {}
        db.set_meta("half_done", "1")
        raise sqlite3.OperationalError("database is locked")

    assert run_main(monkeypatch, tmp_path, ticks) == resilience.EXIT_TICKS_FAILING
    assert len(calls) == 3 + resilience.MAX_FAILED_TICKS
    db = DB(str(tmp_path / "state.db"))
    assert db.get_meta("half_done") is None and db.get_meta("worker_started_at")


def test_the_loop_exits_at_once_on_a_corrupt_database_and_the_next_start_restores_a_backup(monkeypatch, tmp_path):
    good = DB(str(tmp_path / "state.db"))
    good.set_meta("from_backup", "1")
    good.commit()
    good.close()
    assert backup_state.main(["--state", str(tmp_path)]) == 0
    calls = []

    def ticks(db, *a, **k):
        calls.append(1)
        raise sqlite3.DatabaseError("database disk image is malformed")

    assert run_main(monkeypatch, tmp_path, ticks) == resilience.EXIT_DB_CORRUPT and len(calls) == 1
    # What systemd does next: start the worker again. This time the file really is broken on disk.
    for suffix in ("-wal", "-shm"):
        if os.path.exists(str(tmp_path / "state.db") + suffix):
            os.remove(str(tmp_path / "state.db") + suffix)
    corrupt(tmp_path / "state.db")
    seen = []

    def one_good_tick(db, *a, **k):
        seen.append(db.get_meta("from_backup"))
        raise SystemExit(0)

    assert run_main(monkeypatch, tmp_path, one_good_tick) == 0
    assert seen == ["1"]
    assert [n for n in os.listdir(tmp_path) if n.startswith("state.db.corrupt-")]


def test_sqlite_running_out_of_room_is_recognised_as_a_full_disk(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    db.conn.execute(f"PRAGMA max_page_count = {db.one('PRAGMA page_count')[0] + 2}")
    with pytest.raises(sqlite3.DatabaseError) as e:
        for i in range(2000):
            db.set_meta(f"k{i}", "x" * 500)
    assert resilience.is_disk_full(e.value) and not resilience.is_corruption(e.value)
    told = []
    g = resilience.TickGuard(str(tmp_path), notify=lambda key, title, msg, **k: told.append((key, msg)))
    assert g.failure(db, e.value) is None and told[0][0] == "disk_full" and "MB free" in told[0][1]
    db.commit()
    assert db.get_meta("k0") is None                              # the failed tick's writes were rolled back


def test_a_file_that_passes_its_checks_but_keeps_failing_ticks_is_replaced_on_the_third_exit(tmp_path):
    p = tmp_path / "state.db"
    good_db(p)
    told = []
    g = resilience.TickGuard(str(tmp_path), notify=lambda key, title, msg, **k: told.append(key))
    bad = sqlite3.DatabaseError("database disk image is malformed")
    for start in (1, 2):
        assert g.failure(FakeDB, bad) == resilience.EXIT_DB_CORRUPT
        assert resilience.suspects(str(p)) == start
        # The full check passes, so the file is kept and the worker tries again.
        assert resilience.recover_state(str(p), str(tmp_path), notify=lambda *a, **k: told.append("recovered")) is None
    assert g.failure(FakeDB, bad) == resilience.EXIT_DB_CORRUPT
    assert resilience.recover_state(str(p), str(tmp_path), stamp="X",
                                    notify=lambda key, title, msg, **k: told.append(msg)) == "fresh"
    assert "3 starts in a row" in told[-1] and (tmp_path / "state.db.corrupt-X").exists()
    assert resilience.suspects(str(p)) == 0


def test_a_good_tick_clears_the_suspicion(tmp_path):
    p = tmp_path / "state.db"
    good_db(p)
    g = resilience.TickGuard(str(tmp_path), notify=lambda *a, **k: True)
    g.failure(FakeDB, sqlite3.DatabaseError("database disk image is malformed"))
    assert resilience.suspects(str(p)) == 1
    g.ok()
    assert resilience.suspects(str(p)) == 0
