"""push/apns.py and push/sender.py against a fake APNs, the real alert queue (worker/alertq.py) and a temp state."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from push import apns as apns_mod
from push import config
from push.apns import Apns, Result
from push.sender import HOLD_TAG, Sender
from push.store import Store
from worker import alertq
from worker.db import DB

NOW = datetime(2026, 11, 16, 6, 30, tzinfo=timezone.utc)        # noon in India
TOK_A, TOK_B = "aaaaaa" + "1" * 58, "bbbbbb" + "2" * 58


# APNs client

class FakeResponse:
    def __init__(self, status, reason=""):
        self.status_code, self._reason, self.headers, self.http_version = status, reason, {"apns-id": "ID-1"}, "HTTP/2"

    def json(self):
        if not self._reason:
            raise ValueError
        return {"reason": self._reason}


class FakeHttp:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def post(self, url, headers=None, content=None):
        self.calls.append((url, headers, content))
        a = self.answers.pop(0) if self.answers else FakeResponse(200)
        if isinstance(a, Exception):
            raise a
        return a


def make_apns(answers, clock=None):
    cfg = dict(config.DEFAULTS["apns"])
    signed, slept = [], []
    t = clock or [1000.0]
    a = Apns(cfg, client=FakeHttp(answers), clock=lambda: t[0], sleep=slept.append,
             signer=lambda at: signed.append(at) or f"jwt-{len(signed)}")
    return a, signed, slept, t


def test_send_goes_to_the_devices_environment_with_the_contract_headers():
    a, _, _, _ = make_apns([FakeResponse(200), FakeResponse(200)])
    r = a.send(TOK_A, "sandbox", {"aps": {}}, collapse_id="c-1", expiration=123)
    assert (r.outcome, r.apns_id, r.env) == ("ok", "ID-1", "sandbox")
    url, h, _ = a.client().calls[0]
    assert url == "https://api.sandbox.push.apple.com/3/device/" + TOK_A
    assert h["apns-topic"] == "com.pranjald.techgist" and h["apns-push-type"] == "alert"
    assert h["apns-collapse-id"] == "c-1" and h["apns-expiration"] == "123" and h["authorization"] == "bearer jwt-1"
    a.send(TOK_A, "production", {"aps": {}})
    assert a.client().calls[1][0].startswith("https://api.push.apple.com/3/device/")


def test_the_provider_token_is_reused_and_renewed_under_an_hour():
    a, signed, _, t = make_apns([])
    for _ in range(3):
        a.send(TOK_A, "sandbox", {})
    assert len(signed) == 1
    t[0] += a.cfg["jwt_ttl_s"] - 1
    a.send(TOK_A, "sandbox", {})
    assert len(signed) == 1
    t[0] += 1
    a.send(TOK_A, "sandbox", {})
    assert len(signed) == 2 and a.cfg["jwt_ttl_s"] < 3600


def test_a_real_es256_token_carries_key_id_team_and_time(tmp_path):
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "AuthKey.p8"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    cfg = dict(config.DEFAULTS["apns"], key_file=str(path))
    token = Apns(cfg, client=FakeHttp([]), clock=lambda: 1700000000.0).provider_token()
    assert jwt.get_unverified_header(token) == {"alg": "ES256", "kid": "K2DXJ7NMGX", "typ": "JWT"}
    claims = jwt.decode(token, key.public_key(), algorithms=["ES256"], options={"verify_iat": False})
    assert claims == {"iss": "A4HLS6BV3C", "iat": 1700000000}


def test_bad_device_token_is_tried_on_the_other_host_before_the_device_is_gone():
    a, _, _, _ = make_apns([FakeResponse(400, "BadDeviceToken"), FakeResponse(400, "BadDeviceToken")])
    r = a.send(TOK_A, "sandbox", {})
    assert (r.outcome, r.reason, r.attempts) == ("gone", "BadDeviceToken", 2)
    assert [c[0].split("/3/")[0] for c in a.client().calls] == ["https://api.sandbox.push.apple.com",
                                                               "https://api.push.apple.com"]
    # The other host takes it: delivered, and the caller learns the real environment.
    a, _, _, _ = make_apns([FakeResponse(400, "BadDeviceToken"), FakeResponse(200)])
    r = a.send(TOK_A, "sandbox", {})
    assert (r.outcome, r.env) == ("ok", "production")


def test_410_means_gone_at_once():
    a, _, _, _ = make_apns([FakeResponse(410, "Unregistered")])
    r = a.send(TOK_A, "production", {})
    assert (r.outcome, r.status, r.attempts) == ("gone", 410, 1)


def test_429_5xx_and_network_errors_are_retried_with_backoff():
    a, _, slept, _ = make_apns([FakeResponse(429, "TooManyRequests"), FakeResponse(503), FakeResponse(200)])
    assert a.send(TOK_A, "sandbox", {}).outcome == "ok" and slept == [0.5, 2]
    a, _, slept, _ = make_apns([ConnectionError("reset"), FakeResponse(500), FakeResponse(502)])
    r = a.send(TOK_A, "sandbox", {})
    assert (r.outcome, r.attempts) == ("retry", 3) and slept == [0.5, 2]


def test_403_renews_the_token_once_then_reports_auth():
    a, signed, _, t = make_apns([FakeResponse(403, "ExpiredProviderToken"), FakeResponse(200)])
    a.provider_token()
    t[0] += 1500
    assert a.send(TOK_A, "sandbox", {}).outcome == "ok" and len(signed) == 2
    a, _, _, _ = make_apns([FakeResponse(403, "InvalidProviderToken"), FakeResponse(403, "InvalidProviderToken")])
    r = a.send(TOK_A, "sandbox", {})
    assert (r.outcome, r.reason) == ("auth", "InvalidProviderToken")


def test_other_refusals_are_final_and_an_oversized_payload_is_not_sent():
    a, _, _, _ = make_apns([FakeResponse(400, "BadCollapseId")])
    assert a.send(TOK_A, "sandbox", {}).outcome == "bad"
    a, _, _, _ = make_apns([])
    assert a.send(TOK_A, "sandbox", {"x": "y" * 5000}).outcome == "bad" and a.client().calls == []


def test_story_payload_is_the_contract_and_the_text_is_unchanged():
    alert = {"event_id": "ev_1", "title": "Huang weighs return as Nvidia CEO", "body": "Allies assemble a “war room”.",
             "payload": {"url": "https://fallback.example/x"}}
    art = {"url": "https://example.com/a", "image": "https://img.example/a_std.jpg"}
    p = apns_mod.story_payload(alert, art, "techgist.caf")
    assert p == {"aps": {"alert": {"title": alert["title"], "body": alert["body"]}, "sound": "techgist.caf",
                         "mutable-content": 1, "thread-id": "ev_1"},
                 "eventId": "ev_1", "url": "https://example.com/a", "image": "https://img.example/a_std.jpg"}
    assert "image" not in apns_mod.story_payload(alert, {"url": "https://example.com/a", "image": None}, "s")
    assert apns_mod.server_payload("Techgist: worker failed", "x")["aps"]["alert"]["title"].startswith("Server alert: ")


# Sender

class FakeApns:
    def __init__(self):
        self.sent, self.answers = [], {}

    def send(self, token, env, payload, collapse_id=None, expiration=None, priority=10):
        self.sent.append({"token": token, "env": env, "payload": payload, "collapse_id": collapse_id})
        r = self.answers.get(token)
        return r if r is not None else Result("ok", 200, "", "ID", env, 1)


class FakeNtfy:
    def __init__(self):
        self.out, self.inbox = [], []

    def send(self, title, message, key=None, every_s=3600, priority=None, tags=None, log=print):
        self.out.append({"title": title, "message": message, "tags": tags, "key": key})
        return True

    def poll(self, since):
        return self.inbox


class Env:
    def __init__(self, tmp_path, hold_s=0):
        self.cfg = config.load(str(tmp_path))
        self.cfg["rules"]["owner_hold_s"] = hold_s
        self.engine = DB(self.cfg["paths"]["engine_db"])
        alertq.ensure(self.engine)
        self.engine.commit()
        self.store = Store(self.cfg["paths"]["push_db"])
        self.apns, self.ntfy = FakeApns(), FakeNtfy()
        self.articles = {}
        self.lines = []
        self.sender = self.new_sender()

    def new_sender(self):
        edition = type("E", (), {"articles": lambda _self: self.articles})()
        return Sender(self.cfg, Store(self.cfg["paths"]["push_db"]), self.apns, alertq=alertq,
                      edition=edition, ntfy=self.ntfy, log=self.lines.append)

    def device(self, token=TOK_A, tz="Asia/Kolkata", env="sandbox", at=NOW - timedelta(days=1)):
        self.store.register(token, env, tz, "en", "2.0", None, at)

    def story(self, eid="ev_a", now=NOW, age_min=20, spice=8, importance=7, score=0.5, title="Title", body="Body.",
              note=None):
        self.articles[eid] = {"eventId": eid, "url": f"https://example.com/{eid}", "spice": spice,
                              "image": f"https://img.example/{eid}_std.jpg",
                              "publishedAt": (now - timedelta(minutes=age_min)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if note is not None:
            self.articles[eid]["notification"] = note
        got = alertq.enqueue(self.engine, eid, title, body, now, score=score, published_at=now,
                             payload={"url": f"https://example.com/{eid}", "spice": spice, "importance": importance})
        self.engine.commit()
        return got

    def status(self, eid):
        return self.engine.one("SELECT status FROM alert_queue WHERE event_id = ?", eid)["status"]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_a_queued_story_reaches_every_device_once_with_the_rows_collapse_id(env):
    env.device(TOK_A), env.device(TOK_B, env="production")
    env.story()
    env.sender.tick(NOW)
    assert sorted(s["token"] for s in env.apns.sent) == [TOK_A, TOK_B]
    row = env.engine.one("SELECT * FROM alert_queue")
    assert row["status"] == "sent"
    s = env.apns.sent[0]
    assert s["collapse_id"] == row["collapse_id"]
    assert s["payload"]["aps"]["alert"] == {"title": "Title", "body": "Body."}
    assert s["payload"]["eventId"] == "ev_a" and s["payload"]["image"].endswith("ev_a_std.jpg")
    assert {x["env"] for x in env.apns.sent} == {"sandbox", "production"}
    # Later ticks, and the engine listing the story again, send nothing more.
    assert env.story() == "duplicate"
    env.sender.tick(NOW + timedelta(minutes=1))
    env.sender.tick(NOW + timedelta(hours=4))
    assert len(env.apns.sent) == 2
    assert all(TOK_A not in line for line in env.lines)


def test_nothing_is_claimed_while_no_device_is_registered(env):
    env.story()
    env.sender.tick(NOW)
    assert env.status("ev_a") == "queued" and env.apns.sent == []


def test_sending_disabled_takes_and_sends_nothing(env):
    env.cfg["sending_enabled"] = False
    env.device(), env.story()
    env.sender.tick(NOW)
    assert env.status("ev_a") == "queued" and env.apns.sent == []


def test_a_crash_after_the_claim_neither_loses_nor_repeats(env):
    env.device()
    env.story()
    # The process dies right after claim(): the row is `sending`, nothing is in push.db.
    assert [a.event_id for a in alertq.claim(env.engine, NOW)] == ["ev_a"]
    restarted = env.new_sender()
    restarted.tick(NOW + timedelta(seconds=30))
    assert env.apns.sent == []                              # still inside the lease
    restarted.tick(NOW + alertq.LEASE + timedelta(seconds=1))
    assert len(env.apns.sent) == 1 and env.status("ev_a") == "sent"


def test_a_crash_between_the_copy_and_mark_sent_does_not_duplicate(env):
    env.device()
    env.story()
    a = alertq.claim(env.engine, NOW)[0]
    env.store.add_alert(a.event_id, a.collapse_id, a.title, a.body, a.score, a.payload, NOW, 0)   # ... and it dies
    restarted = env.new_sender()
    restarted.tick(NOW + timedelta(seconds=20))
    assert len(env.apns.sent) == 1                          # delivered from push.db meanwhile
    restarted.tick(NOW + alertq.LEASE + timedelta(seconds=1))      # the queue offers the row again
    assert env.status("ev_a") == "sent" and len(env.apns.sent) == 1
    assert len(env.store.q("SELECT * FROM alerts")) == 1


def test_a_crash_between_apns_and_the_record_resends_with_the_same_collapse_id(env):
    env.device()
    env.story()
    env.sender.tick(NOW)
    env.store.conn.execute("DELETE FROM deliveries")        # as if the answer was never recorded
    env.store.conn.commit()
    env.new_sender().tick(NOW + timedelta(seconds=15))
    assert len(env.apns.sent) == 2
    assert env.apns.sent[0]["collapse_id"] == env.apns.sent[1]["collapse_id"]


def test_gone_devices_are_removed_and_the_others_still_get_it(env):
    env.device(TOK_A), env.device(TOK_B)
    env.apns.answers[TOK_A] = Result("gone", 400, "BadDeviceToken", "", "production", 2)
    env.story()
    env.sender.tick(NOW)
    assert [d["token"] for d in env.store.devices()] == [TOK_B]
    h = env.sender.health(NOW)
    assert h["removedDevices"] == 1 and h["sent"]["total"] == 1 and h["devices"]["total"] == 1
    assert h["failed"]["last"]["reason"] == "BadDeviceToken"


def test_a_transient_failure_is_tried_again_next_loop_and_counted(env):
    env.device()
    env.apns.answers[TOK_A] = Result("retry", 503, "", "", "sandbox", 3)
    env.story()
    env.sender.tick(NOW)
    assert env.sender.health(NOW)["failed"]["total"] == 1 and env.store.device_count() == 1
    del env.apns.answers[TOK_A]
    env.sender.tick(NOW + timedelta(seconds=15))
    assert env.sender.health(NOW)["sent"]["total"] == 1 and len(env.apns.sent) == 2


def test_the_device_that_answered_on_the_other_host_has_its_env_corrected(env):
    env.device(env="sandbox")
    env.apns.answers[TOK_A] = Result("ok", 200, "", "ID", "production", 2)
    env.story()
    env.sender.tick(NOW)
    assert env.store.devices()[0]["env"] == "production"


def test_auth_failure_alerts_the_owner_and_loses_nothing(env):
    env.device()
    env.apns.answers[TOK_A] = Result("auth", 403, "InvalidProviderToken", "", "sandbox", 2)
    env.story()
    env.sender.tick(NOW)
    assert any(m["key"] == "apns_auth" for m in env.ntfy.out)
    del env.apns.answers[TOK_A]
    env.sender.tick(NOW + timedelta(seconds=15))
    assert env.sender.health(NOW)["sent"]["total"] == 1


def test_caps_gap_and_quiet_hours_through_the_whole_path(env):
    env.device(TOK_A, tz="Asia/Kolkata")
    gap = timedelta(minutes=env.cfg["rules"]["min_gap_min"])
    t = NOW
    env.story("ev_1", now=t)
    env.sender.tick(t)
    env.story("ev_2", now=t + timedelta(minutes=10))
    env.sender.tick(t + timedelta(minutes=10))
    assert [s["payload"]["eventId"] for s in env.apns.sent] == ["ev_1"]             # inside the gap
    t2 = t + gap
    env.story("ev_3", now=t2)
    env.sender.tick(t2)
    assert [s["payload"]["eventId"] for s in env.apns.sent] == ["ev_1", "ev_3"]     # ev_2 is too old by now
    t3 = t2 + gap                                                                   # 18:00 IST, cap of 2 reached
    env.story("ev_4", now=t3)
    env.story("ev_5", now=t3, importance=9, score=0.1)
    env.sender.tick(t3)
    assert [s["payload"]["eventId"] for s in env.apns.sent][2:] == ["ev_5"]          # only importance 9 passes


def test_quiet_hours_hold_the_best_and_send_it_in_the_morning(env):
    env.device(TOK_A, tz="Asia/Kolkata")
    night = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)                       # 23:30 IST
    env.story("ev_n1", now=night, score=0.3)
    env.story("ev_n2", now=night, score=0.9)
    env.sender.tick(night)
    assert env.apns.sent == [] and env.store.devices()[0]["held_event"] == "ev_n2"
    assert env.sender.health(night)["held"]["devicesHoldingForMorning"] == 1
    morning = datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc)                      # 07:30 IST
    env.sender.tick(morning - timedelta(minutes=1))
    assert env.apns.sent == []
    env.sender.tick(morning)
    assert [s["payload"]["eventId"] for s in env.apns.sent] == ["ev_n2"]
    assert env.store.devices()[0]["held_event"] is None
    env.sender.tick(morning + timedelta(hours=4))
    assert len(env.apns.sent) == 1


def test_a_story_not_in_the_edition_or_without_text_is_not_sent(env):
    env.device()
    env.story("ev_a")
    del env.articles["ev_a"]
    env.story("ev_b", title="")
    env.sender.tick(NOW)
    assert env.apns.sent == []


def test_owner_hold_announces_then_sends_and_a_kill_stops_it(tmp_path):
    env = Env(tmp_path, hold_s=300)
    env.device()
    env.story("ev_a"), env.story("ev_b", score=0.9)
    env.sender.tick(NOW)
    assert env.apns.sent == []
    holds = [m for m in env.ntfy.out if m["tags"] == HOLD_TAG]
    assert len(holds) == 2 and "pushctl kill ev_" in holds[0]["message"]
    assert env.sender.health(NOW)["held"]["storiesInOwnerHold"] == 2
    assert env.store.kill("ev_b") == 1
    env.sender.tick(NOW + timedelta(seconds=299))
    assert env.apns.sent == []
    env.sender.tick(NOW + timedelta(seconds=300))
    assert [s["payload"]["eventId"] for s in env.apns.sent] == ["ev_a"]


def test_server_alerts_go_to_owner_devices_only_and_bypass_story_limits(env):
    env.device(TOK_A, tz="Asia/Kolkata"), env.device(TOK_B)
    env.store.set_owner(TOK_A)
    night = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)                       # 01:30 IST: quiet hours
    env.sender.tick(night)                                                           # first run: cursor only
    assert env.store.get("ntfy_since") == str(int(night.timestamp()))
    env.ntfy.inbox = [{"id": "m1", "event": "message", "title": "Techgist: techgist-worker.service failed",
                       "message": "it was killed"},
                      {"id": "m2", "event": "message", "title": "Techgist push in 5 min", "message": "x",
                       "tags": [HOLD_TAG]}]
    later = night + timedelta(seconds=env.cfg["owner"]["poll_s"])
    env.sender.tick(later)
    assert [s["token"] for s in env.apns.sent] == [TOK_A]
    alert = env.apns.sent[0]["payload"]["aps"]["alert"]
    assert alert["title"] == "Server alert: Techgist: techgist-worker.service failed" and alert["body"] == "it was killed"
    assert "eventId" not in env.apns.sent[0]["payload"]
    assert env.store.get("ntfy_since") == "m2"
    env.ntfy.inbox = []
    env.sender.tick(later + timedelta(seconds=env.cfg["owner"]["poll_s"]))
    assert len(env.apns.sent) == 1


def test_health_file_counts(env):
    env.device(TOK_A), env.device(TOK_B, env="production")
    env.store.set_owner(TOK_B)
    env.story()
    env.sender.tick(NOW)
    doc = json.load(open(env.cfg["paths"]["health"]))
    assert doc["devices"] == {"total": 2, "sandbox": 1, "production": 1, "owner": 1, "holding": 0}
    assert doc["sent"]["total"] == 2 and doc["sent"]["last24h"] == 2 and doc["failed"]["total"] == 0
    assert doc["engineQueue"] == {"sent": 1} and doc["sendingEnabled"] is True
    assert TOK_A not in json.dumps(doc) and TOK_B not in json.dumps(doc)


# The words are the edition's at the moment of sending

def alerts_sent(env):
    return [s["payload"]["aps"]["alert"] for s in env.apns.sent]


def test_a_card_corrected_after_it_was_queued_is_announced_with_the_corrected_text(tmp_path):
    env = Env(tmp_path, hold_s=300)
    env.device()
    env.story(title="Apple's Siri smart glasses take shape", body="Gurman expects a WWDC reveal in June.",
              note={"title": "Apple's Siri smart glasses take shape", "body": "Gurman expects a WWDC reveal in June."})
    env.sender.tick(NOW)                                    # taken from the queue with the first text; owner hold
    assert env.apns.sent == []
    # The engine rewrites the card while the story waits.
    env.articles["ev_a"]["notification"] = {"title": "Apple's Siri glasses reportedly coming",
                                            "body": "Apple's first glasses will not show augmented reality imagery."}
    env.sender.tick(NOW + timedelta(seconds=300))
    assert alerts_sent(env) == [{"title": "Apple's Siri glasses reportedly coming",
                                 "body": "Apple's first glasses will not show augmented reality imagery."}]
    assert env.store.q("SELECT title FROM alerts")[0]["title"] == "Apple's Siri smart glasses take shape"


def test_the_story_held_overnight_is_sent_with_the_mornings_text(env):
    env.device(TOK_A, tz="Asia/Kolkata")
    night = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)                       # 23:30 IST
    env.story("ev_n", now=night, title="Old title", body="Old body.")
    env.sender.tick(night)
    env.articles["ev_n"]["notification"] = {"title": "Corrected title", "body": "Corrected body."}
    env.sender.tick(datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc))               # 07:30 IST
    assert alerts_sent(env) == [{"title": "Corrected title", "body": "Corrected body."}]


@pytest.mark.parametrize("note", [None, {}, {"title": "Only a title"}, {"title": "  ", "body": "x"},
                                  {"title": 5, "body": "x"}, "text"])
def test_without_a_whole_notification_on_the_card_the_queued_text_is_used_never_a_mix(env, note):
    env.device()
    env.story(title="Queued title", body="Queued body.", note=note)
    env.sender.tick(NOW)
    assert alerts_sent(env) == [{"title": "Queued title", "body": "Queued body."}]


# A device is not sent what was already out when it registered

def test_a_new_device_is_not_sent_the_story_its_reader_is_already_looking_at(env):
    env.device(TOK_A)                                       # registered yesterday
    env.story("ev_a")
    env.sender.tick(NOW)
    env.device(TOK_B, at=NOW + timedelta(minutes=1))        # allows alerts with ev_a on the screen
    env.sender.tick(NOW + timedelta(minutes=1, seconds=7))
    assert [s["token"] for s in env.apns.sent] == [TOK_A]
    env.story("ev_b", now=NOW + timedelta(minutes=5), score=0.1)     # news after it registered
    env.sender.tick(NOW + timedelta(minutes=5))
    assert [(s["token"], s["payload"]["eventId"]) for s in env.apns.sent][1:] == [(TOK_B, "ev_b")]


def test_the_first_device_ever_is_not_sent_the_backlog_that_waited_in_the_queue(env):
    env.story("ev_a")                                       # queued while nobody was registered
    env.device(TOK_A, at=NOW + timedelta(minutes=10))
    env.sender.tick(NOW + timedelta(minutes=10))
    assert env.status("ev_a") == "sent" and env.apns.sent == []


def test_a_reinstalled_app_with_a_new_token_is_not_sent_the_same_story_again(env):
    env.device(TOK_A)
    env.story("ev_a")
    env.sender.tick(NOW)
    env.store.remove(TOK_A)
    env.device(TOK_B, at=NOW + timedelta(minutes=8))        # the same phone, a new token, no history
    env.sender.tick(NOW + timedelta(minutes=8))
    assert [s["token"] for s in env.apns.sent] == [TOK_A]


def test_a_push_db_from_before_queued_at_is_upgraded_and_falls_back_to_when_it_was_taken(tmp_path):
    import sqlite3
    path = str(tmp_path / "push.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE alerts (event_id TEXT PRIMARY KEY, collapse_id TEXT NOT NULL, title TEXT NOT NULL,"
              " body TEXT NOT NULL, score REAL, payload TEXT, received_at TEXT NOT NULL, not_before TEXT NOT NULL,"
              " killed INTEGER NOT NULL DEFAULT 0)")
    c.execute("INSERT INTO alerts VALUES ('ev_old','c','T','B',0.5,'{}','2026-11-16T06:00:00Z','2026-11-16T06:00:00Z',0)")
    c.commit(), c.close()
    env = Env(tmp_path)
    assert env.store.q("SELECT queued_at FROM alerts")[0]["queued_at"] is None
    env.articles["ev_old"] = {"eventId": "ev_old", "url": "https://example.com/o", "spice": 9,
                              "publishedAt": "2026-11-16T05:50:00Z"}
    env.device(TOK_A, at=NOW - timedelta(minutes=40))       # registered before 06:00: gets it
    env.device(TOK_B, at=NOW - timedelta(minutes=10))       # after: does not
    env.sender.tick(NOW)
    assert [s["token"] for s in env.apns.sent] == [TOK_A]


# A round that cannot finish stops

def many(env, n):
    toks = [f"{i:06x}" + "c" * 58 for i in range(n)]
    for t in toks:
        env.device(t)
    return toks


def test_a_round_stops_when_apns_is_not_answering_and_the_watchdog_is_told_between_batches(env):
    beats, clock = [], [0.0]
    env.sender.heartbeat, env.sender.mono = (lambda: beats.append(1)), (lambda: clock[0])
    toks = many(env, 100)
    for t in toks:
        env.apns.answers[t] = Result("retry", 0, "ConnectTimeout", "", "sandbox", 3)
    env.story()
    env.sender.tick(NOW)
    batch = env.cfg["apns"]["concurrency"]
    assert len(env.apns.sent) == batch and len(beats) == 1          # one batch, not 100 devices times 32 s
    assert any("APNs is not answering" in line and "92 sends wait" in line for line in env.lines)
    assert env.store.device_count() == 100                           # nobody is removed or marked over it
    # APNs comes back: everyone gets the story, once.
    env.apns.answers.clear()
    env.sender.tick(NOW + timedelta(seconds=15))
    got = [s["token"] for s in env.apns.sent[batch:]]
    assert sorted(got) == sorted(toks) and env.sender.health(NOW)["sent"]["total"] == 100
    assert len(beats) == 1 + 13                                      # told systemd after every batch of the round


def test_a_few_failing_devices_do_not_stop_the_round(env):
    toks = many(env, 40)
    for t in toks[::3]:                                              # never 8 in a row
        env.apns.answers[t] = Result("retry", 503, "", "", "sandbox", 3)
    env.story()
    env.sender.tick(NOW)
    assert len(env.apns.sent) == 40


def test_a_refused_key_stops_the_round_too(env):
    toks = many(env, 30)
    for t in toks:
        env.apns.answers[t] = Result("auth", 403, "InvalidProviderToken", "", "sandbox", 2)
    env.story()
    env.sender.tick(NOW)
    assert len(env.apns.sent) == env.cfg["apns"]["concurrency"]
    assert any(m["key"] == "apns_auth" for m in env.ntfy.out)


def test_a_round_is_bounded_by_time_and_the_rest_go_next_loop(env):
    clock = [0.0]
    env.sender.mono = lambda: clock[0]
    toks = many(env, 40)
    real = env.apns.send

    def slow(*a, **k):
        clock[0] += 10                                               # every send takes 10 s of the round
        return real(*a, **k)
    env.apns.send = slow
    env.story()
    env.sender.tick(NOW)
    n = len(env.apns.sent)
    assert n == 16 and any("the round has run 120 s" in line for line in env.lines)     # 2 batches of 8
    env.sender.tick(NOW + timedelta(seconds=15))
    env.sender.tick(NOW + timedelta(seconds=30))
    assert sorted(s["token"] for s in env.apns.sent) == sorted(toks)                # each device exactly once


def test_a_busy_engine_database_is_logged_only_once_it_lasts(env):
    import sqlite3
    env.device()

    def locked():
        raise sqlite3.OperationalError("database is locked")
    class Busy:
        conn = None
        def close(self): pass
    env.sender.engine = lambda: Busy()
    env.sender.alertq = type("Q", (), {"ensure": staticmethod(lambda db: locked())})
    for i in range(3):
        env.sender.tick(NOW + timedelta(seconds=15 * i))
    assert not any("busy" in line for line in env.lines)
    env.sender.tick(NOW + timedelta(seconds=45))
    assert sum("engine state busy for 4 loops" in line for line in env.lines) == 1
