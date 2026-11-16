"""push/api.py and push/store.py: registration, validation, rate limits, and what never leaves the service."""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from push import api, config
from push.store import Store, dkey

TOKEN = "ab12cd" + "0" * 58
NOW = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)


def body(**over):
    b = {"token": TOKEN, "env": "sandbox", "tz": "Asia/Kolkata", "locale": "en_IN", "appVersion": "2.0"}
    b.update(over)
    return json.dumps({k: v for k, v in b.items() if v is not None}).encode()


@pytest.fixture
def app(tmp_path):
    cfg = config.load(str(tmp_path))
    return api.App(cfg, lambda: Store(cfg["paths"]["push_db"]), clock=lambda: NOW)


def test_register_stores_the_device_and_answers_without_the_token(app):
    status, out = app.handle("POST", "/v1/devices", body(topics=["AI", "Gadgets"]), "1.2.3.4")
    assert (status, out) == (200, {"ok": True})
    d = app.store().devices()[0]
    assert (d["token"], d["env"], d["tz"], d["locale"], d["app_version"]) == (TOKEN, "sandbox", "Asia/Kolkata",
                                                                              "en_IN", "2.0")
    assert json.loads(d["topics"]) == ["AI", "Gadgets"] and d["owner"] == 0


def test_registering_again_updates_and_keeps_the_owner_flag(app):
    app.handle("POST", "/v1/devices", body(), "1.2.3.4")
    app.store().set_owner(TOKEN)
    app.handle("POST", "/v1/devices", body(token=TOKEN.upper(), env="production", tz="Europe/London"), "1.2.3.4")
    rows = app.store().devices()
    assert len(rows) == 1 and (rows[0]["env"], rows[0]["tz"], rows[0]["owner"]) == ("production", "Europe/London", 1)


@pytest.mark.parametrize("over,field", [
    ({"token": "abc"}, "token"), ({"token": "z" * 64}, "token"), ({"token": "a" * 63}, "token"),
    ({"token": "a" * 201}, "token"), ({"token": 5}, "token"), ({"token": None}, "token"),
    ({"env": "prod"}, "env"), ({"env": None}, "env"),
    ({"tz": "x" * 65}, "tz"), ({"tz": "Asia/Kol kata"}, "tz"), ({"tz": None}, "tz"),
    ({"topics": "AI"}, "topics"), ({"topics": ["a"] * 33}, "topics"), ({"topics": ["x" * 33]}, "topics"),
])
def test_bad_input_is_refused_not_trimmed(app, over, field):
    status, out = app.handle("POST", "/v1/devices", body(**over), "1.2.3.4")
    assert status == 400 and out["field"] == field
    assert app.store().device_count() == 0


@pytest.mark.parametrize("over,locale,version", [
    ({"locale": "en_US@rg=inzzzz"}, "en_US", "2.0"),                       # what an iPhone with a region override sends
    ({"locale": "en_IN@calendar=gregorian;numbers=latn"}, "en_IN", "2.0"),
    ({"locale": "x" * 36}, None, "2.0"), ({"locale": 7}, None, "2.0"),
    ({"appVersion": "<script>"}, "en_IN", None),
])
def test_a_device_is_never_refused_over_its_locale_or_version(app, over, locale, version):
    status, out = app.handle("POST", "/v1/devices", body(**over), "1.2.3.4")
    assert status == 200 and out == {"ok": True}
    row = app.store().devices()[0]
    assert (row["locale"], row["app_version"]) == (locale, version)


def test_garbage_bodies(app):
    for raw in (b"", b"not json", b"[1,2]", b"\xff\xfe", b'"token"'):
        assert app.handle("POST", "/v1/devices", raw, "1.2.3.4")[0] == 400


def test_delete_opts_out_and_answers_the_same_for_an_unknown_token(app):
    app.handle("POST", "/v1/devices", body(), "1.2.3.4")
    assert app.handle("DELETE", "/v1/devices/" + TOKEN, b"", "1.2.3.4") == (200, {"ok": True})
    assert app.store().device_count() == 0
    assert app.handle("DELETE", "/v1/devices/" + "f" * 64, b"", "1.2.3.4") == (200, {"ok": True})
    assert app.handle("DELETE", "/v1/devices/nothex", b"", "1.2.3.4")[0] == 400


