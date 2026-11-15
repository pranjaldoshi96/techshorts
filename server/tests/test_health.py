"""Health metrics and alerts (spec section 7): the pass rate per item on its first run, LLM timeouts, importance
drift per story. Pure, in-memory SQLite."""
from datetime import datetime, timedelta, timezone

from worker import db as db_mod, health
from worker.db import DB, iso

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def item(db, at, url, outcome, attempt, reason=None):
    db.log_write(at, url, "item", "m", outcome, reason, attempt=attempt)


def test_pass_rate_counts_each_item_once_on_its_first_attempt():
    db = DB(":memory:")
    t = NOW - timedelta(minutes=30)
    for i in range(9):
        item(db, t, f"u{i}", "ok", 1)
    item(db, t, "bad", "failed", 1, "hedge_dropped")
    # The same bad item and two others coming back from hold: re-runs, not new failures.
    for u in ("x", "y", "z"):
        item(db, t, u, "failed", 2, "too_long")
        item(db, t, u, "failed", 3, "too_long")
    assert health.first_attempt_rate(db, NOW - timedelta(hours=1), NOW) == (9, 1)
    assert "pass_rate" not in health.alerts(db, NOW)
    v = health.validation_stats(db, NOW - timedelta(hours=24))
    assert v["passRate"] == 0.9 and v["retries"] == {"passed": 0, "failed": 6}


def test_pass_rate_alert_fires_on_new_items_failing():
    db = DB(":memory:")
    for i in range(10):
        item(db, NOW - timedelta(minutes=5), f"u{i}", "ok" if i < 7 else "failed", 1)
    assert "pass_rate" in health.alerts(db, NOW)


def test_llm_errors_are_not_validation_failures():
    db = DB(":memory:")
    for i in range(8):
        item(db, NOW - timedelta(minutes=5), f"u{i}", "ok", 1)
    for i in range(6):
        item(db, NOW - timedelta(minutes=5), f"t{i}", "llm_error", 1, "llm_error")
    assert "pass_rate" not in health.alerts(db, NOW)
    v = health.validation_stats(db, NOW - timedelta(hours=24))
    assert v["passRate"] == 1.0 and v["llmErrors"] == 6


def test_eventual_pass_rate_follows_items_through_their_retries():
    db = DB(":memory:")
    item(db, NOW - timedelta(hours=3), "a", "failed", 1)
    item(db, NOW - timedelta(hours=2), "a", "ok", 2)
    item(db, NOW - timedelta(hours=3), "b", "failed", 1)
    item(db, NOW - timedelta(hours=3), "c", "ok", 1)
    v = health.validation_stats(db, NOW - timedelta(hours=24))
    assert v["passRate"] == 0.333 and v["eventualPassRate"] == 0.667


def test_timeout_alert():
    db = DB(":memory:")
    for i in range(12):
        db.log_write(NOW - timedelta(minutes=i), None, "write", "m", "ReadTimeout")
    for i in range(20):
        db.log_write(NOW - timedelta(minutes=i), None, "write", "m", "ok")
    assert "llm_timeouts" in health.alerts(db, NOW)


def test_write_log_attempt_backfill_on_an_old_database(tmp_path):
    import sqlite3
    path = str(tmp_path / "state.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE write_log (at TEXT NOT NULL, url_canonical TEXT, kind TEXT NOT NULL, model TEXT,"
                 " outcome TEXT, reason TEXT, ms INTEGER)")
    for u, outcome in (("a", "failed"), ("b", "ok"), ("a", "failed"), ("a", "ok")):
        conn.execute("INSERT INTO write_log VALUES (?,?,?,?,?,?,?)", (iso(NOW), u, "item", "m", outcome, None, None))
    conn.execute("INSERT INTO write_log VALUES (?,?,?,?,?,?,?)", (iso(NOW), None, "write", "m", "ok", None, 5))
    conn.commit()
    conn.close()
    db = DB(path)
    rows = [(r["url_canonical"], r["attempt"]) for r in db.q("SELECT * FROM write_log ORDER BY rowid")]
    assert rows == [("a", 1), ("b", 1), ("a", 2), ("a", 3), (None, None)]


def _write(db, url, importance, status="ok", event=None):
    db.conn.execute("INSERT INTO writes (url_canonical, status, written_at, importance) VALUES (?,?,?,?)",
                    (url, status, iso(NOW - timedelta(hours=1)), importance))
    if event:
        db.conn.execute("INSERT INTO event_members VALUES (?,?,?,?,?)", (url, event, iso(NOW), "new", 1.0))


def test_importance_drift_is_per_story_over_validated_writes():
    db = DB(":memory:")
    for i in range(30):                         # one mega-story with 30 follow-ups at 9
        _write(db, f"huang{i}", 9, event="ev_huang")
    for i in range(25):                         # 25 ordinary stories
        _write(db, f"s{i}", 5)
    for i in range(10):                         # failed and thin rows don't count
        _write(db, f"f{i}", 9, status="failed" if i % 2 else "thin")
    d = health.importance_drift(db, NOW - timedelta(hours=24))
    assert d["stories"] == 26 and d["rawShareAtLeast8"] == round(1 / 26, 3)
    assert d["itemShareAtLeast8"] == round(30 / 55, 3)
    assert db.get_meta("alert:importance_drift") is None


def test_hold_schedule_depends_on_the_failure():
    db = DB(":memory:")
    for u in ("t", "c"):
        db.conn.execute("INSERT INTO items (url_canonical, url, source_id, title, first_seen_at, published_at, status)"
                        " VALUES (?,?,?,?,?,?,?)", (u, u, "s", "T", iso(NOW), iso(NOW), "new"))
    assert db.hold("t", "llm_error", NOW) == "held"
    assert db.hold("t", "llm_error", NOW) == "held"
    assert db.hold("t", "llm_error", NOW) == "dropped"
    assert db.hold("c", "hedge_dropped", NOW) == "held"
    assert db.item("c")["retry_at"] == iso(NOW + db_mod.CONTENT_RETRIES[0])
    assert db.hold("c", "hedge_dropped", NOW) == "dropped"
