#!/usr/bin/env python3
"""Is the published edition fresh? A check that runs outside the worker, so a hung or dead worker is noticed.

  check_freshness.py /var/lib/techgist/edition/edition.json --restart techgist-worker
      On the VPS, every 5 min from techgist-watchdog.timer (as root): when the edition's publishedAt is older than
      --max-age-min (30), alert and restart the unit (at most once per 30 min).
  check_freshness.py https://raw.githubusercontent.com/<owner>/<repo>/edition/edition.json
      From cron on any other machine, after cutover: checks what readers are actually served.

Alerts go to ntfy when NTFY_TOPIC is set (in the environment or in /etc/techgist/env), else to stderr.
Exit status 0 = fresh, 1 = stale or unreadable. Standard library only.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

STAMP = "/run/techgist-watchdog.restart"


def topic():
    t = os.environ.get("NTFY_TOPIC")
    if not t:
        try:
            for line in open("/etc/techgist/env"):
                if line.startswith("NTFY_TOPIC="):
                    t = line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            pass
    return t


def alert(message):
    print(message, file=sys.stderr)
    t = topic()
    if not t:
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{t}", data=message.encode(),
                                     headers={"Title": "Techgist: edition is stale"})
        urllib.request.urlopen(req, timeout=10)
    except OSError as e:
        print(f"ntfy failed: {type(e).__name__}", file=sys.stderr)


def age_minutes(where):
    """Minutes since the edition's publishedAt, or (None, why) when it cannot be read."""
    try:
        if where.startswith(("http://", "https://")):
            req = urllib.request.Request(where, headers={"Cache-Control": "no-cache", "User-Agent": "techgist-check"})
            body = urllib.request.urlopen(req, timeout=20).read()
        else:
            body = open(where, "rb").read()
        at = datetime.strptime(json.loads(body)["publishedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError) as e:
        return None, f"{type(e).__name__}: {e}"
    return (datetime.now(timezone.utc) - at).total_seconds() / 60, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("where", help="path or URL of edition.json")
    ap.add_argument("--max-age-min", type=float, default=30)
    ap.add_argument("--restart", metavar="UNIT", help="systemd unit to restart when stale (needs root)")
    args = ap.parse_args()
    age, why = age_minutes(args.where)
    if age is not None and age <= args.max_age_min:
        print(f"fresh: published {age:.0f} min ago")
        return 0
    what = f"edition at {args.where} is {age:.0f} min old (limit {args.max_age_min:.0f})" if age is not None \
        else f"edition at {args.where} cannot be read ({why})"
    if args.restart:
        try:
            recent = time.time() - os.path.getmtime(STAMP) < 1800
        except OSError:
            recent = False
        if recent:
            alert(f"{what}; {args.restart} was restarted less than 30 min ago and is still not publishing")
        else:
            open(STAMP, "w").close()
            r = subprocess.run(["systemctl", "restart", args.restart], capture_output=True, text=True)
            alert(f"{what}; restarted {args.restart}" + ("" if r.returncode == 0 else f" (failed: {r.stderr.strip()})"))
    else:
        alert(what)
    return 1


if __name__ == "__main__":
    sys.exit(main())
