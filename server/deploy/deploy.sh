#!/bin/bash
# Copies server/ to the VPS and restarts the worker. Run from a checkout:  server/deploy/deploy.sh [--setup]
#   --setup   also run deploy/setup_vps.sh there (first install, new requirements, or a changed tools/v1decode)
# State (/var/lib/techgist) and the venv are left alone. Nothing here pushes to GitHub: publish.git_remote is
# refused unless it is null, because this box is a test run until cutover.
set -euo pipefail
HOST=${TECHGIST_VPS:-root@213.210.36.218}
KEY=${TECHGIST_VPS_KEY:-$HOME/.ssh/techgist_vps}
HERE=$(cd "$(dirname "$0")/.." && pwd)
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=8 -i "$KEY")

if ! grep -Eq '^\s*git_remote:\s*null(\s|$)' "$HERE/config.yaml"; then
    echo "config.yaml sets publish.git_remote; refusing to deploy a test run that could push" >&2
    exit 1
fi
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
"${SSH[@]}" "$HOST" "/opt/techgist/venv/bin/python -m compileall -q /opt/techgist/worker /opt/techgist/tools >/dev/null; systemctl restart techgist-worker && sleep 3 && systemctl is-active techgist-worker" </dev/null
echo "deployed $REV"
