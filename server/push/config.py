"""Every limit and path of the push service. Defaults are here; a JSON file (TECHGIST_PUSH_CONFIG, or
push.json in the state directory) overrides any of them, section by section, and is read at start."""
import copy
import json
import os

DEFAULTS = {
    "sending_enabled": True,
    "loop_s": 15,
    "api": {
        "host": "127.0.0.1",        # never anything else: nginx is the only way in
        "port": 8787,
        "max_body": 2048,
        "ip_per_min": 60,           # requests per client address per minute
        "token_per_min": 30,        # requests per device token per minute (spec 6.5)
        "max_devices": 500000,
    },
    "apns": {
        "key_file": "/etc/techgist/apns/AuthKey_K2DXJ7NMGX.p8",
        "key_id": "K2DXJ7NMGX",
        "team_id": "A4HLS6BV3C",
        "topic": "com.pranjald.techgist",
        "jwt_ttl_s": 2400,          # Apple accepts a token for 60 min and rejects one renewed under 20 min
        "sound": "techgist.caf",
        "expiration_s": 10800,      # APNs stops trying to deliver a story after 3 h (spec 6.4)
        "timeout_s": 10,
        "attempts": 3,              # per send, on 429 / 5xx / a network error
        "backoff_s": [0.5, 2, 5],
        "concurrency": 8,
    },
    "rules": {
        "min_spice": 7,
        "daily_cap": 2,             # stories per device per local day (spec 6.2)
        "daily_cap_hard": 3,        # ... including the ones importance let past daily_cap (spec 6.3); 0 = no limit
        "override_importance": 9,   # a story this important may exceed daily_cap, never quiet hours
        "min_gap_min": 180,         # between two alerts on one device (spec 6.2)
        "quiet_start": "22:00",     # device-local; nothing is sent from here ...
        "quiet_end": "07:30",       # ... to here. The best story of the night is held and sent after.
        "max_age_h": 3,             # by the story's news date
        "held_max_age_h": 12,       # the held story may be older than max_age_h, up to this
        "owner_hold_s": 300,        # a story waits this long after the owner was told, so it can be killed
    },
    "owner": {
        "relay_ntfy": True,         # operational alerts on the ntfy topic also go to owner devices
        "poll_s": 60,
        "ntfy_url": "https://ntfy.sh",
    },
}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(state_dir=None, path=None):
    cfg = copy.deepcopy(DEFAULTS)
    state = state_dir or os.environ.get("TECHGIST_STATE") or "/var/lib/techgist"
    cfg["paths"] = {
        "state": state,
        "engine_db": os.path.join(state, "state.db"),
        "push_db": os.path.join(state, "push.db"),
        "edition": os.path.join(state, "edition", "edition.json"),
        "health": os.path.join(state, "push_health.json"),
    }
    path = path or os.environ.get("TECHGIST_PUSH_CONFIG") or os.path.join(state, "push.json")
    try:
        with open(path) as f:
            _merge(cfg, json.load(f))
    except FileNotFoundError:
        pass
    return cfg
