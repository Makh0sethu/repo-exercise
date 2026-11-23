#!/bin/bash
# Install or update Sentinel (public-only test site) on the VPS. Normally run by the release script:
#   sudo bash /tmp/sentinel-release.sh      (which checks the sec-bundle pass, then calls this)
# Expects the code unpacked at /tmp/sentinel. Safe to re-run: the env file and the data are kept;
# code, units and nginx files are replaced. The previous code is saved for deploy/rollback.sh.
set -euo pipefail

NAME=sentinel
DOMAIN=sentinel.uhambo.co.zw
PORT=8101
SRC=/tmp/sentinel
APP=/opt/$NAME
DATA=/var/lib/$NAME
BACKUPS=/var/backups/$NAME
ETC=/etc/$NAME
SITE=/etc/nginx/sites-available/$DOMAIN

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -f "$SRC/manage.py" ] || { echo "code not found at $SRC"; exit 1; }

echo "=== packages ==="
apt-get install -y -qq python3-venv >/dev/null

echo "=== user and directories ==="
id $NAME >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin $NAME
install -d -o $NAME -g $NAME -m 700 "$DATA" "$DATA/cache" "$DATA/logs" "$DATA/evidence" "$DATA/osv-data" "$BACKUPS"
install -d -o root -g $NAME -m 750 "$ETC"

echo "=== code (previous version saved for rollback) ==="
install -d -o root -g root -m 755 "$APP"
if [ -f "$APP/manage.py" ]; then
  tar czf "$BACKUPS/code-previous.tgz" -C "$APP" --exclude=./.venv --exclude=./staticfiles .
  chown $NAME:$NAME "$BACKUPS/code-previous.tgz"; chmod 600 "$BACKUPS/code-previous.tgz"
fi
find "$APP" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
cp -r "$SRC"/. "$APP"/
rm -rf "$APP/.git" "$APP/.claude"
chown -R root:root "$APP"
chmod -R u=rwX,go=rX "$APP"

