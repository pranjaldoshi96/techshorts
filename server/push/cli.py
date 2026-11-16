"""The push service by hand. On the VPS:  /opt/techgist-push/pushctl <command>

  status                      the health file's counts, live
  devices                     registered devices: first six characters of the token, env, time zone, owner flag
  owner PREFIX | --latest     mark a device as the owner's: it also gets server alerts (the ntfy ones) by APNs
  owner PREFIX --off          ... and unmark it
  kill EVENT_ID               stop a story before it is sent (during the owner hold, or while it waits its turn)
  test PREFIX ["title" "body"]    send one labelled test notification to a device now, outside every rule, and
                              print APNs' answer. A device APNs rejects is removed, as in normal sending.
  test PREFIX --event EVENT_ID    the same, but the story itself, exactly as a real alert carries it (a tap opens it)

PREFIX is the start of a device token (six characters are usually enough). Full tokens are never printed.
"""
import argparse
import json
import sys

from . import config
from .apns import Apns, sound_for, story_payload
from .sender import Edition, EngineDB, Sender, alert_text, utcnow
from .store import Store, short


def _one(store, prefix):
    rows = store.find(prefix)
    if len(rows) != 1:
        sys.exit(f"{len(rows)} devices start with that; give a longer prefix" if rows else "no such device")
    return rows[0]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pushctl")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    sub.add_parser("devices")
    o = sub.add_parser("owner")
    o.add_argument("prefix", nargs="?")
    o.add_argument("--latest", action="store_true", help="the device that registered most recently")
    o.add_argument("--off", action="store_true")
    sub.add_parser("kill").add_argument("event")
    t = sub.add_parser("test")
    t.add_argument("prefix")
    t.add_argument("text", nargs="*")
    t.add_argument("--event", help="send this edition story as a real alert would look")
    a = ap.parse_args(argv)
    cfg = config.load()
    store = Store(cfg["paths"]["push_db"])
    now = utcnow()
    if a.cmd in (None, "status"):
        print(json.dumps(Sender(cfg, store, None).health(now), indent=1))
    elif a.cmd == "devices":
        for d in store.devices():
            print(f"{short(d['token'])} {d['env']:<10} {d['tz']:<24} v{d['app_version'] or '?':<8}"
                  f" {'OWNER' if d['owner'] else '     '} registered {d['created_at']} seen {d['updated_at']}")
        print(f"{store.device_count()} devices")
    elif a.cmd == "owner":
        rows = store.latest() if a.latest else [_one(store, a.prefix or sys.exit("give a token prefix or --latest"))]
        if not rows:
            sys.exit("no devices registered")
        store.set_owner(rows[0]["token"], not a.off)
        print(f"{short(rows[0]['token'])} is {'no longer ' if a.off else ''}an owner device")
    elif a.cmd == "kill":
        n = store.kill(a.event)
        try:
            from worker import alertq
            db = EngineDB(cfg["paths"]["engine_db"], timeout=30)
            n += alertq.cancel(db, a.event)
            db.commit()
            db.close()
        except Exception as e:
            print(f"(engine queue not touched: {type(e).__name__})")
        print("killed" if n else "nothing waiting under that id")
    elif a.cmd == "test":
        d = _one(store, a.prefix)
        if a.event:
            # The story as a real alert carries it (text, link and picture from the edition), outside the rules.
            art = Edition(cfg["paths"]["edition"]).articles().get(a.event)
            title, body = alert_text({"title": "", "body": ""}, art)
            if not title or not body:
                sys.exit("that event is not in the edition, or has no notification text")
            payload = story_payload({"event_id": a.event, "title": title, "body": body}, art, sound_for(d, cfg["apns"]["sound"]))
        else:
            title, body = a.text if len(a.text) == 2 else ("Techgist test", "A test notification from the push server.")
            payload = {"aps": {"alert": {"title": title, "body": body}, "sound": sound_for(d, cfg["apns"]["sound"]),
                               "thread-id": "test"}, "kind": "test"}
        sender = Sender(cfg, store, Apns(cfg["apns"]))
        res = sender.apns.send(d["token"], d["env"], payload, collapse_id="test-" + now.strftime("%H%M%S"),
                               expiration=int(now.timestamp()) + 600)
        sender._settle(d, None, res, now)
        print(f"APNs: {res.outcome} status={res.status} reason={res.reason or '-'} host={res.env}"
              f" attempts={res.attempts} apns-id={res.apns_id or '-'}")
        return 0 if res.outcome == "ok" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
