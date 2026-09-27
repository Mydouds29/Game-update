"""Configuration chargée depuis les variables d'environnement (préfixe GU_)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """Configuration invalide ou incomplète."""


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _get_int(name: str, default: int, minimum: int = 0) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un entier (reçu {raw!r})") from exc
    if value < minimum:
        raise ConfigError(f"{name} doit être >= {minimum} (reçu {value})")
    return value


@dataclass(frozen=True)
class Config:
    env: str = "production"
    secret_key: str = ""
    database_path: Path = Path("instance/game_update.sqlite3")
    backup_dir: Path = Path("instance/backups")
    backup_keep: int = 14
    log_level: str = "INFO"
    base_url: str = "http://localhost:5000"
    timezone: str = "Europe/Paris"

    basic_auth_user: str | None = None
    basic_auth_password_hash: str | None = None
    trusted_proxies: int = 1

    user_agent: str = "GameUpdateBot/0.1"
    http_timeout: int = 20
    http_min_interval: int = 5
    http_max_bytes: int = 5 * 1024 * 1024
    default_fetch_interval: int = 3600
    max_backoff: int = 86400

    notifier: str = "log"
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str | None = None
    ntfy_token: str | None = None

    testing: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @classmethod
    def from_env(cls) -> "Config":
        env = (_get("GU_ENV", "production") or "production").lower()
        if env not in {"development", "production"}:
            raise ConfigError("GU_ENV doit valoir 'development' ou 'production'")

        secret_key = _get("GU_SECRET_KEY")
        if not secret_key:
            if env == "production":
                raise ConfigError("GU_SECRET_KEY est obligatoire en production")
            secret_key = "dev-only-insecure-key"
        elif env == "production" and len(secret_key) < 32:
            raise ConfigError("GU_SECRET_KEY doit faire au moins 32 caractères")

        user = _get("GU_BASIC_AUTH_USER")
        pw_hash = _get("GU_BASIC_AUTH_PASSWORD_HASH")
        if bool(user) != bool(pw_hash):
            raise ConfigError(
                "GU_BASIC_AUTH_USER et GU_BASIC_AUTH_PASSWORD_HASH vont ensemble"
            )

        notifier = (_get("GU_NOTIFIER", "log") or "log").lower()
        if notifier not in {"ntfy", "log", "none"}:
            raise ConfigError("GU_NOTIFIER doit valoir ntfy, log ou none")
        ntfy_topic = _get("GU_NTFY_TOPIC")
        if notifier == "ntfy" and not ntfy_topic:
            raise ConfigError("GU_NTFY_TOPIC est obligatoire avec GU_NOTIFIER=ntfy")

        log_level = (_get("GU_LOG_LEVEL", "INFO") or "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("GU_LOG_LEVEL invalide")

        tz = _get("GU_TIMEZONE", "Europe/Paris") or "Europe/Paris"
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"GU_TIMEZONE inconnu : {tz}") from exc

        return cls(
            env=env,
            timezone=tz,
            secret_key=secret_key,
            database_path=Path(
                _get("GU_DATABASE_PATH", "instance/game_update.sqlite3")
            ),
            backup_dir=Path(_get("GU_BACKUP_DIR", "instance/backups")),
            backup_keep=_get_int("GU_BACKUP_KEEP", 14, minimum=1),
            log_level=log_level,
            base_url=(_get("GU_BASE_URL", "http://localhost:5000") or "").rstrip("/"),
            basic_auth_user=user,
            basic_auth_password_hash=pw_hash,
            trusted_proxies=_get_int("GU_TRUSTED_PROXIES", 1),
            user_agent=_get("GU_USER_AGENT", "GameUpdateBot/0.1")
            or "GameUpdateBot/0.1",
            http_timeout=_get_int("GU_HTTP_TIMEOUT", 20, minimum=1),
            http_min_interval=_get_int("GU_HTTP_MIN_INTERVAL", 5),
            default_fetch_interval=_get_int(
                "GU_DEFAULT_FETCH_INTERVAL", 3600, minimum=300
            ),
            max_backoff=_get_int("GU_MAX_BACKOFF", 86400, minimum=600),
            notifier=notifier,
            ntfy_server=(_get("GU_NTFY_SERVER", "https://ntfy.sh") or "").rstrip("/"),
            ntfy_topic=ntfy_topic,
            ntfy_token=_get("GU_NTFY_TOKEN"),
        )
