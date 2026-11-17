"""Android in the push service: the registry's `platform`, push/fcm.py against a fake Google, and the sender
sending one story through APNs and FCM alike. No network, no real key."""
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from push import api, cli, config
from push import fcm as fcm_mod
from push.apns import Result
from push.fcm import Fcm
from push.sender import Sender
from push.store import Store, dkey
from test_push_sender import Env, FakeApns
from worker import alertq

NOW = datetime(2026, 11, 16, 6, 30, tzinfo=timezone.utc)        # noon in India
IOS = "ab12cd" + "0" * 58
IOS_B = "bbbbbb" + "2" * 58
# Shaped like an FCM registration token: an instance id, a colon, mixed case, "_" and "-".
FCM = "dQw4w9_XgcQ:APA91b" + "Hk-3_zT" * 20
FCM_B = "eRx5x0_YhdR:APA91b" + "Jm-4_aU" * 20


def body(**over):
    b = {"token": IOS, "env": "production", "tz": "Asia/Kolkata", "locale": "en_IN", "appVersion": "2.0"}
    b.update(over)
    return json.dumps({k: v for k, v in b.items() if v is not None}).encode()


@pytest.fixture
def app(tmp_path):
    cfg = config.load(str(tmp_path))
    return api.App(cfg, lambda: Store(cfg["paths"]["push_db"]), clock=lambda: NOW)


# Registry

def test_a_registration_without_platform_is_an_iphone_as_the_live_app_sends_it(app):
    assert app.handle("POST", "/v1/devices", body(), "1.2.3.4") == (200, {"ok": True})
    assert app.handle("POST", "/v1/devices", body(token="c" * 64, platform="ios"), "1.2.3.4")[0] == 200
    assert [d["platform"] for d in app.store().devices()] == ["ios", "ios"]


def test_an_android_device_registers_with_its_fcm_token_exactly_as_sent(app):
    assert app.handle("POST", "/v1/devices", body(token=FCM, platform="android", sound="wire"), "1.2.3.4") == \
        (200, {"ok": True})
    d = app.store().devices()[0]
    assert (d["token"], d["platform"], d["sound"], d["tz"]) == (FCM, "android", "wire", "Asia/Kolkata")
    assert FCM != FCM.lower()                                           # the capitals were kept
    # The same token again refreshes the one row.
    app.handle("POST", "/v1/devices", body(token=FCM, platform="android", tz="Europe/London"), "1.2.3.4")
    assert [(r["platform"], r["tz"]) for r in app.store().devices()] == [("android", "Europe/London")]


@pytest.mark.parametrize("platform", ["windows", "Android", "IOS", "", 5, ["android"]])
def test_an_unknown_platform_is_refused_by_name(app, platform):
    status, out = app.handle("POST", "/v1/devices", body(platform=platform), "1.2.3.4")
    assert status == 400 and out["field"] == "platform" and app.store().device_count() == 0


@pytest.mark.parametrize("over", [
    {"token": FCM},                                     # an FCM token that does not say it is Android
    {"token": FCM, "platform": "ios"},
    {"token": IOS, "platform": "android"},              # 64 characters: too short for FCM
    {"token": FCM[:99], "platform": "android"}, {"token": "a" * 1025, "platform": "android"},
    {"token": FCM + " ", "platform": "android"}, {"token": FCM[:120] + "/" + FCM[:20], "platform": "android"},
    {"token": 7, "platform": "android"},
])
def test_each_platform_keeps_its_own_token_rule(app, over):
    status, out = app.handle("POST", "/v1/devices", body(**over), "1.2.3.4")
    assert status == 400 and out["field"] == "token" and app.store().device_count() == 0


def test_the_iphone_rule_is_unchanged_hex_in_any_case_stored_lower(app):
    assert app.handle("POST", "/v1/devices", body(token=IOS.upper()), "1.2.3.4")[0] == 200
    assert [d["token"] for d in app.store().devices()] == [IOS]


def test_a_one_kilobyte_fcm_token_with_every_other_field_fits_in_a_body(tmp_path):
    cfg = config.load(str(tmp_path))
    big = "A" * 1024
    raw = body(token=big, platform="android", topics=["x" * 32] * 32, sound="chime")
    assert 2048 < len(raw) <= cfg["api"]["max_body"] == 4096
    app = api.App(cfg, lambda: Store(cfg["paths"]["push_db"]), clock=lambda: NOW)
    assert app.handle("POST", "/v1/devices", raw, "1.2.3.4")[0] == 200


