#!/usr/bin/env python3
"""Sends one message to the owner's ntfy topic. Standard library only; used by the systemd units and by
check_freshness.py, and usable by hand.

  alert.py --unit-failed techgist-worker.service
      From techgist-alert@.service (OnFailure= of the worker and the backup). systemd runs it every time the unit
      fails, also when Restart= is about to bring it back, so a crash is reported while the worker recovers. In a
      crash loop it sends one message per --every-min (15) and says how many failures it left out.
  alert.py --test
      One clearly labelled test message; exits 0 only if ntfy accepted it.
  alert.py --title "Techgist: something" "text"

The topic is NTFY_TOPIC from the environment or from /etc/techgist/env (root:techgist 640). It is a secret (anyone
who knows it can read and post alerts) and is never printed. Without a topic the message goes to stderr (the
journal) and the exit status is 3.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.request

ENV_FILE = "/etc/techgist/env"
NTFY_URL = "https://ntfy.sh"


def topic():
    t = os.environ.get("NTFY_TOPIC")
    if not t:
        try:
            for line in open(ENV_FILE):
                if line.startswith("NTFY_TOPIC="):
                    t = line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            pass
    return t or None


def send(title, message, priority=None, tags=None):
    """True when ntfy accepted the message. Prints what happened, never the topic."""
    t = topic()
    if not t:
        print(f"ALERT {title}: {message} (NTFY_TOPIC unset; not sent)", file=sys.stderr)
        return False
    headers = {"Title": title.encode("ascii", "replace").decode()}
    if priority:
        headers["Priority"] = str(priority)
    if tags:
        headers["Tags"] = tags
    try:
        req = urllib.request.Request(f"{NTFY_URL}/{t}", data=message.encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(2000)
        try:
            sent = json.loads(body).get("id")
        except ValueError:
            sent = None
        print(f"ALERT {title}: {message} (ntfy accepted, id {sent})", file=sys.stderr)
        return True
    except (OSError, ValueError) as e:
        print(f"ALERT {title}: {message} (ntfy failed: {type(e).__name__})", file=sys.stderr)
        return False


def state_dir():
    """Where the rate limit is remembered: systemd's runtime directory (tmpfs), else /tmp."""
    return (os.environ.get("RUNTIME_DIRECTORY") or "").split(":")[0] or "/tmp"


def _stamp(key, directory=None):
    return os.path.join(directory or state_dir(), "alert-" + "".join(c if c.isalnum() else "_" for c in key) + ".json")


def forget(key, directory=None):
    try:
        os.remove(_stamp(key, directory))
    except OSError:
        pass


def limited(key, every_s, now=None, directory=None):
    """Rate limit for a repeating alert. Returns (send now?, how many were left out since the last one sent)."""
    now = time.time() if now is None else now
    path = _stamp(key, directory)
    try:
        st = json.load(open(path))
    except (OSError, ValueError):
        st = {}
    go = now - st.get("sent_at", 0) >= every_s
    skipped = st.get("skipped", 0)
    st = {"sent_at": now, "skipped": 0} if go else {"sent_at": st.get("sent_at", 0), "skipped": skipped + 1}
    try:
        with open(path + ".tmp", "w") as f:
            json.dump(st, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
    return go, skipped


def unit_failed(unit, every_s):
    """The message for a failed unit. systemd passes what happened in MONITOR_* (systemd 251+)."""
    result = os.environ.get("MONITOR_SERVICE_RESULT") or "failed"
    code, status = os.environ.get("MONITOR_EXIT_CODE") or "", os.environ.get("MONITOR_EXIT_STATUS") or ""
    go, skipped = limited(f"unit-{unit}", every_s)
    how = {"signal": f"was killed by {status}", "watchdog": "stopped answering and was killed by the watchdog",
           "exit-code": f"exited with status {status}", "oom-kill": "was killed for using too much memory",
           "timeout": "timed out", "core-dump": f"crashed ({status})",
           "start-limit-hit": "failed to start too often"}.get(result, f"failed ({result} {code} {status})".strip())
    msg = f"{unit} on {socket.gethostname()} {how}."
    if unit.startswith("techgist-worker"):
        msg += " systemd restarts it automatically; the watchdog reports if the edition stays stale."
    if skipped:
        msg += f" {skipped} more failures since the last alert were not reported one by one."
    msg += f" See: journalctl -u {unit} -n 50"
    if not go:
        print(f"{msg} (not sent: one alert per {every_s // 60} min)", file=sys.stderr)
        return True
    ok = send(f"Techgist: {unit} failed", msg, priority="high", tags="warning")
    if not ok:
        # Not delivered: forget the stamp so the next failure tries again instead of waiting out the interval.
        forget(f"unit-{unit}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("message", nargs="?")
    ap.add_argument("--title", default="Techgist")
    ap.add_argument("--priority")
    ap.add_argument("--unit-failed", metavar="UNIT")
    ap.add_argument("--every-min", type=float, default=15)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    if not topic():
        send(args.title, args.message or (f"{args.unit_failed} failed" if args.unit_failed else "test"))
        return 3
    if args.test:
        ok = send("Techgist: TEST alert (no action needed)",
                  f"This is a test of Techgist's alert channel from {socket.gethostname()}, sent "
                  f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. Real alerts arrive here.", tags="white_check_mark")
    elif args.unit_failed:
        ok = unit_failed(args.unit_failed, int(args.every_min * 60))
    else:
        ok = send(args.title, args.message or "", priority=args.priority)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
