"""Commandes d'administration : `flask --app wsgi <commande>`."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import click
from flask import Flask, current_app

from .catalog import CatalogError, load_catalog, sync_catalog
from .collectors.base import ParseError
from .collectors.http import FetchError, HttpClient
from .config import Config
from .db import repository as repo
from .db.models import connect, migrate
from .services import ingest
from .services.notify import build_notifier, dispatch_pending


def make_http(cfg: Config) -> HttpClient:
    return HttpClient(user_agent=cfg.user_agent, timeout=cfg.http_timeout,
                      min_interval=cfg.http_min_interval, max_bytes=cfg.http_max_bytes)


def _conn() -> sqlite3.Connection:
    return connect(current_app.config["GU"].database_path)


def backup_database(cfg: Config) -> Path:
    """Sauvegarde cohérente à chaud (API backup de SQLite) + rotation."""
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = cfg.backup_dir / f"game_update-{stamp}.sqlite3"
    src = connect(cfg.database_path)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    target.chmod(0o600)
    backups = sorted(cfg.backup_dir.glob("game_update-*.sqlite3"))
    for old in backups[:-cfg.backup_keep]:
        old.unlink()
    return target


def register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db() -> None:
        """Crée ou met à jour le schéma de la base."""
        conn = _conn()
        try:
            applied = migrate(conn)
        finally:
            conn.close()
        click.echo(f"Migrations appliquées : {', '.join(applied) or 'aucune'}")

    @app.cli.command("sync-catalog")
    @click.option("--file", "path", type=click.Path(exists=True, path_type=Path),
                  help="Catalogue alternatif (défaut : app/catalog.toml).")
    def sync_catalog_cmd(path: Path | None) -> None:
        """Applique le catalogue curaté des jeux et sources."""
        try:
            data = load_catalog(path)
        except CatalogError as exc:
            raise click.ClickException(str(exc)) from exc
        conn = _conn()
        try:
            stats = sync_catalog(conn, data)
        finally:
            conn.close()
        click.echo(json.dumps(stats))

    @app.cli.command("collect")
    @click.option("--source", "source_key", help="Clé d'une source précise.")
    @click.option("--force", is_flag=True, help="Ignore la planification.")
    def collect(source_key: str | None, force: bool) -> None:
        """Lance une collecte (sources dues, ou toutes avec --force)."""
        cfg = current_app.config["GU"]
        notifier = build_notifier(cfg)
        conn = _conn()
        try:
            try:
                outcomes = ingest.run_due(
                    conn, make_http(cfg), cfg, force=force, only_key=source_key,
                    notify_channel=notifier.channel if notifier else None,
                )
            except KeyError as exc:
                raise click.ClickException(str(exc)) from exc
            sent = dispatch_pending(conn, notifier, cfg.base_url) if notifier else 0
        finally:
            conn.close()
        for o in outcomes:
            line = f"{o.source_key:40} {o.status:13} +{len(o.new_ids)} ~{len(o.updated_ids)}"
            click.echo(line + (f"  {o.error}" if o.error else ""))
        click.echo(f"Notifications envoyées : {sent}")

    @app.cli.command("verify-sources")
    def verify_sources() -> None:
        """Vérifie les AppID Steam et l'accessibilité des pages du catalogue."""
        from .collectors.steam import fetch_app_name
        cfg = current_app.config["GU"]
        http = make_http(cfg)
        conn = _conn()
        try:
            sources = repo.list_sources(conn)
        finally:
            conn.close()
        problems = 0
        for s in sources:
            try:
                if s.type == "steam":
                    name = fetch_app_name(http, s.params["appid"])
                    ok = bool(name)
                    detail = f"AppID {s.params['appid']} = {name!r} (attendu : {s.game_name})"
                else:
                    url = s.params.get("url") or s.params.get("list_url")
                    allowed = http.allowed_by_robots(url)
                    resp = http.get(url, check_robots=False) if allowed else None
                    ok = allowed and resp is not None
                    detail = (f"{url} -> HTTP {resp.status}, {len(resp.text)} car."
                              if resp else f"{url} -> interdit par robots.txt")
            except (FetchError, ParseError, KeyError) as exc:
                ok, detail = False, str(exc)
            problems += not ok
            state = "OK " if ok else "KO "
            enabled = "" if s.enabled else " [désactivée]"
            click.echo(f"{state} {s.key}{enabled}: {detail}")
        if problems:
            raise click.ClickException(f"{problems} source(s) à corriger")

    @app.cli.command("record-fixture")
    @click.argument("source_key")
    @click.option("--out", type=click.Path(path_type=Path), default=Path("tests/fixtures"))
    def record_fixture(source_key: str, out: Path) -> None:
        """Enregistre la réponse brute d'une source pour les tests."""
        from .collectors.steam import NEWS_URL, OFFICIAL_FEED
        cfg = current_app.config["GU"]
        conn = _conn()
        try:
            s = repo.get_source(conn, source_key)
        finally:
            conn.close()
        if s is None:
            raise click.ClickException(f"source inconnue : {source_key}")
        http = make_http(cfg)
        if s.type == "steam":
            resp = http.get(NEWS_URL, check_robots=False, params={
                "appid": s.params["appid"], "count": 20, "maxlength": 0,
                "feeds": OFFICIAL_FEED, "format": "json"})
            ext = "json"
        else:
            resp = http.get(s.params.get("url") or s.params["list_url"])
            ext = "json" if s.params.get("mode") == "json_list" else "html"
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{source_key}.recorded.{ext}"
        target.write_text(resp.text, encoding="utf-8")
        click.echo(f"Écrit : {target} ({len(resp.text)} caractères)")

    @app.cli.command("notify")
    def notify_cmd() -> None:
        """Renvoie les notifications en attente."""
        cfg = current_app.config["GU"]
        notifier = build_notifier(cfg)
        if notifier is None:
            raise click.ClickException("GU_NOTIFIER=none")
        conn = _conn()
        try:
            click.echo(f"Envoyées : {dispatch_pending(conn, notifier, cfg.base_url)}")
        finally:
            conn.close()

    @app.cli.command("test-notify")
    def test_notify() -> None:
        """Envoie une notification de test pour valider la configuration."""
        from .services.notify import Notification, NotifyError
        cfg = current_app.config["GU"]
        notifier = build_notifier(cfg)
        if notifier is None:
            raise click.ClickException("GU_NOTIFIER=none")
        try:
            notifier.send(Notification("Game Update", "Notification de test", cfg.base_url))
        except NotifyError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo("Notification envoyée.")

    @app.cli.command("backup-db")
    def backup_db() -> None:
        """Sauvegarde la base SQLite et applique la rotation."""
        click.echo(f"Sauvegarde : {backup_database(current_app.config['GU'])}")