def test_there_is_no_listing_or_lookup(app):
    app.handle("POST", "/v1/devices", body(), "1.2.3.4")
    for method, path in (("GET", "/v1/devices"), ("GET", "/v1/devices/" + TOKEN), ("PUT", "/v1/devices")):
        status, out = app.handle(method, path, b"", "1.2.3.4")
        assert status == 405 and TOKEN not in json.dumps(out)
    assert app.handle("GET", "/v1/other", b"", "1.2.3.4")[0] == 404
    assert app.handle("GET", "/", b"", "1.2.3.4")[0] == 404


def test_rate_limit_per_address_and_per_token(app):
    n = app.cfg["ip_per_min"]
    codes = [app.handle("DELETE", "/v1/devices/" + f"{i:064x}", b"", "9.9.9.9")[0] for i in range(n + 1)]
    assert codes[:n] == [200] * n and codes[n] == 429
    assert app.handle("DELETE", "/v1/devices/" + "a" * 64, b"", "8.8.8.8")[0] == 200      # another address is fine
    t = app.cfg["token_per_min"]
    codes = [app.handle("POST", "/v1/devices", body(), f"10.0.0.{i}")[0] for i in range(t + 1)]
    assert codes[:t] == [200] * t and codes[t] == 429


def test_rate_limit_window_passes():
    clock = [0.0]
    rl = api.RateLimit(2, clock=lambda: clock[0])
    assert [rl.allow("a"), rl.allow("a"), rl.allow("a")] == [True, True, False]
    clock[0] = 61
    assert rl.allow("a")


def test_registry_cap(tmp_path):
    cfg = config.load(str(tmp_path))
    cfg["api"]["max_devices"] = 1
    app = api.App(cfg, lambda: Store(cfg["paths"]["push_db"]), clock=lambda: NOW)
    assert app.handle("POST", "/v1/devices", body(), "1.1.1.1")[0] == 200
    assert app.handle("POST", "/v1/devices", body(token="c" * 64), "1.1.1.2")[0] == 503
    assert app.handle("POST", "/v1/devices", body(), "1.1.1.3")[0] == 200          # a known device still refreshes


def test_health_says_only_ok(tmp_path):
    cfg = config.load(str(tmp_path))
    alive = [True]
    app = api.App(cfg, lambda: Store(cfg["paths"]["push_db"]), alive=lambda: alive[0])
    assert app.handle("GET", "/v1/health", b"", "1.1.1.1") == (200, {"ok": True})
    alive[0] = False
    assert app.handle("GET", "/v1/health", b"", "1.1.1.1") == (503, {"ok": False})


def test_the_log_line_never_carries_a_whole_token():
    assert api.masked("/v1/devices/" + TOKEN) == "/v1/devices/ab12cd…"
    assert TOKEN[:7] not in api.masked("/v1/devices/" + TOKEN + "?x=1")


