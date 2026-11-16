#!/bin/bash
# Copies server/ to the VPS and restarts the worker. Run from a checkout:  server/deploy/deploy.sh [--setup]
#   --setup   also run deploy/setup_vps.sh there (first install, new requirements, or a changed tools/v1decode)
# State (/var/lib/techgist) and the venv are left alone. The worker it restarts pushes the edition to GitHub when
# publish.git_remote is set (see the guard below).
set -euo pipefail
HOST=${TECHGIST_VPS:-root@213.210.36.218}
KEY=${TECHGIST_VPS_KEY:-$HOME/.ssh/techgist_vps}
HERE=$(cd "$(dirname "$0")/.." && pwd)
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=8 -i "$KEY")

# Since the cutover (2026-11-16) this box is the live publisher: it force-pushes the edition branch that the app
# reads. The only remote a deploy accepts is that repository, or null (the rollback: nothing is pushed). Anything
# else, or a branch other than `edition`, is refused, so a typo cannot replace some other branch or repository.
LIVE_REMOTE=git@github.com:pranjaldoshi96/techshorts.git
remote=$(sed -nE 's/^[[:space:]]+git_remote:[[:space:]]*([^[:space:]#]+).*/\1/p' "$HERE/config.yaml")
branch=$(sed -nE 's/^[[:space:]]+git_branch:[[:space:]]*([^[:space:]#]+).*/\1/p' "$HERE/config.yaml")
if [ "$remote" != null ] && [ "$remote" != "$LIVE_REMOTE" ]; then
    echo "config.yaml: publish.git_remote is '$remote'; only null or $LIVE_REMOTE can be deployed" >&2
    exit 1
fi
if [ "$branch" != edition ]; then
    echo "config.yaml: publish.git_branch is '$branch'; only 'edition' can be deployed" >&2
    exit 1
fi
echo "publish.git_remote: $remote"
rsync -az --delete -e "${SSH[*]}" \
    --exclude /venv --exclude /REVISION --exclude __pycache__ --exclude .pytest_cache --exclude '*.pyc' \
    --exclude /main.swift --exclude /publish.sh --exclude /enrich.py --exclude /editorial.example.json \
    "$HERE/" "$HOST:/opt/techgist/" </dev/null
REV=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)
"${SSH[@]}" "$HOST" "echo $REV > /opt/techgist/REVISION; chown -R root:root /opt/techgist --no-dereference 2>/dev/null; chmod -R go+rX /opt/techgist" </dev/null
if [ "${1:-}" = "--setup" ]; then
    "${SSH[@]}" "$HOST" "nohup bash /opt/techgist/deploy/setup_vps.sh > /var/log/techgist-setup.log 2>&1 &" </dev/null
    echo "setup running on the VPS; follow /var/log/techgist-setup.log, then: systemctl restart techgist-worker"
    exit 0
fi
# The unit files are part of the deploy: a changed one (a new key file, a restart policy) is installed before the
# restart, and the watchdog and backup timers are enabled. The worker itself stays enabled, so it starts at boot.
"${SSH[@]}" "$HOST" 'changed=0; for f in /opt/techgist/deploy/techgist-*.service /opt/techgist/deploy/techgist-*.timer; do u=$(basename "$f"); cmp -s "$f" "/etc/systemd/system/$u" || { install -m 644 "$f" /etc/systemd/system/ && changed=1; }; done; [ $changed = 1 ] && systemctl daemon-reload; systemctl enable techgist-worker >/dev/null 2>&1; systemctl enable --now techgist-watchdog.timer techgist-backup.timer >/dev/null 2>&1; [ $changed = 1 ] && systemctl restart techgist-watchdog.timer; true' </dev/null
"${SSH[@]}" "$HOST" "/opt/techgist/venv/bin/python -m compileall -q /opt/techgist/worker /opt/techgist/tools >/dev/null; systemctl restart techgist-worker && sleep 3 && systemctl is-active techgist-worker" </dev/null
echo "deployed $REV"
