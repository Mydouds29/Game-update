# Game Update

Service web **mobile-first** qui centralise les patch notes des jeux suivis
(Steam, Blizzard News, sites officiels), avec notification sur téléphone à
chaque nouveau patch. v1 : outil perso hébergé sur un VPS, accès privé.

Stack : Python 3.11+ · Flask · SQLite · Jinja · gunicorn + systemd.

## Fonctionnement

```
collectors/ (rss, steam, blizzard, html_generic)  ->  RawPatch
parsing/classifier.py  patch note ou promo/événement ?
parsing/normalizer.py  BBCode/HTML -> sections -> éléments (fix/new/balance/change/note)
services/ingest.py     déduplication (id source + hash de contenu), mises à jour, fetch_log
services/notify.py     ntfy (interface Notifier, remplaçable par Web Push)
scheduler.py           worker unique, intervalle par source, backoff exponentiel
web/                   fil filtrable, détail avec navigation par sections, gestion des jeux
```

- **Catalogue curaté** : `app/catalog.toml` (jeux, sources, fréquences, vide pour l'instant), appliqué par
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

Autres commandes : `discover-feeds <url>`, `verify-sources`, `record-fixture <source>`, `notify`,
`test-notify`, `backup-db`.

## Déploiement

Voir [`deploy/README.md`](deploy/README.md) : durcissement du VPS, Tailscale,
`install.sh`, unités systemd, sauvegardes.

## Jeux couverts

Aucun jeu n'est configuré : `app/catalog.toml` est vide en attendant que les
jeux et leurs sources soient choisis. Le format d'une entrée est décrit en tête
du fichier ; `tests/fixtures/catalog_test.toml` contient des exemples utilisés
uniquement par les tests.

Choix de la source d'un jeu, dans cet ordre :

1. **Flux RSS/Atom officiel de l'éditeur** : prévu pour être repris, le plus stable.
   Le chercher avec `flask --app wsgi discover-feeds <url du site officiel>`.
2. **Annonces Steam de l'éditeur** : contenu officiel et API autorisée, mais dépendance à Valve.
3. **Page du site officiel** (scraping) : seulement à défaut, après lecture de ses
   conditions d'utilisation.

Avant d'activer une source : `flask --app wsgi verify-sources`, puis
`record-fixture` pour enregistrer une réponse réelle et la tester.

Les parseurs Blizzard et les fixtures de test ont été écrits d'après la structure
connue de ces pages, sans accès réseau à Steam/Blizzard pendant le développement
(voir `tests/fixtures/README.md`).