[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "=== env file ==="
if [ ! -f "$ETC/env" ]; then
  gen() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
  sed -e "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$(gen 50)|" \
      -e "s|^SENTINEL_ADMIN_PATH=.*|SENTINEL_ADMIN_PATH=manage-$(python3 -c 'import secrets; print(secrets.token_hex(6))')|" \
      "$APP/deploy/env.example" > "$ETC/env"
  echo "created $ETC/env"
fi
chown root:$NAME "$ETC/env"
chmod 640 "$ETC/env"

manage() { sudo -u $NAME bash -c "set -a; . $ETC/env; set +a; cd $APP && .venv/bin/python manage.py $*"; }

echo "=== migrate and static files ==="
manage migrate --noinput
# collectstatic writes into /opt, which belongs to root. Point its log folder elsewhere so root never
# creates a log file the app user then can't write to.
( set -a; . "$ETC/env"; set +a; export SENTINEL_LOG_DIR="$(mktemp -d)"; cd "$APP" && .venv/bin/python manage.py collectstatic --noinput -v 0; rm -rf "$SENTINEL_LOG_DIR" )
chmod -R go=rX "$APP/staticfiles"
manage check --deploy --fail-level ERROR

echo "=== systemd ==="
cp "$APP"/deploy/$NAME.service "$APP"/deploy/$NAME-maintenance.service "$APP"/deploy/$NAME-maintenance.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now $NAME.service $NAME-maintenance.timer
systemctl restart $NAME.service

echo "=== local health check ==="
HOSTHDR=$(grep '^DJANGO_ALLOWED_HOSTS=' "$ETC/env" | cut -d= -f2 | cut -d, -f1)
ok=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS -H "Host: $HOSTHDR" -H "X-Forwarded-Proto: https" "http://127.0.0.1:$PORT/health/"; then ok=1; echo; break; fi
  sleep 1
done
[ -n "$ok" ] || { echo "the app did not answer on 127.0.0.1:$PORT. See: journalctl -u $NAME -n 50"; exit 1; }

echo "=== nginx: shared pieces ==="
# Backups of the main site / Sentinel vhost are only valid for this run: never restore an older copy
# over changes made since (the main site's config belongs to more than this app).
rm -f "$BACKUPS/main-site.bak" "$BACKUPS/nginx-site.bak"
[ -f /etc/nginx/conf.d/cloudflare-realip.conf ] || echo "WARNING: /etc/nginx/conf.d/cloudflare-realip.conf is missing, so visitor IPs will be Cloudflare's"
for f in /etc/nginx/conf.d/$NAME-zones.conf /etc/nginx/snippets/$NAME-proxy.conf /etc/nginx/snippets/$NAME-path.conf; do
  [ -f "$f" ] && cp "$f" "$BACKUPS/$(basename "$f").bak"
done
install -m 644 "$APP/deploy/$NAME-zones.nginx.conf" /etc/nginx/conf.d/$NAME-zones.conf
install -m 644 "$APP/deploy/$NAME-proxy.nginx.conf" /etc/nginx/snippets/$NAME-proxy.conf
install -m 644 "$APP/deploy/$NAME-path.nginx.conf" /etc/nginx/snippets/$NAME-path.conf

restore_nginx() {
  echo "nginx -t failed; restoring the previous config"
  for f in /etc/nginx/conf.d/$NAME-zones.conf /etc/nginx/snippets/$NAME-proxy.conf /etc/nginx/snippets/$NAME-path.conf; do
    if [ -f "$BACKUPS/$(basename "$f").bak" ]; then cp "$BACKUPS/$(basename "$f").bak" "$f"; else rm -f "$f"; fi
  done
  [ -f "$BACKUPS/main-site.bak" ] && cp "$BACKUPS/main-site.bak" "$MAIN_SITE"
  if [ -f "$BACKUPS/nginx-site.bak" ]; then cp "$BACKUPS/nginx-site.bak" "$SITE"; elif [ -n "${NEW_SITE:-}" ]; then rm -f "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"; fi
  nginx -t && systemctl reload nginx
  exit 1
}

# Mode comes from the env file: SENTINEL_URL_PREFIX=/sentinel serves it at uhambo.co.zw/sentinel/
# (no DNS needed); empty serves it on its own subdomain.
PREFIX=$(grep '^SENTINEL_URL_PREFIX=' "$ETC/env" | cut -d= -f2 | tr -d '[:space:]')
MAIN_SITE=/etc/nginx/sites-available/uhambo.co.zw
INCLUDE_LINE="include snippets/$NAME-path.conf;"

if [ -n "$PREFIX" ]; then
  [ "$PREFIX" = "/sentinel" ] || { echo "nginx snippet is written for /sentinel, but the env file says $PREFIX"; exit 1; }
  echo "=== nginx: path mode, https://uhambo.co.zw$PREFIX/ ==="
  if ! grep -qF "$INCLUDE_LINE" "$MAIN_SITE"; then
    cp "$MAIN_SITE" "$BACKUPS/main-site.bak"
    cp "$MAIN_SITE" "$MAIN_SITE.bak-$(date +%F-%H%M%S)"
    # Insert the include before the first "location / {" in the 443 server block (as uhambo-backend does).
    python3 - "$MAIN_SITE" "$INCLUDE_LINE" <<'PY'
import re, sys
path, line = sys.argv[1], sys.argv[2]
text = open(path).read()
m443 = re.search(r"listen\s+(\[::\]:)?443", text)
if not m443:
    sys.exit("no 443 server block found")
loc = re.search(r"^(\s*)location\s+/\s*\{", text[m443.end():], re.M)
if not loc:
    sys.exit("no 'location / {' after the 443 listen line")
i = m443.end() + loc.start()
indent = loc.group(1).lstrip("\n")
text = text[:i] + f"\n{indent}{line}\n" + text[i:]
open(path, "w").write(text)
PY
  fi
  if nginx -t; then systemctl reload nginx; else restore_nginx; fi
  echo "=== checks ==="
  systemctl --no-pager --lines=0 status $NAME.service | head -5
  ss -tlnp | grep ":$PORT " || true
  systemctl list-timers --no-pager "$NAME-*"
  echo "done: https://uhambo.co.zw$PREFIX/"
  exit 0
fi

echo "=== nginx: subdomain mode, https://$DOMAIN/ ==="
if grep -qF "$INCLUDE_LINE" "$MAIN_SITE"; then
  # Moving off the temporary path: take the include out of the main site.
  cp "$MAIN_SITE" "$BACKUPS/main-site.bak"
  cp "$MAIN_SITE" "$MAIN_SITE.bak-$(date +%F-%H%M%S)"
  grep -vF "$INCLUDE_LINE" "$BACKUPS/main-site.bak" > "$MAIN_SITE"
fi

echo "=== TLS certificate ==="
if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
  # certonly: bare --nginx would write into sites-enabled/default (Sprint 5 lesson).
  certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos --keep-until-expiring \
    || { echo "certbot failed. Check the DNS record for $DOMAIN (proxied A record), then re-run. The app is installed; nginx is unchanged."; exit 1; }
fi

echo "=== nginx: site ==="
if [ -f "$SITE" ]; then cp "$SITE" "$BACKUPS/nginx-site.bak"; else NEW_SITE=1; fi
install -m 644 "$APP/deploy/$DOMAIN.nginx.conf" "$SITE"
ln -sf "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
if nginx -t; then systemctl reload nginx; else restore_nginx; fi

echo "=== checks ==="
systemctl --no-pager --lines=0 status $NAME.service | head -5
ss -tlnp | grep ":$PORT " || true
systemctl list-timers --no-pager "$NAME-*"
echo "done: https://$DOMAIN/"
