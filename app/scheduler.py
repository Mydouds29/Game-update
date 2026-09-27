"""Worker de collecte périodique (processus séparé du serveur web).

Un seul processus planifie : on évite ainsi que chaque worker gunicorn lance
sa propre collecte. La fréquence est propre à chaque source
(``fetch_interval``), avec backoff exponentiel en cas d'échec.
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv

from .cli import make_http
from .config import Config, ConfigError
from .db.models import connect, migrate
from .logging_setup import setup_logging
from .services import ingest
from .services.notify import build_notifier, dispatch_pending

log = logging.getLogger("app.scheduler")

MAX_SLEEP = 60


def seconds_until_next(conn, now: datetime) -> float:
    row = conn.execute(
        "SELECT min(s.next_fetch_at) FROM sources s JOIN games g ON g.id = s.game_id"
        " WHERE s.enabled = 1 AND g.active = 1"
    ).fetchone()
    if not row or row[0] is None:
        return MAX_SLEEP
    delta = (datetime.fromisoformat(row[0]) - now).total_seconds()
    return max(1.0, min(MAX_SLEEP, delta))


def run_forever(config: Config, stop: threading.Event) -> None:
    conn = connect(config.database_path)
    migrate(conn)
    http = make_http(config)
    notifier = build_notifier(config)
    channel = notifier.channel if notifier else None
    log.info("worker.started", extra={"notifier": config.notifier})
    try:
        while not stop.is_set():
            try:
                outcomes = ingest.run_due(conn, http, config, notify_channel=channel)
                if outcomes:
                    log.info("worker.cycle", extra={
                        "sources": len(outcomes),
                        "errors": sum(o.status == "error" for o in outcomes),
                        "new": sum(len(o.new_ids) for o in outcomes),
                    })
                if notifier:
                    dispatch_pending(conn, notifier, config.base_url)
                wait = seconds_until_next(conn, datetime.now(timezone.utc))
            except Exception:  # noqa: BLE001 - le worker ne doit pas mourir sur une erreur
                log.exception("worker.cycle_failed")
                wait = MAX_SLEEP
            stop.wait(wait)
    finally:
        conn.close()
        log.info("worker.stopped")


def main() -> int:
    load_dotenv()
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration invalide : {exc}")
        return 2
    setup_logging(config.log_level)
    stop = threading.Event()

    def _handle(signum, _frame):  # type: ignore[no-untyped-def]
        log.info("worker.signal", extra={"signal": signum})
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    run_forever(config, stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
