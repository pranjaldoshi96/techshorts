#!/bin/bash
# Run on the VPS as root. Gives the push registry a public HTTPS name with a Let's Encrypt certificate:
#   https://$NAME/v1/...  ->  http://127.0.0.1:8787/v1/...
# It ADDS one nginx file (/etc/nginx/sites-available/techgist-push, linked into sites-enabled) and touches no
# other: the unrelated site on this machine keeps its own server blocks and stays the default for port 443.
# Every nginx change is checked with `nginx -t` first; if the check fails the file is removed again and nothing
# is reloaded. Safe to run again.
set -euo pipefail
NAME=${TECHGIST_PUSH_HOST:-techgist.213-210-36-218.sslip.io}
PORT=${TECHGIST_PUSH_PORT:-8787}
SITE=/etc/nginx/sites-available/techgist-push
LINK=/etc/nginx/sites-enabled/techgist-push
WEBROOT=/var/www/techgist-acme
LIVE=/etc/letsencrypt/live/$NAME

http_block() {
cat <<EOF
# Techgist push registry (server/push/deploy/setup_https.sh). Not part of the other site on this machine.
server {
    listen 80;
    server_name $NAME;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 404; }
}
EOF
}

https_block() {
cat <<EOF
server {
    listen 443 ssl;
    server_name $NAME;
    ssl_certificate $LIVE/fullchain.pem;
    ssl_certificate_key $LIVE/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 4k;
    access_log off;
    # Only the registry's API is reachable; the path is passed on unchanged.
    location /v1/ {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }
    location / { return 404; }
}
EOF
}

apply() {    # writes the file from stdin, checks, reloads; on a failed check removes it and stops
    cat > "$SITE"
    ln -sfn "$SITE" "$LINK"
    if ! nginx -t 2>/tmp/techgist-nginx-t.log; then
        cat /tmp/techgist-nginx-t.log >&2
        rm -f "$LINK" "$SITE"
        echo "nginx -t failed with our file; it was removed and nginx was not reloaded" >&2
        exit 1
    fi
    systemctl reload nginx
}

mkdir -p "$WEBROOT"
if [ ! -s "$LIVE/fullchain.pem" ]; then
    http_block | apply
    certbot certonly --webroot -w "$WEBROOT" -d "$NAME" --non-interactive --keep-until-expiring \
        --deploy-hook "nginx -t && systemctl reload nginx"
fi
{ http_block; https_block; } | apply
echo "https://$NAME is configured; certificate renews through certbot.timer"
