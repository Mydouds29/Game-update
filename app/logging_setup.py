"""Logging structuré (une ligne JSON par événement, lisible par journald)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# Attributs standard d'un LogRecord : tout le reste vient de `extra=` et
# est sérialisé tel quel dans la ligne JSON.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    # Idempotent : la factory peut être appelée plusieurs fois (tests, CLI).
    for handler in list(root.handlers):
        if getattr(handler, "_game_update", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._game_update = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    # urllib3 est bavard en DEBUG et peut journaliser des URLs avec jetons.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
