#!/bin/bash
# One-time (and safe to repeat) setup of the Techgist worker on the VPS. Run as root ON the VPS, after
# deploy/deploy.sh has copied the code to /opt/techgist. It never touches nginx or sshd.
#   user techgist (system, no shell) · code /opt/techgist · venv /opt/techgist/venv · state /var/lib/techgist
#   keys /etc/techgist (root:techgist, dir 750, files 640) · firewall: OpenSSH, 80, 443
set -euo pipefail
SWIFT_URL=${SWIFT_URL:-https://download.swift.org/swift-6.2-release/ubuntu2404/swift-6.2-RELEASE/swift-6.2-RELEASE-ubuntu24.04.tar.gz}
APP=/opt/techgist
STATE=/var/lib/techgist

export DEBIAN_FRONTEND=noninteractive
# The provider's mirror list (sources.list.d/ubuntu-mirrors.list) points at a mirror that no longer exists, and apt
# picks it first. These options make this script's apt calls use the official archive only, without editing any
# apt configuration or dropping the other lists.
APT=(apt-get -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/ubuntu.sources -o Dir::Etc::sourceparts=/dev/null
     -o APT::Get::List-Cleanup=0)
"${APT[@]}" update -qq
# python3-venv for the venv; the rest is what the Swift toolchain needs to link the 1.2 decoder.
"${APT[@]}" install -y -qq python3-venv rsync ufw binutils libc6-dev libgcc-13-dev libstdc++-13-dev zlib1g-dev \
    libcurl4-openssl-dev libxml2-dev libedit2 libsqlite3-0 libncurses6 libpython3.12 tzdata pkg-config >/dev/null

id techgist >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin techgist
install -d -o techgist -g techgist -m 750 "$STATE" "$STATE/edition" "$STATE/bin"
install -d -o root -g root -m 755 "$APP"

# Key files: readable by the service's group only, still closed to everyone else.
if [ -d /etc/techgist ]; then
    chown root:techgist /etc/techgist && chmod 750 /etc/techgist
    for f in /etc/techgist/*; do [ -f "$f" ] && chown root:techgist "$f" && chmod 640 "$f"; done
fi

[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
"$APP/venv/bin/python" -m compileall -q "$APP/worker" "$APP/tools" >/dev/null

# The 1.2 decoder: built once with a Swift toolchain under /opt/swift (no Docker here), stdlib linked in.
# check_edition rebuilds it only when tools/v1decode changes; the service itself has no swiftc on its PATH, so
# after such a change run this script again.
if ! sudo -u techgist env TECHGIST_STATE="$STATE" "$APP/venv/bin/python" - <<'PY'
import sys
sys.path.insert(0, "/opt/techgist")
from tools import check_edition as c
from worker import config
sys.exit(0 if c.decoder_path(config.load()) else 1)
PY
then
    if [ ! -x /opt/swift/usr/bin/swiftc ]; then
        mkdir -p /opt/swift
        curl -fsSL "$SWIFT_URL" -o /opt/swift.tar.gz
        tar -xzf /opt/swift.tar.gz -C /opt/swift --strip-components=1
        rm -f /opt/swift.tar.gz
    fi
    (cd "$APP" && sudo -u techgist env PATH="/opt/swift/usr/bin:$PATH" HOME=/tmp TECHGIST_STATE="$STATE" "$APP/venv/bin/python" - <<'PY'
import sys
sys.path.insert(0, "/opt/techgist")
from tools import check_edition as c
from worker import config
print("v1decode:", c.decoder_path(config.load()))
PY
    )
fi

# Firewall: the SSH rule goes in before ufw is switched on.
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw status | grep -q "Status: active" || ufw --force enable >/dev/null

install -m 644 "$APP"/deploy/techgist-*.service "$APP"/deploy/techgist-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable techgist-worker >/dev/null 2>&1
systemctl enable --now techgist-watchdog.timer techgist-backup.timer >/dev/null 2>&1
echo "setup done; start with: systemctl restart techgist-worker"
