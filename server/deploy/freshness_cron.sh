#!/bin/bash
# The outside check that the app's edition is still being published: run from cron on a machine that is NOT the
# VPS (the old private box), so it still works when the whole VPS is down.
#
#   */5 * * * * $HOME/techgist-freshness/freshness_cron.sh
#
# Reads what readers are served (the public URL) with check_freshness.py and, when it is older than the limit
# (20 min; the worker pushes every 2-3 min and GitHub caches for 5), sends an ntfy alert: once when it goes stale,
# a reminder every 30 min while it stays stale, and one message when it is fresh again.
# Beside this script: check_freshness.py, alert.py and `env` (mode 600) holding NTFY_TOPIC=..., the same topic as
# /etc/techgist/env on the VPS. The topic is a secret: it is never printed or logged here.
#   freshness_cron.sh [URL] [LIMIT_MIN]      FRESHNESS_STAMP=<file> keeps a test run's state apart from cron's
set -u
dir=$(cd "$(dirname "$0")" && pwd)
url=${1:-https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json}
limit=${2:-20}
stamp=${FRESHNESS_STAMP:-$dir/alerted}
log=$dir/freshness.log
set -a
[ -f "$dir/env" ] && . "$dir/env"
set +a

out=$(python3 "$dir/check_freshness.py" "$url" --max-age-min "$limit" 2>&1)
status=$?
if [ $status -eq 0 ]; then
    if [ -f "$stamp" ]; then
        python3 "$dir/alert.py" --title "Techgist: published edition is fresh again" "$out (checked from outside the server)" 2>/dev/null
        rm -f "$stamp"
    fi
elif [ ! -f "$stamp" ] || [ -n "$(find "$stamp" -mmin +30 2>/dev/null)" ]; then
    # check_freshness.py --alert words and sends the message; the stamp is set only if ntfy took it.
    if python3 "$dir/check_freshness.py" "$url" --max-age-min "$limit" --alert 2>&1 | grep -q "ntfy accepted"; then
        touch "$stamp"
        out="$out [alert sent]"
    else
        out="$out [alert NOT sent]"
    fi
fi
echo "$(date -u +%FT%TZ) status=$status $out" >> "$log"
tail -n 2000 "$log" > "$log.tmp" 2>/dev/null && mv "$log.tmp" "$log"
exit $status
