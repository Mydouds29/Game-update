# Déploiement sur le VPS

Cible : une petite instance Debian 12 / Ubuntu 24.04 (1 vCPU, 1 Go de RAM suffit).
L'application tourne sous un utilisateur système dédié `gameupdate`, sans privilèges :

| Unité systemd | Rôle |
|---|---|
| `game-update-web` | gunicorn sur `127.0.0.1:8000` |
| `game-update-worker` | collecte périodique + notifications (un seul processus) |
| `game-update-backup.timer` | sauvegarde SQLite quotidienne (rotation `GU_BACKUP_KEEP`) |

## 1. Durcissement de base (une fois)

```bash
# Mises à jour de sécurité automatiques
apt update && apt install -y unattended-upgrades python3-venv rsync ufw
dpkg-reconfigure -plow unattended-upgrades

# SSH par clé uniquement (vérifier d'abord que ta clé fonctionne !)
cat >/etc/ssh/sshd_config.d/10-hardening.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
systemctl reload ssh

# Pare-feu : rien d'ouvert au public hormis SSH (et 80/443 seulement pour la variante Caddy)
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw enable
```

## 2. Accès privé via Tailscale (recommandé pour la v1)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
# HTTPS automatique sur le tailnet (certificat *.ts.net), proxy vers gunicorn :
tailscale serve --bg --https=443 http://127.0.0.1:8000
```

L'app n'est alors joignable que depuis tes appareils Tailscale. Mettre
`GU_BASE_URL=https://<machine>.<tailnet>.ts.net` et `GU_TRUSTED_PROXIES=1`.
Une fois SSH accessible via Tailscale, on peut même restreindre SSH :
`ufw delete allow OpenSSH && ufw allow in on tailscale0`.

Variante publique : `deploy/Caddyfile` (HTTPS Let's Encrypt), `ufw allow 80,443/tcp`,
et **obligatoirement** l'authentification Basic (`GU_BASIC_AUTH_*`).

## 3. Installation de l'application

```bash
git clone <dépôt> /tmp/game-update && cd /tmp/game-update
sudo ./deploy/install.sh          # 1er passage : crée /etc/game-update/env puis s'arrête
sudo nano /etc/game-update/env    # GU_SECRET_KEY, GU_BASE_URL, GU_NTFY_TOPIC…
sudo ./deploy/install.sh          # 2e passage : migrations, catalogue, services
```

Mise à jour : `git pull` puis relancer `sudo ./deploy/install.sh`.

## 4. Vérifications après installation

```bash
cd /opt/game-update
sudo -u gameupdate bash -c 'set -a; . /etc/game-update/env; set +a; .venv/bin/flask --app wsgi verify-sources'
sudo -u gameupdate bash -c 'set -a; . /etc/game-update/env; set +a; .venv/bin/flask --app wsgi test-notify'
journalctl -u game-update-worker -f      # logs JSON de la collecte
```

`verify-sources` contrôle que chaque AppID Steam correspond au bon jeu et que
les pages Blizzard répondent (et sont autorisées par robots.txt). Corriger
`app/catalog.toml` si besoin puis `flask --app wsgi sync-catalog`.

## 5. Notifications (ntfy)

Choisir un sujet **long et aléatoire** (`python3 -c "import secrets; print('gu-' + secrets.token_urlsafe(18))"`) :
sur `ntfy.sh`, quiconque connaît le nom du sujet peut le lire. Pour plus de
confidentialité, auto-héberger ntfy sur le VPS derrière Tailscale et utiliser
`GU_NTFY_TOKEN`. Sur le téléphone : appli ntfy → s'abonner au sujet.

## 6. Sauvegardes et restauration

Les sauvegardes sont dans `/var/lib/game-update/backups` (copie à chaud via
l'API backup de SQLite). Les copier aussi hors du VPS (ex. `rsync` depuis un
autre appareil du tailnet). Restauration :

```bash
systemctl stop game-update-web game-update-worker
rm -f /var/lib/game-update/game_update.sqlite3-wal /var/lib/game-update/game_update.sqlite3-shm
cp /var/lib/game-update/backups/game_update-<date>.sqlite3 /var/lib/game-update/game_update.sqlite3
chown gameupdate: /var/lib/game-update/game_update.sqlite3
systemctl start game-update-web game-update-worker
```
