"""techgist-push.service: the registry's HTTP server and the sending loop, in one process.

  python -m push.main            (PYTHONPATH holds this directory's parent and the engine's code, for worker.alertq)

It tells systemd it is alive after every tick and between the batches of a round of sends (WatchdogSec in the
unit); a tick that keeps failing ends the process
so systemd starts a clean one.
"""
import os
import socket
import sys
import time

from . import api, config
from .apns import Apns
from .sender import Ntfy, Sender
from .store import Store


def sd_notify(msg):
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(path)
            s.send(msg.encode())
    except OSError:
        pass


def log(msg):
    print(msg, flush=True)


def main():
    cfg = config.load()
    try:
        from worker import alertq
    except ImportError:
        alertq = None
        log("worker.alertq not importable: registry only, nothing is taken from the engine's queue")
    store = Store(cfg["paths"]["push_db"])
    ntfy = Ntfy(cfg)
    sender = Sender(cfg, store, Apns(cfg["apns"]), alertq=alertq, ntfy=ntfy, log=log,
                    heartbeat=lambda: sd_notify("WATCHDOG=1"))
    api.serve(cfg, lambda: Store(cfg["paths"]["push_db"]), sender.alive, log)
    key_ok = os.access(cfg["apns"]["key_file"], os.R_OK)
    log(f"push service up on {cfg['api']['host']}:{cfg['api']['port']}; sending "
        f"{'on' if cfg['sending_enabled'] else 'off'}; APNs key {'readable' if key_ok else 'NOT readable'}; "
        f"{store.device_count()} devices")
    if not key_ok and cfg["sending_enabled"]:
        ntfy.send("Techgist push: APNs key not readable", "The push service started but cannot read its APNs key "
                  "file; nothing can be sent until it can.", key="apns_key", priority="high", tags="warning", log=log)
    sd_notify("READY=1")
    failures = 0
    while True:
        try:
            sender.tick()
            failures = 0
        except Exception as e:
            failures += 1
            log(f"tick failed ({failures} in a row): {type(e).__name__}: {e}")
            if failures >= 10:
                ntfy.send("Techgist push: the sending loop keeps failing",
                          f"10 ticks in a row failed ({type(e).__name__}); the service is restarting itself.",
                          key="tick_failing", priority="high", tags="warning", log=log)
                sys.exit(1)
        sd_notify("WATCHDOG=1")
        time.sleep(cfg["loop_s"])


if __name__ == "__main__":
    main()
