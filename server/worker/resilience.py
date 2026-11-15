"""Staying up: the systemd watchdog heartbeat, alerts that need no database, a state.db that is checked before it is
used, and what a failed tick leads to. Standard library only, so a broken dependency cannot take this down too.

  heartbeat()        tells systemd the loop is moving (WatchdogSec= in the unit). A tick that hangs in a network or
                     LLM call stops calling it and the worker is killed and restarted.
  ntfy()             one message to the owner's ntfy topic (NTFY_TOPIC); True only when ntfy accepted it.
  alert()            ntfy at most once per key per interval, remembered in a file (the runtime directory, which is a
                     tmpfs: it works with a full disk and a broken database).
  open_state()       opens state.db; a corrupt file is set aside and the newest good backup restored (or a fresh
                     state started), with an alert either way, instead of a crash loop.
  TickGuard          after a failed tick: roll back, name the cause (disk full, corrupt, locked), alert, and exit for
                     a clean restart when it does not clear.
"""
import json
import logging
import os
import shutil
import socket
import sqlite3
import time
import urllib.request

log = logging.getLogger("techgist")

NTFY_URL = "https://ntfy.sh"
# Exit statuses the unit restarts from like any other; they only make the journal readable.
EXIT_TICKS_FAILING = 70
EXIT_DB_CORRUPT = 71
# A tick fails this many times in a row (five minutes) before the process gives up and lets systemd start a new one.
MAX_FAILED_TICKS = 5
# Free space under which the owner is told, long before SQLite or the edition write fails.
DISK_LOW_BYTES = 1 << 30
DISK_LOW_SHARE = 0.03


def sd_notify(state):
    """Sends one sd_notify datagram. False outside systemd (no NOTIFY_SOCKET) or when it cannot be sent."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    if path.startswith("@"):
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(state.encode(), path)
        return True
    except OSError:
        return False


def heartbeat(status=None):
    """The loop is alive. Called at the start and end of a tick and between its stages."""
    return sd_notify("WATCHDOG=1" + (f"\nSTATUS={status}" if status else ""))


def ntfy(title, message, priority=None, tags=None, topic=None, opener=urllib.request.urlopen):
    """Posts to the owner's ntfy topic. Returns True when ntfy accepted the message, False when the topic is unset
    or the post failed. The topic is a secret: it is never logged."""
    topic = topic or os.environ.get("NTFY_TOPIC")
    if not topic:
        log.warning("ALERT %s: %s (NTFY_TOPIC unset; not sent)", title, message)
        return False
    # HTTP headers are latin-1; a title with anything else would raise before the request is sent.
    headers = {"Title": title.encode("ascii", "replace").decode()}
    if priority:
        headers["Priority"] = str(priority)
    if tags:
        headers["Tags"] = tags
    try:
        req = urllib.request.Request(f"{NTFY_URL}/{topic}", data=message.encode(), headers=headers, method="POST")
        with opener(req, timeout=10) as r:
            body = r.read(2000)
        try:
            sent = json.loads(body).get("id")
        except ValueError:
            sent = None
        log.warning("ALERT %s: %s (ntfy accepted, id %s)", title, message, sent)
        return True
    except (OSError, ValueError) as e:
        log.warning("ALERT %s: %s (ntfy failed: %s)", title, message, type(e).__name__)
        return False


def stamp_dir(state_dir=None):
    """Where alert() remembers what it sent: systemd's runtime directory (tmpfs), else the state directory."""
    d = (os.environ.get("RUNTIME_DIRECTORY") or "").split(":")[0]
    if not d:
        d = os.path.join(state_dir or os.path.expanduser(os.environ.get("TECHGIST_STATE") or "~/.techshorts"), "run")
    return d


def alert(key, title, message, every_s=3600, priority="high", state_dir=None, now=None, send=ntfy):
    """ntfy at most once per key per `every_s`. The stamp is written only when the message was accepted, so an
    alert that could not be sent is tried again the next time the condition is seen."""
    now = time.time() if now is None else now
    d = stamp_dir(state_dir)
    path = os.path.join(d, "alert-" + "".join(ch if ch.isalnum() else "_" for ch in key))
    try:
        if now - os.path.getmtime(path) < every_s:
            return False
    except OSError:
        pass
    if not send(title, message, priority=priority):
        return False
    try:
        os.makedirs(d, exist_ok=True)
        with open(path, "w"):
            pass
        os.utime(path, (now, now))
    except OSError:
        pass
    return True


# The database

def quick_check(path):
    """None when SQLite finds the file sound, else what is wrong with it. A missing file is sound (a fresh state)."""
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(path, timeout=30)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return str(e)
    return None if rows == [("ok",)] else "; ".join(str(r[0]) for r in rows[:3])


def backups(state_dir):
    """Backup files, newest first: the nightly ones (deploy/backup_state.py) and the hand-made ones beside them."""
    d = os.path.join(state_dir, "backups")
    found = []
    for sub in ("nightly", ""):
        try:
            names = os.listdir(os.path.join(d, sub))
        except OSError:
            continue
        found += [os.path.join(d, sub, n) for n in names if n.startswith("state-") and n.endswith(".db")]
    # state-<UTC time>.db: the name sorts by time.
    return sorted(found, key=os.path.basename, reverse=True)


