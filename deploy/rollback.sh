#!/bin/bash
# Put back the code that ran before the last install. Run as: sudo bash /opt/sentinel/deploy/rollback.sh
# No sec-bundle check on purpose (decision 0006): undoing a release must always work.
# Restores code, static files and the nginx files saved by install.sh. Data and the env file are untouched;
# database migrations are not reversed (a test site with no migrations in this release, but check first).
#
# To take the site offline completely instead:
#   sudo systemctl disable --now sentinel sentinel-maintenance.timer
#   sudo rm /etc/nginx/sites-enabled/sentinel.uhambo.co.zw && sudo nginx -t && sudo systemctl reload nginx
set -euo pipefail

NAME=sentinel
DOMAIN=sentinel.uhambo.co.zw
APP=/opt/$NAME
BACKUPS=/var/backups/$NAME

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -f "$BACKUPS/code-previous.tgz" ] || { echo "no previous version saved in $BACKUPS; nothing to roll back to"; exit 1; }

# Keep this script running from a stable copy: it lives inside the folder being replaced.
if [ "${SENTINEL_ROLLBACK_COPY:-}" != 1 ]; then
  cp "$0" /tmp/$NAME-rollback.sh
  SENTINEL_ROLLBACK_COPY=1 exec bash /tmp/$NAME-rollback.sh
fi

echo "=== code ==="
find "$APP" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
tar xzf "$BACKUPS/code-previous.tgz" -C "$APP"
chown -R root:root "$APP"
chmod -R u=rwX,go=rX "$APP"
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
( set -a; . /etc/$NAME/env; set +a; export SENTINEL_LOG_DIR="$(mktemp -d)"; cd "$APP" && .venv/bin/python manage.py collectstatic --noinput -v 0; rm -rf "$SENTINEL_LOG_DIR" )
chmod -R go=rX "$APP/staticfiles"
systemctl restart $NAME.service

echo "=== nginx ==="
[ -f "$BACKUPS/nginx-site.bak" ] && cp "$BACKUPS/nginx-site.bak" /etc/nginx/sites-available/$DOMAIN
[ -f "$BACKUPS/nginx-proxy.bak" ] && cp "$BACKUPS/nginx-proxy.bak" /etc/nginx/snippets/$NAME-proxy.conf
nginx -t && systemctl reload nginx

sleep 2
systemctl --no-pager --lines=0 status $NAME.service | head -5
echo "rolled back"
