"""Loads server/config.yaml. Every setting has a default here so a partial file still runs."""
import copy
import os
from pathlib import Path

import yaml

SERVER_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SERVER_DIR / "config.yaml"
SOURCES_PATH = SERVER_DIR / "sources.yaml"

DEFAULTS = {
    "state_dir": "~/.techshorts",
    "tick_seconds": 60,
    "fetch": {
        "timeout_s": 15,
        "parallel": 8,
        "user_agent": "Mozilla/5.0 (compatible; TechgistBot/1.0)",
        "max_items_per_feed": 100,
        "max_age_days": 7,
        "max_age_days_reads": 14,
        "first_run_skip_hours": 36,
        "failure_alert_after": 6,
    },
    "check_every_hours": 24,
    "llm": {"base_url": "https://integrate.api.nvidia.com/v1", "model": None, "fallback_model": None, "daily_budget": 0,
            "rate_per_min": 30, "concurrency": 6, "timeout_s": 30, "max_items_per_tick": 18},
    "embed": {"model": None, "dims": None, "lead_words": 120},
    # Placeholders only; the real thresholds are calibrated into config.yaml (tools/label_pairs.py).
    "cluster": {"t_cand": 0.5, "t_join": 0.6, "t_signal": 0.5},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path=None):
    path = Path(path or os.environ.get("TECHGIST_CONFIG") or CONFIG_PATH)
    cfg = _merge(DEFAULTS, yaml.safe_load(path.read_text()) if path.exists() else {})
    cfg["state_dir"] = os.path.expanduser(os.environ.get("TECHGIST_STATE") or cfg["state_dir"])
    return cfg


def state_path(cfg, name):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    return os.path.join(cfg["state_dir"], name)
