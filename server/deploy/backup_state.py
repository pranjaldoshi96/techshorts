#!/usr/bin/env python3
"""A consistent backup of state.db, checked, with rotation. Standard library only.

  backup_state.py                      nightly, from techgist-backup.timer (as the techgist user)
  backup_state.py --label predeploy    by hand before a risky change: backups/state-<time>.db

Nightly backups go to <state>/backups/nightly/state-<UTC time>.db; the newest --keep (7) are kept. Hand-made ones
(backups/state-<time>.db) are thinned to the newest --keep-manual (5). Anything else in backups/ (the run1-* directory,
saved editions) is never touched.

The copy is made with SQLite's backup API in one step: a single read snapshot of the live database, taken while
the worker keeps writing (WAL), so the file is a complete database as of one instant. It is then checked with
PRAGMA integrity_check; a backup that fails the check is deleted and the exit status is 1, which the unit's
OnFailure= turns into an alert. The worker restores from the newest backup that passes when it finds state.db
corrupt at start (worker/resilience.py).
"""
import argparse
import os
import sqlite3
import sys
import time


def backup(src, dest):
    """Copies the live database to `dest` atomically (written under a temporary name). Returns its size."""
    tmp = dest + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(tmp)
    try:
        try:
            s.backup(d)                 # pages=-1: the whole database in one step, one snapshot
            # The copy is a plain file to be opened later, possibly from a read-only place: no WAL beside it.
            d.execute("PRAGMA journal_mode=DELETE")
            d.commit()
        finally:
            d.close()
            s.close()
        problem = check(tmp)
        if problem:
            raise RuntimeError(f"the backup failed its integrity check: {problem}")
        os.replace(tmp, dest)
    finally:
        # Nothing half-made is left behind, whatever went wrong.
        for leftover in (tmp, tmp + "-journal", tmp + "-wal", tmp + "-shm"):
            if os.path.exists(leftover):
                os.remove(leftover)
    return os.path.getsize(dest)


def check(path):
    """None when the file is a sound database, else what is wrong."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = c.execute("PRAGMA integrity_check").fetchall()
        finally:
            c.close()
    except sqlite3.DatabaseError as e:
        return str(e)
    return None if rows == [("ok",)] else "; ".join(str(r[0]) for r in rows[:3])


def rotate(directory, keep):
    """Removes all but the newest `keep` state-<time>.db files (and their -wal/-shm) in `directory`."""
    try:
        names = sorted(n for n in os.listdir(directory) if n.startswith("state-") and n.endswith(".db"))
    except OSError:
        return []
    gone = names[:-keep] if keep > 0 else []
    for n in gone:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.path.join(directory, n + suffix))
            except OSError:
                pass
    return gone


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.environ.get("TECHGIST_STATE") or os.path.expanduser("~/.techshorts"))
    ap.add_argument("--label", help="a hand-made backup (backups/state-<time>.db) instead of a nightly one")
    ap.add_argument("--keep", type=int, default=7)
    ap.add_argument("--keep-manual", type=int, default=5)
    args = ap.parse_args(argv)
    src = os.path.join(args.state, "state.db")
    root = os.path.join(args.state, "backups")
    out_dir = root if args.label else os.path.join(root, "nightly")
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"state-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db")
    started = time.monotonic()
    try:
        size = backup(src, dest)
    except (sqlite3.Error, OSError, RuntimeError) as e:
        print(f"backup of {src} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    gone = rotate(os.path.join(root, "nightly"), args.keep) + rotate(root, args.keep_manual)
    print(f"backup {dest} ({size / (1 << 20):.1f} MB, checked, {time.monotonic() - started:.1f} s)"
          + (f"; removed {', '.join(gone)}" if gone else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
