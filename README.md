# Game Update

Service web **mobile-first** qui centralise les patch notes des jeux suivis
(Steam, Blizzard News, sites officiels), avec notification sur téléphone à
chaque nouveau patch. v1 : outil perso hébergé sur un VPS, accès privé.

Stack : Python 3.11+ · Flask · SQLite · Jinja · gunicorn + systemd.

## Fonctionnement

```
collectors/ (steam, blizzard, html_generic)   ->  RawPatch
parsing/classifier.py  patch note ou promo/événement ?
parsing/normalizer.py  BBCode/HTML -> sections -> éléments (fix/new/balance/change/note)
services/ingest.py     déduplication (id source + hash de contenu), mises à jour, fetch_log
services/notify.py     ntfy (interface Notifier, remplaçable par Web Push)
scheduler.py           worker unique, intervalle par source, backoff exponentiel
web/                   fil filtrable, détail avec navigation par sections, gestion des jeux
```

- **Catalogue curaté** : `app/catalog.toml` (jeux, sources, fréquences), appliqué par
  `flask --app wsgi sync-catalog`. L'état actif/inactif se gère ensuite dans l'écran « Jeux ».
- **Robustesse** : une source en échec est journalisée dans `fetch_log`, reprogrammée
  avec backoff (respect de `Retry-After`), et n'affecte pas les autres.
- **Scraping responsable** : User-Agent explicite, robots.txt (pages HTML), ETag /
  Last-Modified, une requête par hôte toutes les `GU_HTTP_MIN_INTERVAL` s, timeouts,
  taille de réponse bornée.
- **Dates** : la date de la source est conservée brute (`published_at_raw`) et parsée ;
  la date de détection est stockée à part. Une date annoncée dans le futur est signalée.
- **Première collecte** d'une source : l'historique est importé sans notifier.
- **Sécurité** : CSP stricte (aucun script ni style inline), CSRF sur les formulaires,
  cookies `Secure`/`HttpOnly`/`SameSite`, auth HTTP Basic optionnelle, en-têtes de
  sécurité, aucune redirection ouverte, logs JSON avec identifiant de requête.

## Développement

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env    # puis GU_ENV=development, GU_NOTIFIER=log, GU_DATABASE_PATH=instance/gu.sqlite3
.venv/bin/flask --app wsgi init-db
.venv/bin/flask --app wsgi sync-catalog
.venv/bin/flask --app wsgi collect --force     # collecte immédiate
.venv/bin/flask --app wsgi run --debug          # http://127.0.0.1:5000
.venv/bin/python -m app.scheduler               # worker périodique
.venv/bin/pytest
```

Autres commandes : `verify-sources`, `record-fixture <source>`, `notify`,
`test-notify`, `backup-db`.

## Déploiement

Voir [`deploy/README.md`](deploy/README.md) : durcissement du VPS, Tailscale,
`install.sh`, unités systemd, sauvegardes.

## État des sources (à la livraison)

| Jeu | Source | État |
|---|---|---|
| Diablo IV | Blizzard News, page unique découpée par version | URL de la maquette, **à vérifier** |
| Diablo IV | Steam 2344520 | désactivée (doublon possible) |
| Palworld | Steam 1623730 | AppID vérifié |
| Space Marine 2 | Steam 2183900 | AppID vérifié |
| Total War: Warhammer III | Steam 1142710 | AppID vérifié |
| Diablo II: Resurrected | Blizzard News, liste d'articles | URL de liste **à vérifier** |
| Wuthering Waves | Site officiel (API JSON) | **désactivée**, endpoint à identifier |

Les parseurs Blizzard et les fixtures de test ont été écrits d'après la structure
connue de ces pages, sans accès réseau à Steam/Blizzard pendant le développement.
Lancer `verify-sources` puis `record-fixture` depuis le VPS et ajouter des tests sur
les réponses réelles (voir `tests/fixtures/README.md`).
