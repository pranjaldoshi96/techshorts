#!/usr/bin/env python3
"""Is the published edition fresh? A check that runs outside the worker, so a hung or dead worker is noticed.

  check_freshness.py /var/lib/techgist/edition/edition.json --restart techgist-worker --quiet
      On the VPS, every minute from techgist-watchdog.timer (as root). When the edition's publishedAt is older
      than --max-age-min (15) the unit is restarted and the owner alerted. A worker that was (re)started less than
      --max-age-min ago is given that long to publish before it is restarted again; while it stays stale a
      reminder goes out every 30 min, and one message says when it is publishing again.
  check_freshness.py https://raw.githubusercontent.com/<owner>/<repo>/edition/edition.json
  check_freshness.py https://.../health.json
      From any other machine (cron, or .github/workflows/edition-freshness.yml): checks what readers are actually
      served, so it also notices the whole VPS being down. Accepts edition.json (publishedAt) or the health.json
      published beside it (edition.lastPublishAt). Add --alert to send an ntfy message when stale (NTFY_TOPIC in
      the environment).

Exit status: 0 fresh, 1 stale, 2 the file could not be read or understood. Standard library only; alert.py from
this directory sends the messages.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alert as alert_mod  # noqa: E402

STATE = "/run/techgist-watchdog.json"
REMIND_S = 1800


def published_at(doc):
    """The time of the last publish, from an edition.json or a health.json."""
    at = doc.get("publishedAt") or (doc.get("edition") or {}).get("lastPublishAt")
    if not at:
        raise KeyError("no publishedAt or edition.lastPublishAt")
    return datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def age_minutes(where, now=None):
    """(minutes since the last publish, None), or (None, why) when it cannot be read."""
    try:
        if where.startswith(("http://", "https://")):
            req = urllib.request.Request(where, headers={"Cache-Control": "no-cache", "User-Agent": "techgist-check"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
        else:
            with open(where, "rb") as f:
                body = f.read()
        at = published_at(json.loads(body))
    except (OSError, ValueError, KeyError, AttributeError) as e:
        return None, f"{type(e).__name__}: {e}"
    return ((now or datetime.now(timezone.utc)) - at).total_seconds() / 60, None


def unit_uptime_s(unit, run=subprocess.run):
    """Seconds since the unit became active, or None when it is not active (dead, failed, restarting)."""
    r = run(["systemctl", "show", unit, "-p", "ActiveState", "-p", "ActiveEnterTimestampMonotonic"],
            capture_output=True, text=True)
    props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    if props.get("ActiveState") != "active":
        return None
    try:
        return max(0.0, time.monotonic() - int(props["ActiveEnterTimestampMonotonic"]) / 1e6)
    except (KeyError, ValueError):
        return None


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, st):
    try:
        with open(path + ".tmp", "w") as f:
            json.dump(st, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def watchdog(what, unit, max_age_min, state_path=STATE, now=None, uptime=unit_uptime_s, run=subprocess.run,
             send=alert_mod.send):
    """The edition is stale: restart the unit unless it was started within the window, and tell the owner without
    repeating it every minute. Returns what was done: "restarted", "waiting" or "restart_failed"."""
    now = time.time() if now is None else now
    st = load_state(state_path)
    st.setdefault("stale_since", now)
    up = uptime(unit)
    if up is not None and up < max_age_min * 60:
        # Just (re)started, by this check, by systemd after a crash, or by a deploy: let it publish.
        done = "waiting"
        if now - st.get("alerted_at", 0) >= REMIND_S:
            if send("Techgist: edition is stale",
                    f"{what}. {unit} was started {up / 60:.0f} min ago and has not published yet; it is restarted "
                    f"again if it has not within {max_age_min:.0f} min of starting.", priority="high",
                    tags="warning"):
                st["alerted_at"] = now
    else:
        r = run(["systemctl", "restart", unit], capture_output=True, text=True)
        done = "restarted" if r.returncode == 0 else "restart_failed"
        st["restarts"] = st.get("restarts", 0) + 1
        state = "was not running" if up is None else f"was running (up {up / 60:.0f} min) but not publishing"
        if send("Techgist: edition is stale, worker restarted",
                f"{what}. {unit} {state}; " + ("restarted it" if done == "restarted" else
                                               f"the restart FAILED: {r.stderr.strip()[:200]}")
                + f" (restart {st['restarts']} of this outage).", priority="urgent" if done != "restarted" else "high",
                tags="rotating_light"):
            st["alerted_at"] = now
    save_state(state_path, st)
    return done


def recovered(age, state_path=STATE, now=None, send=alert_mod.send):
    """The edition is fresh. If the last runs found it stale, say so once."""
    st = load_state(state_path)
    if not st:
        return False
    now = time.time() if now is None else now
    try:
        os.remove(state_path)
    except OSError:
        pass
    # Only worth a message if the owner was told about the outage.
    if st.get("alerted_at"):
        send("Techgist: publishing again",
             f"The edition is fresh again (published {age:.0f} min ago) after "
             f"{(now - st.get('stale_since', now)) / 60:.0f} min stale and {st.get('restarts', 0)} restart(s).",
             tags="white_check_mark")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("where", help="path or URL of edition.json or health.json")
    ap.add_argument("--max-age-min", type=float, default=15)
    ap.add_argument("--restart", metavar="UNIT", help="systemd unit to restart when stale (needs root)")
    ap.add_argument("--alert", action="store_true", help="without --restart: send an ntfy message when not fresh")
    ap.add_argument("--quiet", action="store_true", help="print nothing when fresh")
    ap.add_argument("--state", default=STATE, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    age, why = age_minutes(args.where)
    if age is not None and age <= args.max_age_min:
        if args.restart:
            recovered(age, args.state)
        if not args.quiet:
            print(f"fresh: published {age:.0f} min ago (limit {args.max_age_min:.0f})")
        return 0
    what = f"The edition at {args.where} is {age:.0f} min old (limit {args.max_age_min:.0f})" if age is not None \
        else f"The edition at {args.where} cannot be read ({why})"
    if args.restart:
        print(f"{what}: {watchdog(what, args.restart, args.max_age_min, args.state)}", file=sys.stderr)
    else:
        print(what, file=sys.stderr)
        if args.alert:
            alert_mod.send("Techgist: published edition is stale", what + ". Checked from outside the server: the "
                           "worker, the VPS or the push to GitHub is down.", priority="urgent", tags="rotating_light")
    return 1 if age is not None else 2


if __name__ == "__main__":
    sys.exit(main())