def test_delete_takes_an_fcm_token_plain_or_percent_encoded_and_an_unknown_one_alike(app):
    for path in ("/v1/devices/" + FCM, "/v1/devices/" + urllib.parse.quote(FCM, safe="")):
        app.handle("POST", "/v1/devices", body(token=FCM, platform="android"), "1.2.3.4")
        assert "%3A" in urllib.parse.quote(FCM, safe="")
        assert app.handle("DELETE", path, b"", "1.2.3.4") == (200, {"ok": True})
        assert app.store().device_count() == 0
    assert app.handle("DELETE", "/v1/devices/" + FCM_B, b"", "1.2.3.4") == (200, {"ok": True})
    # Its capitals matter: another case is another token, and this one stays.
    app.handle("POST", "/v1/devices", body(token=FCM, platform="android"), "1.2.3.4")
    assert app.handle("DELETE", "/v1/devices/" + FCM.swapcase(), b"", "1.2.3.4") == (200, {"ok": True})
    assert app.store().device_count() == 1
    # An iPhone's token still goes in any case; nonsense is still refused.
    app.handle("POST", "/v1/devices", body(), "1.2.3.4")
    assert app.handle("DELETE", "/v1/devices/" + IOS.upper(), b"", "1.2.3.4") == (200, {"ok": True})
    assert [d["token"] for d in app.store().devices()] == [FCM]
    for bad in ("nothex", FCM[:99], FCM + "%2Fx", "%ff" * 40):
        assert app.handle("DELETE", "/v1/devices/" + bad, b"", "1.2.3.4")[0] == 400


def test_the_log_line_never_carries_more_than_six_characters_of_an_fcm_token():
    quoted = urllib.parse.quote(FCM, safe="")
    for path in ("/v1/devices/" + FCM, "/v1/devices/" + quoted, "/v1/devices/" + FCM + "?x=1"):
        line = api.masked(path)
        assert line.startswith("/v1/devices/dQw4w9…") and FCM[:7] not in line and len(line) <= 23


def test_over_http_an_android_device_registers_and_opts_out_and_the_log_is_masked(tmp_path):
    cfg = config.load(str(tmp_path))
    cfg["api"]["port"] = 0
    lines = []
    httpd = api.serve(cfg, lambda: Store(cfg["paths"]["push_db"]), lambda: True, lines.append)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        req = urllib.request.Request(base + "/v1/devices", data=body(token=FCM, platform="android"), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read()) == {"ok": True}
        assert Store(cfg["paths"]["push_db"]).devices()[0]["token"] == FCM
        gone = urllib.request.Request(base + "/v1/devices/" + urllib.parse.quote(FCM, safe=""), method="DELETE")
        with urllib.request.urlopen(gone, timeout=5) as r:
            assert r.status == 200
        assert Store(cfg["paths"]["push_db"]).device_count() == 0
    finally:
        httpd.shutdown()
    assert len(lines) == 2 and all(FCM[:7] not in line for line in lines)
    assert lines[1] == "api DELETE /v1/devices/dQw4w9… 200"


# Store

OLD_DEVICES = ("CREATE TABLE devices (token TEXT PRIMARY KEY, dkey TEXT NOT NULL, env TEXT NOT NULL, tz TEXT NOT NULL,"
               " locale TEXT, app_version TEXT, topics TEXT, owner INTEGER NOT NULL DEFAULT 0, held_event TEXT,"
               " sound TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")


def test_a_push_db_from_before_android_is_upgraded_in_place_and_its_devices_are_iphones(tmp_path):
    path = str(tmp_path / "push.db")
    c = sqlite3.connect(path)
    c.execute(OLD_DEVICES)
    c.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (IOS, dkey(IOS), "production", "Asia/Kolkata", "en_IN", "2.0", None, 1, "ev_held", "wire",
               "2026-11-15T06:00:00Z", "2026-11-16T06:00:00Z"))
    c.commit(), c.close()
    s = Store(path)
    d = s.devices()[0]
    assert (d["token"], d["platform"], d["owner"], d["held_event"], d["sound"], d["created_at"]) == \
        (IOS, "ios", 1, "ev_held", "wire", "2026-11-15T06:00:00Z")
    s.register(FCM, "production", "UTC", None, None, None, NOW, platform="android")
    assert s.platform_counts() == {"ios": 1, "android": 1} and s.device_stats()["total"] == 2
    Store(path)                                                         # opening it again changes nothing
    assert [r["platform"] for r in Store(path).devices()] == ["ios", "android"]
    # The service's previous code (a rollback) can still write to the upgraded file: the column has a default.
    c = sqlite3.connect(path)
    c.execute("INSERT INTO devices (token, dkey, env, tz, locale, app_version, topics, sound, created_at, updated_at)"
              " VALUES (?,?,?,?,?,?,?,?,?,?)", (IOS_B, dkey(IOS_B), "sandbox", "UTC", None, None, None, None,
                                                "2026-11-16T07:00:00Z", "2026-11-16T07:00:00Z"))
    c.commit(), c.close()
    assert Store(path).platform_counts() == {"ios": 2, "android": 1}


