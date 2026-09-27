#!/usr/bin/env bash
# Installation / mise à jour sur le VPS (Debian/Ubuntu). À lancer en root
# depuis une copie du dépôt : sudo ./deploy/install.sh
set -euo pipefail

APP_DIR=/opt/game-update
DATA_DIR=/var/lib/game-update
CONF_DIR=/etc/game-update
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

id gameupdate >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin gameupdate
install -d -o gameupdate -g gameupdate -m 0700 "$DATA_DIR" "$DATA_DIR/backups"
install -d -o root -g gameupdate -m 0750 "$CONF_DIR"

if [ "$SRC_DIR" != "$APP_DIR" ]; then
  rsync -a --delete --exclude .git --exclude .venv --exclude .env "$SRC_DIR/" "$APP_DIR/"
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet "$APP_DIR"
chown -R root:root "$APP_DIR"

if [ ! -f "$CONF_DIR/env" ]; then
  install -o root -g gameupdate -m 0640 "$APP_DIR/.env.example" "$CONF_DIR/env"
  echo ">> Éditer $CONF_DIR/env (GU_SECRET_KEY, GU_NTFY_TOPIC, ...) puis relancer ce script."
  exit 0
fi

install -m 0644 "$APP_DIR"/deploy/game-update-{web,worker,backup}.service \
  "$APP_DIR"/deploy/game-update-backup.timer /etc/systemd/system/
systemctl daemon-reload

# Commandes d'administration exécutées sous l'utilisateur de service, avec son environnement.
run() {
  runuser -u gameupdate -- bash -c 'set -a; . /etc/game-update/env; set +a; cd /opt/game-update; exec .venv/bin/flask --app wsgi "$@"' _ "$@"
}
run init-db
run sync-catalog

systemctl enable --now game-update-web game-update-worker game-update-backup.timer
systemctl restart game-update-web game-update-worker
systemctl --no-pager --lines=0 status game-update-web game-update-worker
