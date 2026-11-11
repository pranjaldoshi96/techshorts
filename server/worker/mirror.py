"""Pushes the published directory to the repo's `edition` branch, where 1.2 reads edition.json (spec 2.13).

Off unless `publish.git_remote` is set (it stays off until cutover, while publish.sh still owns the branch). Same
scheme as publish.sh: one commit, amended and force-pushed, so the branch never grows; images already on GitHub
aren't uploaded again because the local repo is kept. At most once per `publish.git_every_min` (10 min).
"""
import logging
import os
import subprocess
from datetime import timedelta

from .db import from_iso, iso

log = logging.getLogger("techgist")
EXCLUDE = "*.tmp\n*.rejected\n"


def _git(out_dir, *args, env=None):
    return subprocess.run(["git", "-C", out_dir, *args], capture_output=True, text=True, timeout=300,
                          env={**os.environ, **(env or {})})


def push(db, cfg, out_dir, now, force=False):
    """Returns None when off, "waiting" inside the interval, "pushed", or "failed: <why>"."""
    p = cfg.get("publish") or {}
    remote = p.get("git_remote")
    if not remote:
        return None
    last = from_iso(db.get_meta("mirror_at"))
    if not force and last and now - last < timedelta(minutes=p.get("git_every_min", 10)):
        return "waiting"
    branch = p.get("git_branch", "edition")
    if not os.path.isdir(os.path.join(out_dir, ".git")):
        _git(out_dir, "init", "-q", "-b", branch)
    with open(os.path.join(out_dir, ".git", "info", "exclude"), "w") as f:
        f.write(EXCLUDE)
    open(os.path.join(out_dir, ".nojekyll"), "a").close()
    _git(out_dir, "add", "-A")
    author = ["-c", "user.name=Techgist worker", "-c", "user.email=techshorts-publisher@users.noreply.github.com"]
    has_head = _git(out_dir, "rev-parse", "-q", "--verify", "HEAD").returncode == 0
    msg = f"Edition {iso(now)}"
    r = _git(out_dir, *author, "commit", "-q", *(["--amend"] if has_head else []), "--allow-empty", "-m", msg)
    if r.returncode != 0:
        return f"failed: commit {r.stderr.strip()[:200]}"
    env = {}
    key = os.path.expanduser(p.get("deploy_key") or "")
    if key and os.path.exists(key):
        env["GIT_SSH_COMMAND"] = f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    r = _git(out_dir, "push", "-q", "-f", remote, f"HEAD:{branch}", env=env)
    db.set_meta("mirror_at", iso(now))
    if r.returncode != 0:
        log.warning("mirror push failed: %s", r.stderr.strip()[:200])
        return f"failed: push {r.stderr.strip()[:200]}"
    return "pushed"