def is_corruption(exc):
    """Is this the database file itself being broken (as opposed to locked, or the disk being full)?"""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    text = str(exc).lower()
    return "malformed" in text or "not a database" in text or "disk image" in text or "corrupt" in text


def is_locked(exc):
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def is_disk_full(exc):
    if isinstance(exc, OSError) and exc.errno in (28, 122):          # ENOSPC, EDQUOT
        return True
    return isinstance(exc, sqlite3.DatabaseError) and "full" in str(exc).lower()


def recover_state(path, state_dir, stamp=None, notify=alert):
    """Called before the worker opens state.db. A corrupt file is moved aside (never deleted) and replaced by the
    newest backup that passes the same check; with no good backup the worker starts from an empty state, which
    publishes again within minutes (a source's first poll only takes the last day and a half). Returns what was
    done: None (the file was sound), "restored:<backup>" or "fresh"."""
    problem = quick_check(path)
    if problem is None:
        return None
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.replace(path + suffix, f"{path}.corrupt-{stamp}{suffix}")
    for b in backups(state_dir):
        if quick_check(b) is None:
            # A SQLite backup is one complete file; copy it under a temporary name so a crash here leaves no
            # half-written state.db behind.
            shutil.copyfile(b, path + ".restoring")
            os.replace(path + ".restoring", path)
            done = f"restored:{os.path.basename(b)}"
            break
    else:
        done = "fresh"
    # Stories from before this moment may already have been alerted on by the state that was lost; the alert queue
    # will not queue them again (alertq.FLOOR_KEY).
    floor = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('alertq_floor', ?)", (floor,))
        conn.commit()
    finally:
        conn.close()
    what = (f"state.db was corrupt ({problem[:120]}). It was set aside as state.db.corrupt-{stamp} and "
            + (f"the backup {done.split(':', 1)[1]} was restored; items since that backup are fetched again."
               if done != "fresh" else "no usable backup was found, so the worker started from an empty state."))
    log.error(what)
    notify("db_corrupt", "Techgist: state.db was corrupt", what, every_s=0, priority="urgent", state_dir=state_dir)
    return done


def disk_free(path):
    """(free bytes, share free) of the filesystem holding `path`."""
    u = shutil.disk_usage(path)
    return u.free, u.free / u.total if u.total else 1.0


def disk_low(path):
    """A message when the disk is nearly full, else None."""
    try:
        free, share = disk_free(path)
    except OSError:
        return None
    if free < DISK_LOW_BYTES or share < DISK_LOW_SHARE:
        return f"{free / (1 << 20):.0f} MB free ({share:.1%}) on the disk holding {path}"
    return None


class TickGuard:
    """What a failed tick leads to. One failed tick is retried (a bad sources.yaml edit, a hiccup); the cause is
    named and alerted on, and a failure that does not clear ends the process so systemd starts a clean one:

      corrupt database   alert and exit at once; the next start runs recover_state().
      disk full          alert at once and keep ticking (a restart frees nothing); alerts repeat hourly.
      locked, anything   after MAX_FAILED_TICKS in a row, alert and exit.
    """

    def __init__(self, state_dir, max_failed=MAX_FAILED_TICKS, notify=alert):
        self.state_dir, self.max_failed, self.notify = state_dir, max_failed, notify
        self.failed = 0

    def ok(self):
        self.failed = 0

    def failure(self, db, exc):
        """Returns an exit status when the process should end, else None."""
        self.failed += 1
        try:
            # A tick that died half-way must not have its partial writes committed by the next one.
            db.conn.rollback()
        except sqlite3.Error:
            pass
        why = f"{type(exc).__name__}: {str(exc)[:160]}"
        if is_corruption(exc):
            self.notify("db_corrupt_seen", "Techgist: state.db is corrupt",
                        f"A tick failed with {why}. The worker is restarting and will restore the newest good backup.",
                        every_s=600, priority="urgent", state_dir=self.state_dir)
            return EXIT_DB_CORRUPT
        if is_disk_full(exc):
            try:
                free = f"{disk_free(self.state_dir)[0] / (1 << 20):.0f} MB free"
            except OSError:
                free = "free space unknown"
            self.notify("disk_full", "Techgist: disk full",
                        f"A tick failed with {why} ({free} on {self.state_dir}). Nothing is published until space "
                        f"is freed; the worker keeps trying every minute.", priority="urgent",
                        state_dir=self.state_dir)
            return None
        if self.failed >= self.max_failed:
            cause = "state.db is locked by another process" if is_locked(exc) else "the same or another error"
            self.notify("ticks_failing", "Techgist: worker restarting after failed ticks",
                        f"{self.failed} ticks in a row failed ({cause}); the last: {why}. Exiting so systemd starts "
                        f"a clean worker.", every_s=1800, state_dir=self.state_dir)
            return EXIT_TICKS_FAILING
        return None