def test_a_device_is_found_by_prefix_whatever_its_platform(tmp_path):
    s = Store(str(tmp_path / "push.db"))
    s.register(IOS, "sandbox", "UTC", None, None, None, NOW)
    s.register(FCM, "production", "UTC", None, None, None, NOW, platform="android")
    s.register(FCM_B, "production", "UTC", None, None, None, NOW, platform="android")
    assert [d["token"] for d in s.find("dQw4w9")] == [FCM]              # as typed, capitals and all
    assert s.find("dqw4w9") == [] and s.find("DQW4W9") == []
    assert [d["token"] for d in s.find("AB12CD")] == [IOS]              # hex in any case, as before
    assert [d["token"] for d in s.find("ab12")] == [IOS]
    assert s.find("dQw4w9%") == [] and s.find("dQw4w_") == [] and s.find("_") == []     # nothing is a wildcard
    assert len(s.find("")) == 3


# FCM client

class FakeResponse:
    def __init__(self, status, doc=None):
        self.status_code, self._doc, self.headers = status, doc, {}

    def json(self):
        if self._doc is None:
            raise ValueError
        return self._doc


def ok(name="projects/techgist-test/messages/0:1700000000%abc"):
    return FakeResponse(200, {"name": name})


def err(status, code=None, state="X"):
    e = {"code": status, "message": "The registration token " + FCM + " is not valid", "status": state}
    if code:
        e["details"] = [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": code}]
    return FakeResponse(status, {"error": e})


class FakeHttp:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def post(self, url, headers=None, content=None):
        self.calls.append((url, headers, content))
        a = self.answers.pop(0) if self.answers else ok()
        if isinstance(a, Exception):
            raise a
        return a


def make_fcm(answers, lasts=3600):
    cfg = dict(config.DEFAULTS["fcm"], project_id="techgist-test")
    fetched, slept, t = [], [], [1000.0]
    f = Fcm(cfg, client=FakeHttp(answers), clock=lambda: t[0], sleep=slept.append,
            fetcher=lambda at: (fetched.append(at) or f"access-{len(fetched)}", lasts))
    return f, fetched, slept, t


MSG = {"data": {"kind": "story", "title": "T", "body": "B", "eventId": "ev_1"}, "android": {"priority": "HIGH"}}


def sent_body(f, i=0):
    return json.loads(f.client().calls[i][2])


def test_send_posts_the_message_to_the_projects_v1_endpoint_with_token_ttl_and_collapse_key():
    f, _, _, t = make_fcm([ok()])
    r = f.send(FCM, "production", MSG, collapse_id="c-1", expiration=t[0] + 900)
    assert (r.outcome, r.status, r.apns_id, r.env, r.attempts) == \
        ("ok", 200, "projects/techgist-test/messages/0:1700000000%abc", "production", 1)
    url, h, _ = f.client().calls[0]
    assert url == "https://fcm.googleapis.com/v1/projects/techgist-test/messages:send"
    assert h["authorization"] == "Bearer access-1" and h["content-type"].startswith("application/json")
    assert sent_body(f) == {"message": {"token": FCM, "data": MSG["data"],
                                        "android": {"priority": "HIGH", "ttl": "900s", "collapse_key": "c-1"}}}
    assert "notification" not in sent_body(f)["message"]
    assert MSG["android"] == {"priority": "HIGH"}                       # the caller's message is not written to


def test_without_an_expiration_the_configured_ttl_is_used_and_a_past_one_is_now_or_never():
    f, _, _, t = make_fcm([])
    f.send(FCM, "production", MSG)
    f.send(FCM, "production", MSG, expiration=t[0] - 5)
    assert sent_body(f, 0)["message"]["android"] == {"priority": "HIGH", "ttl": "10800s"}
    assert sent_body(f, 1)["message"]["android"]["ttl"] == "0s"


def test_env_means_nothing_to_fcm_and_comes_back_as_it_went_in():
    f, _, _, _ = make_fcm([])
    assert [f.send(FCM, e, MSG).env for e in ("sandbox", "production")] == ["sandbox", "production"]


def test_the_access_token_is_fetched_once_reused_and_renewed_before_it_runs_out():
    f, fetched, _, t = make_fcm([])
    for _ in range(3):
        f.send(FCM, "production", MSG)
    assert fetched == [1000.0]
    t[0] += 3600 - fcm_mod.RENEW_BEFORE_S - 1
    f.send(FCM, "production", MSG)
    assert len(fetched) == 1
    t[0] += 1                                                           # five minutes before Google's hour ends
    f.send(FCM, "production", MSG)
    assert len(fetched) == 2 and f.client().calls[-1][1]["authorization"] == "Bearer access-2"


@pytest.mark.parametrize("answer,outcome,reason", [
    (err(404, "UNREGISTERED", "NOT_FOUND"), "gone", "UNREGISTERED"),
    (err(404, None, "NOT_FOUND"), "gone", "NOT_FOUND"),
    (err(400, "UNREGISTERED", "INVALID_ARGUMENT"), "gone", "UNREGISTERED"),
    (err(403, "SENDER_ID_MISMATCH", "PERMISSION_DENIED"), "gone", "SENDER_ID_MISMATCH"),
    (err(403, None, "PERMISSION_DENIED"), "auth", "PERMISSION_DENIED"),
    (err(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT"), "bad", "INVALID_ARGUMENT"),
    (err(400, None, "FAILED_PRECONDITION"), "bad", "FAILED_PRECONDITION"),
    (FakeResponse(418), "bad", ""),
])
def test_refusals_are_final_and_say_whether_the_device_or_our_credentials_are_at_fault(answer, outcome, reason):
    f, _, slept, _ = make_fcm([answer])
    r = f.send(FCM, "production", MSG)
    assert (r.outcome, r.reason, r.attempts, r.status) == (outcome, reason, 1, answer.status_code)
    assert slept == [] and len(f.client().calls) == 1


def test_429_5xx_and_network_errors_are_retried_with_backoff():
    f, _, slept, _ = make_fcm([err(429, "QUOTA_EXCEEDED"), err(503, "UNAVAILABLE"), ok()])
    assert f.send(FCM, "production", MSG).outcome == "ok" and slept == [0.5, 2]
    f, _, slept, _ = make_fcm([ConnectionError("reset"), err(500, "INTERNAL"), FakeResponse(502)])
    r = f.send(FCM, "production", MSG)
    assert (r.outcome, r.attempts, r.status) == ("retry", 3, 502) and slept == [0.5, 2]


def test_401_fetches_a_new_access_token_once_then_reports_auth():
    f, fetched, _, _ = make_fcm([err(401, None, "UNAUTHENTICATED"), ok()])
    r = f.send(FCM, "production", MSG)
    assert (r.outcome, r.attempts) == ("ok", 2) and len(fetched) == 2
    assert [c[1]["authorization"] for c in f.client().calls] == ["Bearer access-1", "Bearer access-2"]
    f, fetched, _, _ = make_fcm([err(401, None, "UNAUTHENTICATED"), err(401, None, "UNAUTHENTICATED")])
    r = f.send(FCM, "production", MSG)
    assert (r.outcome, r.status, r.reason) == ("auth", 401, "UNAUTHENTICATED") and len(fetched) == 2


def test_googles_error_text_which_can_quote_the_token_never_becomes_the_reason():
    f, _, _, _ = make_fcm([FakeResponse(400, {"error": {"status": "token " + FCM, "details": [{"errorCode": FCM}]}})])
    r = f.send(FCM, "production", MSG)
    assert r.outcome == "bad" and r.reason == "" and FCM[:7] not in repr(r)


def test_an_oversized_message_is_not_sent():
    f, _, _, _ = make_fcm([])
    r = f.send(FCM, "production", {"data": {"body": "y" * 5000}})
    assert (r.outcome, r.reason) == ("bad", "PayloadTooLarge") and f.client().calls == []


def service_account(tmp_path, **over):
    pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)       # made here, used nowhere else
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    acc = dict({"type": "service_account", "project_id": "techgist-test", "private_key": pem,
                "client_email": "push@techgist-test.iam.gserviceaccount.com",
                "token_uri": "https://oauth2.googleapis.com/token"}, **over)
    path = tmp_path / "service-account.json"
    path.write_text(json.dumps(acc))
    return str(path), key


def test_a_real_rs256_assertion_is_exchanged_for_the_access_token_and_the_file_names_the_project(tmp_path):
    import jwt
    path, key = service_account(tmp_path)
    http = FakeHttp([FakeResponse(200, {"access_token": "ya29.fake", "expires_in": 3599, "token_type": "Bearer"}),
                     ok(), ok()])
    f = Fcm(dict(config.DEFAULTS["fcm"], credentials_file=path), client=http, clock=lambda: 1700000000.0)
    assert f.send(FCM, "production", MSG).outcome == "ok" and f.send(FCM, "production", MSG).outcome == "ok"
    (url, h, form), (send_url, send_h, _), _ = http.calls               # one exchange, two sends
    assert url == "https://oauth2.googleapis.com/token" and h["content-type"] == "application/x-www-form-urlencoded"
    q = urllib.parse.parse_qs(form.decode())
    assert q["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    assert jwt.get_unverified_header(q["assertion"][0])["alg"] == "RS256"
    claims = jwt.decode(q["assertion"][0], key.public_key(), algorithms=["RS256"], audience=url,
                        options={"verify_iat": False, "verify_exp": False})
    assert claims == {"iss": "push@techgist-test.iam.gserviceaccount.com", "aud": url, "iat": 1700000000,
                      "scope": "https://www.googleapis.com/auth/firebase.messaging", "exp": 1700003600}
    assert send_url == "https://fcm.googleapis.com/v1/projects/techgist-test/messages:send"
    assert send_h["authorization"] == "Bearer ya29.fake"


def test_credentials_google_refuses_or_a_broken_file_are_auth_and_googles_own_trouble_is_a_retry(tmp_path):
    path, _ = service_account(tmp_path)
    cfg = dict(config.DEFAULTS["fcm"], credentials_file=path)
    f = Fcm(cfg, client=FakeHttp([FakeResponse(400, {"error": "invalid_grant", "error_description": "x"})]))
    r = f.send(FCM, "production", MSG)
    assert (r.outcome, r.status, r.reason) == ("auth", 400, "invalid_grant")
    slept = []
    f = Fcm(cfg, client=FakeHttp([FakeResponse(503), FakeResponse(503), FakeResponse(503)]), sleep=slept.append)
    assert f.send(FCM, "production", MSG).outcome == "retry" and slept == [0.5, 2]
    for text in ("not json", "{}", json.dumps({"client_email": "a@b", "project_id": "p", "private_key": "nonsense"})):
        (tmp_path / "broken.json").write_text(text)
        f = Fcm(dict(cfg, credentials_file=str(tmp_path / "broken.json")), client=FakeHttp([]))
        r = f.send(FCM, "production", MSG)
        assert r.outcome == "auth" and r.reason.startswith("CredentialsFile") and f.client().calls == []
    path, _ = service_account(tmp_path, token_uri="http://oauth2.example/token")         # never over plain http
    f = Fcm(dict(cfg, credentials_file=path), client=FakeHttp([]))
    assert f.send(FCM, "production", MSG).outcome == "auth" and f.client().calls == []


def test_fcm_is_built_only_when_the_service_account_file_can_be_read(tmp_path):
    cfg = config.load(str(tmp_path))
    assert cfg["fcm"]["credentials_file"] == "/etc/techgist/fcm/service-account.json"
    cfg["fcm"]["credentials_file"] = str(tmp_path / "absent.json")
    assert fcm_mod.from_config(cfg) is None
    (tmp_path / "absent.json").write_text("{}")
    assert isinstance(fcm_mod.from_config(cfg), Fcm)                    # and the file is not opened until a send


# The message

def test_the_story_message_is_data_only_all_strings_and_the_text_is_unchanged():
    alert = {"event_id": "ev_1", "title": "Huang weighs return as Nvidia CEO", "body": "Allies assemble a “war room”.",
             "payload": {"url": "https://fallback.example/x"}}
    art = {"url": "https://example.com/a", "image": "https://img.example/a_std.jpg"}
    m = fcm_mod.story_message(alert, art, "wire")
    assert m == {"data": {"kind": "story", "title": alert["title"], "body": alert["body"], "eventId": "ev_1",
                          "url": "https://example.com/a", "image": "https://img.example/a_std.jpg", "sound": "wire"},
                 "android": {"priority": "HIGH"}}
    assert "notification" not in m and "token" not in m
    assert all(isinstance(v, str) for v in m["data"].values())
    bare = fcm_mod.story_message(alert, {"url": None, "image": None}, None)["data"]
    assert bare == {"kind": "story", "title": alert["title"], "body": alert["body"], "eventId": "ev_1",
                    "url": "https://fallback.example/x"}                # no picture, no choice of sound: no key


def test_server_and_test_messages_say_what_they_are():
    s = fcm_mod.server_message("Techgist: worker failed", "it was killed")
    assert s["data"] == {"kind": "server", "title": "Server alert: Techgist: worker failed", "body": "it was killed"}
    t = fcm_mod.trial_message("Techgist test", "Hello.", "soft")
    assert t["data"] == {"kind": "test", "title": "Techgist test", "body": "Hello.", "sound": "soft"}
    assert "notification" not in s and "notification" not in t and "eventId" not in s["data"]


# Sender

class FakeFcm(FakeApns):
    """A FakeApns by another name: Fcm.send has the same signature and answers."""


class Both(Env):
    """The sender tests' Env with a fake FCM beside the fake APNs (fcm=None: not configured)."""

    def __init__(self, tmp_path, hold_s=0, fcm=True):
        self.fcm = FakeFcm() if fcm else None
        super().__init__(tmp_path, hold_s)

    def new_sender(self):
        edition = type("E", (), {"articles": lambda _self: self.articles})()
        return Sender(self.cfg, Store(self.cfg["paths"]["push_db"]), self.apns, alertq=alertq,
                      edition=edition, ntfy=self.ntfy, log=self.lines.append, fcm=self.fcm)

    def android(self, token=FCM, tz="Asia/Kolkata", at=NOW - timedelta(days=1), sound=None):
        self.store.register(token, "production", tz, "en", "1.0", None, at, sound=sound, platform="android")


@pytest.fixture
def env(tmp_path):
    return Both(tmp_path)


def events(fake):
    return [s["payload"]["data"]["eventId"] if "data" in s["payload"] else s["payload"]["eventId"] for s in fake.sent]


def test_one_story_reaches_the_iphone_through_apns_and_the_android_phone_through_fcm_once_each(env):
    env.device(IOS), env.android(FCM, sound="flash")
    env.story()
    env.sender.tick(NOW)
    assert [s["token"] for s in env.apns.sent] == [IOS] and [s["token"] for s in env.fcm.sent] == [FCM]
    row = env.engine.one("SELECT * FROM alert_queue")
    a, f = env.apns.sent[0], env.fcm.sent[0]
    assert a["payload"]["aps"]["alert"] == {"title": "Title", "body": "Body."} and "data" not in a["payload"]
    assert f["payload"] == {"data": {"kind": "story", "title": "Title", "body": "Body.", "eventId": "ev_a",
                                     "url": "https://example.com/ev_a",
                                     "image": "https://img.example/ev_a_std.jpg", "sound": "flash"},
                            "android": {"priority": "HIGH"}}
    assert a["collapse_id"] == f["collapse_id"] == row["collapse_id"]
    env.sender.tick(NOW + timedelta(minutes=1))
    env.sender.tick(NOW + timedelta(hours=4))
    assert len(env.apns.sent) == 1 and len(env.fcm.sent) == 1
    h = env.sender.health(NOW)
    assert h["sent"]["total"] == 2 and h["platforms"] == {"ios": 1, "android": 1} and h["fcm"] == {"configured": True}
    assert any("fcm-id" in line and "dQw4w9…" in line for line in env.lines)
    assert all(FCM[:7] not in line and IOS[:7] not in line for line in env.lines)


def test_the_gap_and_the_daily_cap_are_the_same_on_both_platforms(env):
    env.device(IOS), env.android(FCM)
    gap = timedelta(minutes=env.cfg["rules"]["min_gap_min"])
    t = NOW
    env.story("ev_1", now=t)
    env.sender.tick(t)
    env.story("ev_2", now=t + timedelta(minutes=10))
    env.sender.tick(t + timedelta(minutes=10))                                      # inside the gap
    t2 = t + gap
    env.story("ev_3", now=t2)
    env.sender.tick(t2)
    t3 = t2 + gap                                                                   # cap of 2 reached
    env.story("ev_4", now=t3)
    env.story("ev_5", now=t3, importance=9, score=0.1)
    env.sender.tick(t3)
    assert events(env.apns) == events(env.fcm) == ["ev_1", "ev_3", "ev_5"]


def test_quiet_hours_hold_the_best_story_for_the_morning_on_both_platforms(env):
    env.device(IOS, tz="Asia/Kolkata"), env.android(FCM, tz="Asia/Kolkata")
    env.android(FCM_B, tz="America/Los_Angeles")                                    # 10:00 there: not quiet
    night = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)                      # 23:30 IST
    env.story("ev_n1", now=night, score=0.3)
    env.story("ev_n2", now=night, score=0.9)
    env.sender.tick(night)
    assert env.apns.sent == [] and [s["token"] for s in env.fcm.sent] == [FCM_B]
    assert {d["token"]: d["held_event"] for d in env.store.devices()} == {IOS: "ev_n2", FCM: "ev_n2", FCM_B: None}
    morning = datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc)                     # 07:30 IST
    env.sender.tick(morning - timedelta(minutes=1))
    assert env.apns.sent == [] and len(env.fcm.sent) == 1
    env.sender.tick(morning)
    assert events(env.apns) == ["ev_n2"] and [(s["token"], s["payload"]["data"]["eventId"])
                                              for s in env.fcm.sent[1:]] == [(FCM, "ev_n2")]


def test_an_android_device_waits_quietly_while_fcm_is_not_configured_and_gets_the_story_once_it_is(tmp_path):
    env = Both(tmp_path, fcm=False)
    env.device(IOS), env.android(FCM)
    env.story()
    for i in range(4):
        env.sender.tick(NOW + timedelta(seconds=15 * i))
    assert [s["token"] for s in env.apns.sent] == [IOS]                 # the iPhone is served as ever
    assert env.store.q("SELECT COUNT(*) n FROM deliveries")[0]["n"] == 1        # nothing recorded for the Android
    h = env.sender.health(NOW)
    assert (h["sent"]["total"], h["failed"]["total"], h["failed"]["last"], h["removedDevices"]) == (1, 0, None, 0)
    assert h["fcm"] == {"configured": False} and h["platforms"] == {"ios": 1, "android": 1}
    assert sum("FCM not configured, 1 Android devices wait" in line for line in env.lines) == 1     # said once
    assert env.ntfy.out == []                                           # and never an alert to the owner
    # The service account arrives and the service restarts, inside the story's three hours.
    env.fcm = FakeFcm()
    env.new_sender().tick(NOW + timedelta(minutes=30))
    assert [s["token"] for s in env.fcm.sent] == [FCM] and len(env.apns.sent) == 1
    env.new_sender().tick(NOW + timedelta(minutes=31))
    assert len(env.fcm.sent) == 1


def test_a_story_too_old_by_the_time_fcm_is_configured_is_not_sent_late(tmp_path):
    env = Both(tmp_path, fcm=False)
    env.android(FCM)
    env.story()
    env.sender.tick(NOW)
    env.fcm = FakeFcm()
    env.new_sender().tick(NOW + timedelta(hours=env.cfg["rules"]["max_age_h"], minutes=1))
    assert env.fcm.sent == []


def test_a_token_fcm_no_longer_knows_removes_the_device_and_the_others_still_get_it(env):
    env.android(FCM), env.android(FCM_B), env.device(IOS)
    env.fcm.answers[FCM] = Result("gone", 404, "UNREGISTERED", "", "production", 1)
    env.story()
    env.sender.tick(NOW)
    assert sorted(d["token"] for d in env.store.devices()) == sorted([FCM_B, IOS])
    h = env.sender.health(NOW)
    assert h["removedDevices"] == 1 and h["sent"]["total"] == 2 and h["platforms"] == {"ios": 1, "android": 1}
    assert h["failed"]["last"]["reason"] == "UNREGISTERED" and h["failed"]["last"]["service"] == "FCM"
    assert any(line == "removed dQw4w9…: FCM 404 UNREGISTERED" for line in env.lines)


def test_a_story_fcm_refuses_for_good_is_not_tried_again_on_that_device(env):
    env.android(FCM)
    env.fcm.answers[FCM] = Result("bad", 400, "INVALID_ARGUMENT", "", "production", 1)
    env.story()
    env.sender.tick(NOW)
    env.sender.tick(NOW + timedelta(seconds=15))
    assert len(env.fcm.sent) == 1 and env.store.device_count() == 1


def test_fcm_being_down_stops_its_own_round_and_never_the_iphones(env):
    droids = [f"{i:06d}_Xy:APA91b" + "Hk-3_zT" * 20 for i in range(30)]
    for i, tok in enumerate(droids):                    # registered first: they come before the iPhones in a round
        env.android(tok, at=NOW - timedelta(days=2))
        env.fcm.answers[tok] = Result("retry", 0, "ConnectTimeout", "", "production", 3)
    phones = [f"{i:06x}" + "c" * 58 for i in range(20)]
    for tok in phones:
        env.device(tok)
    env.story()
    env.sender.tick(NOW)
    assert sorted(s["token"] for s in env.apns.sent) == sorted(phones)
    assert len(env.fcm.sent) == env.cfg["apns"]["concurrency"]          # one batch, then the breaker
    assert any("FCM is not answering" in line and "22 sends wait" in line for line in env.lines)
    assert not any("APNs is not answering" in line for line in env.lines)
    assert env.store.device_count() == 50
    env.fcm.answers.clear()
    env.sender.tick(NOW + timedelta(seconds=15))
    assert sorted(s["token"] for s in env.fcm.sent[8:]) == sorted(droids) and len(env.apns.sent) == 20


def test_refused_credentials_alert_the_owner_about_the_right_service(env):
    env.device(IOS), env.android(FCM)
    env.fcm.answers[FCM] = Result("auth", 403, "PERMISSION_DENIED", "", "production", 1)
    env.story()
    env.sender.tick(NOW)
    alerts = [m for m in env.ntfy.out if m["key"]]
    assert [m["key"] for m in alerts] == ["fcm_auth"]
    assert "FCM" in alerts[0]["title"] and "PERMISSION_DENIED" in alerts[0]["message"]
    assert "APNs" not in alerts[0]["title"] and len(env.apns.sent) == 1
    del env.fcm.answers[FCM]                                            # nothing was lost
    env.sender.tick(NOW + timedelta(seconds=15))
    assert events(env.fcm) == ["ev_a", "ev_a"] and env.sender.health(NOW)["sent"]["total"] == 2
    # ... and APNs refusing its key is still reported as APNs.
    env.apns.answers[IOS] = Result("auth", 403, "InvalidProviderToken", "", "production", 2)
    env.ntfy.out.clear()
    later = NOW + timedelta(hours=4)
    env.story("ev_b", now=later)
    env.sender.tick(later)
    assert [(m["key"], m["title"]) for m in env.ntfy.out if m["key"]] == \
        [("apns_auth", "Techgist push: APNs refused our key")]


def test_server_alerts_reach_each_owner_device_through_its_own_service(env):
    env.cfg["owner"]["relay_ntfy"] = True
    env.device(IOS), env.android(FCM), env.android(FCM_B)
    env.store.set_owner(IOS), env.store.set_owner(FCM)
    env.sender.tick(NOW)                                                # first run: cursor only
    env.ntfy.inbox = [{"id": "m1", "event": "message", "title": "Techgist: no_publish", "message": "it stopped",
                       "priority": 4}]
    env.sender.tick(NOW + timedelta(seconds=env.cfg["owner"]["poll_s"]))
    assert [s["token"] for s in env.apns.sent] == [IOS] and [s["token"] for s in env.fcm.sent] == [FCM]
    assert env.apns.sent[0]["payload"]["aps"]["alert"]["title"] == "Server alert: Techgist: no_publish"
    assert env.fcm.sent[0]["payload"]["data"] == {"kind": "server", "title": "Server alert: Techgist: no_publish",
                                                  "body": "it stopped"}
    assert env.fcm.sent[0]["collapse_id"] == "ntfy-m1"


def test_an_android_owner_device_is_passed_over_while_fcm_is_not_configured(tmp_path):
    env = Both(tmp_path, fcm=False)
    env.cfg["owner"]["relay_ntfy"] = True
    env.device(IOS), env.android(FCM)
    env.store.set_owner(IOS), env.store.set_owner(FCM)
    env.sender.tick(NOW)
    env.ntfy.inbox = [{"id": "m1", "event": "message", "title": "Techgist: no_publish", "message": "m", "priority": 4}]
    env.sender.tick(NOW + timedelta(seconds=env.cfg["owner"]["poll_s"]))
    assert [s["token"] for s in env.apns.sent] == [IOS] and env.sender.health(NOW)["failed"]["total"] == 0


# pushctl

def ctl(monkeypatch, tmp_path, capsys, argv, fcm):
    monkeypatch.setenv("TECHGIST_STATE", str(tmp_path))
    monkeypatch.delenv("TECHGIST_PUSH_CONFIG", raising=False)
    monkeypatch.setattr(cli.fcm_mod, "from_config", lambda cfg: fcm)
    try:
        code = cli.main(argv)
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


def test_pushctl_lists_the_platform_and_tests_an_android_device_through_fcm(monkeypatch, tmp_path, capsys):
    cfg = config.load(str(tmp_path))
    s = Store(cfg["paths"]["push_db"])
    s.register(IOS, "production", "Asia/Kolkata", "en", "2.0", None, NOW)
    s.register(FCM, "production", "Asia/Kolkata", "en", "1.0", None, NOW, sound="soft", platform="android")
    fake = FakeFcm()
    code, out = ctl(monkeypatch, tmp_path, capsys, ["devices"], fake)
    rows = out.splitlines()
    assert rows[0].startswith("ab12cd… ios ") and rows[1].startswith("dQw4w9… android ") and FCM[:7] not in out
    code, out = ctl(monkeypatch, tmp_path, capsys, ["test", "dQw4w9"], fake)
    assert code == 0 and out.endswith("FCM: ok status=200 reason=- attempts=1 message=ID\n")
    assert fake.sent[0]["token"] == FCM
    assert fake.sent[0]["payload"]["data"] == {"kind": "test", "title": "Techgist test",
                                               "body": "A test notification from the push server.", "sound": "soft"}
    # The story itself, as a real alert carries it.
    os.makedirs(os.path.dirname(cfg["paths"]["edition"]))
    with open(cfg["paths"]["edition"], "w") as f:
        json.dump({"articles": [{"eventId": "ev_x", "url": "https://example.com/x", "image": "https://img.example/x.jpg",
                                 "notification": {"title": "Edition title", "body": "Edition body."}}]}, f)
    code, out = ctl(monkeypatch, tmp_path, capsys, ["test", "dQw4w9", "--event", "ev_x"], fake)
    assert code == 0 and "\nFCM: ok " in out
    assert fake.sent[1]["payload"]["data"] == {"kind": "story", "title": "Edition title", "body": "Edition body.",
                                               "eventId": "ev_x", "url": "https://example.com/x",
                                               "image": "https://img.example/x.jpg", "sound": "soft"}
    assert Store(cfg["paths"]["push_db"]).q("SELECT COUNT(*) n FROM deliveries")[0]["n"] == 0    # outside the rules
    # A token FCM no longer knows is removed, as in normal sending.
    fake.answers[FCM] = Result("gone", 404, "UNREGISTERED", "", "production", 1)
    code, out = ctl(monkeypatch, tmp_path, capsys, ["test", "dQw4w9"], fake)
    assert code == 1 and "\nFCM: gone status=404 reason=UNREGISTERED" in out and FCM[:7] not in out
    assert [d["token"] for d in Store(cfg["paths"]["push_db"]).devices()] == [IOS]


def test_pushctl_says_so_when_an_android_device_is_tested_without_fcm(monkeypatch, tmp_path, capsys):
    cfg = config.load(str(tmp_path))
    Store(cfg["paths"]["push_db"]).register(FCM, "production", "UTC", None, None, None, NOW, platform="android")
    code, out = ctl(monkeypatch, tmp_path, capsys, ["test", "dQw4w9"], None)
    assert "FCM is not configured" in str(code) and Store(cfg["paths"]["push_db"]).device_count() == 1
    code, out = ctl(monkeypatch, tmp_path, capsys, ["status"], None)
    doc = json.loads(out)
    assert doc["fcm"] == {"configured": False} and doc["platforms"] == {"ios": 0, "android": 1}
