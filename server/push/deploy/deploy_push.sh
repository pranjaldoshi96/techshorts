#!/bin/bash
# Copies server/push/ to /opt/techgist-push on the VPS and restarts techgist-push.service.
#   server/push/deploy/deploy_push.sh            code, venv (when requirements changed), unit, restart
#   server/push/deploy/deploy_push.sh --key      also copy the APNs key from this Mac (never displayed)
#   server/push/deploy/deploy_push.sh --https    also run setup_https.sh there (nginx server block + certificate)
# It never touches /opt/techgist, the worker, or any nginx file but its own.
set -euo pipefail
HOST=${TECHGIST_VPS:-root@213.210.36.218}
KEY=${TECHGIST_VPS_KEY:-$HOME/.ssh/techgist_vps}
APNS_KEY=${TECHGIST_APNS_KEY:-$HOME/Desktop/AuthKey_K2DXJ7NMGX.p8}
HERE=$(cd "$(dirname "$0")/.." && pwd)
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$KEY")
DEST=/opt/techgist-push

"${SSH[@]}" "$HOST" "mkdir -p $DEST/push" </dev/null
rsync -az --delete -e "${SSH[*]}" --exclude __pycache__ --exclude '*.pyc' "$HERE/" "$HOST:$DEST/push/" </dev/null
REV=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)

for arg in "$@"; do
    if [ "$arg" = "--key" ]; then
        "${SSH[@]}" "$HOST" "install -d -o root -g techgist -m 750 /etc/techgist/apns" </dev/null
        scp -q -o BatchMode=yes -i "$KEY" "$APNS_KEY" "$HOST:/etc/techgist/apns/$(basename "$APNS_KEY")" </dev/null
        "${SSH[@]}" "$HOST" "chown root:techgist /etc/techgist/apns/*.p8 && chmod 640 /etc/techgist/apns/*.p8" </dev/null
        echo "APNs key copied"
    fi
done

"${SSH[@]}" "$HOST" "set -e
echo $REV > $DEST/REVISION
printf '%s\n' '#!/bin/sh' '# The push service by hand, as its own user (see push/cli.py).' 'cd $DEST && exec sudo -u techgist env TECHGIST_STATE=/var/lib/techgist PYTHONPATH=$DEST:/opt/techgist $DEST/venv/bin/python -m push.cli \"\$@\"' > $DEST/pushctl
chmod 755 $DEST/pushctl
chown -R root:root $DEST --no-dereference; chmod -R go+rX $DEST
[ -x $DEST/venv/bin/python ] || python3 -m venv $DEST/venv
if ! cmp -s $DEST/push/requirements.txt $DEST/venv/requirements.installed; then
    $DEST/venv/bin/pip install -q --disable-pip-version-check -r $DEST/push/requirements.txt
    cp $DEST/push/requirements.txt $DEST/venv/requirements.installed
fi
$DEST/venv/bin/python -m compileall -q $DEST/push >/dev/null
cmp -s $DEST/push/deploy/techgist-push.service /etc/systemd/system/techgist-push.service || {
    install -m 644 $DEST/push/deploy/techgist-push.service /etc/systemd/system/ && systemctl daemon-reload; }
systemctl enable techgist-push >/dev/null 2>&1
systemctl restart techgist-push
sleep 3
systemctl is-active techgist-push" </dev/null

for arg in "$@"; do
    if [ "$arg" = "--https" ]; then
        "${SSH[@]}" "$HOST" "bash $DEST/push/deploy/setup_https.sh" </dev/null
    fi
done
echo "deployed push $REV"
