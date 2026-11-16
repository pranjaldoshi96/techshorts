"""A nightly backup of push.db (devices, stories taken, what each device got), beside the engine's own backups.

  python -m push.backup          from techgist-push-backup.timer, as the techgist user

One consistent snapshot through SQLite's backup API while the service keeps writing, checked with
PRAGMA integrity_check, written as <state>/backups/nightly/push-<UTC time>.db (mode 640: it holds device tokens);
the newest KEEP are kept. The engine's backup (deploy/backup_state.py) only ever touches state-*.db there.
A failed backup leaves nothing behind and exits 1, which the unit's OnFailure= turns into an alert.

To restore: stop techgist-push, copy the newest push-*.db over push.db (remove push.db-wal and push.db-shm), start
it. Devices that registered since the backup register again the next time their app starts.
"""
import os
import sqlite3
import sys
import time

from . import config

KEEP = 7


def backup(src, dest):
    tmp = dest + ".tmp"
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(tmp)
    try:
        try:
            s.backup(d)
            d.execute("PRAGMA journal_mode=DELETE")
            d.commit()
        finally:
            d.close()
            s.close()
        c = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            rows = c.execute("PRAGMA integrity_check").fetchall()
        finally:
            c.close()
        if rows != [("ok",)]:
            raise RuntimeError("the backup failed its integrity check: " + "; ".join(str(r[0]) for r in rows[:3]))
        os.chmod(tmp, 0o640)
        os.replace(tmp, dest)
    finally:
        for leftover in (tmp, tmp + "-journal", tmp + "-wal", tmp + "-shm"):
            if os.path.exists(leftover):
                os.remove(leftover)
    return os.path.getsize(dest)


def rotate(directory, keep=KEEP):
    names = sorted(n for n in os.listdir(directory) if n.startswith("push-") and n.endswith(".db"))
    gone = names[:-keep] if keep > 0 else []
    for n in gone:
        os.remove(os.path.join(directory, n))
    return gone


def main(state=None, now=None):
    cfg = config.load(state)
    out_dir = os.path.join(cfg["paths"]["state"], "backups", "nightly")
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"push-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}.db")
    try:
        size = backup(cfg["paths"]["push_db"], dest)
    except (sqlite3.Error, OSError, RuntimeError) as e:
        print(f"backup of push.db failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    gone = rotate(out_dir)
    print(f"backup {dest} ({size // 1024} KB, checked)" + (f"; removed {', '.join(gone)}" if gone else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
