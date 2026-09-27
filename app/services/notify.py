"""Notifications de nouveaux patchs, derrière une interface interchangeable
(ntfy pour la v1 perso, Web Push/VAPID envisagé pour la version publique)."""

from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests

from ..config import Config
from ..db import repository as repo

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


class NotifyError(Exception):
    pass


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    click_url: str


class Notifier(ABC):
    channel: str = ""

    @abstractmethod
    def send(self, notification: Notification) -> None:
        """Envoie la notification ou lève NotifyError."""


class NtfyNotifier(Notifier):
    channel = "ntfy"

    def __init__(self, server: str, topic: str, token: str | None = None,
                 timeout: int = 10, session: requests.Session | None = None) -> None:
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token
        self.timeout = timeout
        self.session = session or requests.Session()

    def send(self, notification: Notification) -> None:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Publication JSON : évite les soucis d'encodage des en-têtes HTTP.
        payload = {
            "topic": self.topic,
            "title": notification.title[:250],
            "message": notification.message[:4000],
            "click": notification.click_url,
            "tags": ["video_game"],
        }
        try:
            resp = self.session.post(self.server, json=payload, headers=headers,
                                     timeout=self.timeout)
        except requests.RequestException as exc:
            raise NotifyError(f"ntfy injoignable : {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise NotifyError(f"ntfy a répondu HTTP {resp.status_code}")


class LogNotifier(Notifier):
    channel = "log"

    def send(self, notification: Notification) -> None:
        log.info("notify.log", extra={"title": notification.title,
                                      "click": notification.click_url})


def build_notifier(config: Config) -> Notifier | None:
    if config.notifier == "ntfy":
        return NtfyNotifier(config.ntfy_server, config.ntfy_topic or "", config.ntfy_token,
                            timeout=config.http_timeout)
    if config.notifier == "log":
        return LogNotifier()
    return None


def dispatch_pending(conn: sqlite3.Connection, notifier: Notifier, base_url: str) -> int:
    """Envoie les notifications en attente ; retourne le nombre envoyé."""
    sent = 0
    for row in repo.pending_notifications(conn, notifier.channel, MAX_ATTEMPTS):
        if not row["game_active"]:
            # Jeu désactivé entre-temps : on n'envoie pas, on clôt.
            repo.mark_notification(conn, row["id"], sent=True, error="jeu inactif")
            continue
        notification = Notification(
            title=f"{row['game_name']} — nouveau patch",
            message=row["title"],
            click_url=f"{base_url}/patch/{row['patch_id']}",
        )
        try:
            notifier.send(notification)
        except NotifyError as exc:
            log.warning("notify.failed", extra={"patch_id": row["patch_id"],
                                                "attempt": row["attempts"] + 1,
                                                "error": str(exc)})
            repo.mark_notification(conn, row["id"], sent=False, error=str(exc))
            continue
        repo.mark_notification(conn, row["id"], sent=True)
        sent += 1
        log.info("notify.sent", extra={"patch_id": row["patch_id"],
                                       "channel": notifier.channel})
    return sent