def test_over_http_on_localhost_with_size_limit_and_masked_log(tmp_path):
    cfg = config.load(str(tmp_path))
    cfg["api"]["port"] = 0
    lines = []
    httpd = api.serve(cfg, lambda: Store(cfg["paths"]["push_db"]), lambda: True, lines.append)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert httpd.server_address[0] == "127.0.0.1"
        req = urllib.request.Request(base + "/v1/devices", data=body(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Real-IP": "5.5.5.5"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200 and json.loads(r.read()) == {"ok": True}
        with urllib.request.urlopen(urllib.request.Request(base + "/v1/devices/" + TOKEN, method="DELETE"),
                                    timeout=5) as r:
            assert r.status == 200
        big = urllib.request.Request(base + "/v1/devices", data=b"x" * 5000, method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(big, timeout=5)
        assert e.value.code == 413
        with urllib.request.urlopen(base + "/v1/health", timeout=5) as r:
            assert json.loads(r.read()) == {"ok": True}
        with urllib.request.urlopen(urllib.request.Request(base + "/v1/health", method="HEAD"), timeout=5) as r:
            assert r.status == 200 and r.read() == b"" and r.headers["Content-Type"] == "application/json"
    finally:
        httpd.shutdown()
    assert lines and all(TOKEN not in line and TOKEN[:7] not in line for line in lines)


def test_unregistering_deletes_the_token_but_the_story_is_still_not_repeated(tmp_path):
    s = Store(str(tmp_path / "push.db"))
    s.register(TOKEN, "sandbox", "UTC", None, None, None, NOW)
    s.record(TOKEN, "ev_a", "sent", NOW, "2026-11-16")
    s.remove(TOKEN)
    dump = "\n".join(str(tuple(r)) for t in ("devices", "deliveries", "alerts", "kv")
                     for r in s.q(f"SELECT * FROM {t}"))
    assert TOKEN not in dump
    s.register(TOKEN, "sandbox", "UTC", None, None, None, NOW)
    assert [e for e, *_ in s.history(NOW)[dkey(TOKEN)]] == ["ev_a"]


def test_two_connections_write_at_once(tmp_path):
    path = str(tmp_path / "push.db")
    Store(path)

    def work(i):
        s = Store(path)
        for j in range(20):
            s.register(f"{i:02x}{j:02x}" + "0" * 60, "sandbox", "UTC", None, None, None, NOW)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert Store(path).device_count() == 80


def test_registrations_from_one_address_are_limited_tighter_than_other_requests(app):
    n = app.cfg["ip_post_per_min"]
    assert n <= 10 < app.cfg["ip_per_min"]
    codes = [app.handle("POST", "/v1/devices", body(token=f"{i:064x}"), "7.7.7.7")[0] for i in range(n + 1)]
    assert codes[:n] == [200] * n and codes[n] == 429
    assert app.store().device_count() == n                              # the refused one was not stored
    assert app.handle("DELETE", "/v1/devices/" + f"{0:064x}", b"", "7.7.7.7")[0] == 200     # opting out still works
    assert app.handle("POST", "/v1/devices", body(), "7.7.7.8")[0] == 200                   # another address is fine


def test_head_on_health_answers_like_get_and_elsewhere_is_refused(app):
    assert app.handle("HEAD", "/v1/health", b"", "1.2.3.4") == (200, {"ok": True})
    assert app.handle("HEAD", "/v1/devices", b"", "1.2.3.4")[0] == 405
    assert app.handle("HEAD", "/", b"", "1.2.3.4")[0] == 404


def test_the_nightly_backup_is_a_whole_checked_copy_and_keeps_seven(tmp_path):
    import os
    import sqlite3
    from push import backup
    cfg = config.load(str(tmp_path))
    live = Store(cfg["paths"]["push_db"])
    live.register(TOKEN, "production", "Asia/Kolkata", "en_IN", "2.0", None, NOW)
    night = tmp_path / "backups" / "nightly"
    night.mkdir(parents=True)
    (night / "state-20261101T000000Z.db").write_text("the engine's; not ours to rotate")
    for day in range(9):
        assert backup.main(str(tmp_path), now=1794000000 + 86400 * day) == 0
    ours = sorted(n for n in os.listdir(night) if n.startswith("push-"))
    assert len(ours) == 7 and (night / "state-20261101T000000Z.db").exists()
    assert sorted(os.listdir(night)) == sorted(ours + ["state-20261101T000000Z.db"])     # nothing half-made left
    c = sqlite3.connect(str(night / ours[-1]))
    assert [r[0] for r in c.execute("SELECT token FROM devices")] == [TOKEN]
    assert os.stat(night / ours[-1]).st_mode & 0o777 == 0o640
    # No push.db to copy: exit 1 for the unit's OnFailure, and nothing written.
    assert backup.main(str(tmp_path / "empty")) == 1
    assert os.listdir(tmp_path / "empty" / "backups" / "nightly") == []
