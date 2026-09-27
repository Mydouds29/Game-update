"""Orchestration : collecte -> classification -> normalisation -> stockage.

Chaque source est traitée isolément : une source en échec est journalisée
dans ``fetch_log`` et reprogrammée avec un backoff, sans bloquer les autres.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..collectors.base import CollectorConfigError, ParseError, SourceConfig
from ..collectors.http import FetchError, HttpClient, RobotsDisallowed
from ..collectors.registry import get_collector
from ..config import Config
from ..db import repository as repo
from ..parsing.classifier import classify, is_ptr
from ..parsing.normalizer import normalize

log = logging.getLogger(__name__)


@dataclass
class SourceOutcome:
    source_key: str
    status: str  # ok | not_modified | error
    new_ids: list[int]
    updated_ids: list[int]
    error: str | None = None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def next_fetch(now: datetime, interval: int, failures: int, max_backoff: int,
               retry_after: int | None = None) -> datetime:
    """Intervalle normal si succès ; backoff exponentiel plafonné sinon."""
    if failures <= 0:
        return now + timedelta(seconds=interval)
    delay = min(max_backoff, max(300, interval) * 2 ** min(failures - 1, 10))
    if retry_after:
        delay = max(delay, min(retry_after, max_backoff))
    return now + timedelta(seconds=delay)


def run_source(conn: sqlite3.Connection, source: repo.SourceRow, http: HttpClient,
               config: Config, *, notify_channel: str | None,
               clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
               ) -> SourceOutcome:
    started = clock()
    t0 = time.monotonic()
    interval = source.fetch_interval or config.default_fetch_interval
    ctx = {"source": source.key, "game": source.game_slug, "type": source.type}

    def elapsed_ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    def fail(message: str, retry_after: int | None = None, permanent: bool = False) -> SourceOutcome:
        failures = source.consecutive_failures + 1
        delay_to = (started + timedelta(seconds=config.max_backoff) if permanent
                    else next_fetch(started, interval, failures, config.max_backoff, retry_after))
        with repo.transaction(conn):
            repo.update_source_state(conn, source.id, success=False, now=_iso(started),
                                     next_fetch_at=_iso(delay_to))
            repo.record_fetch(conn, source_id=source.id, started_at=_iso(started),
                              status="error", duration_ms=elapsed_ms(), error=message)
        log.warning("ingest.source_failed", extra={**ctx, "error": message,
                                                   "failures": failures,
                                                   "next_fetch_at": _iso(delay_to)})
        return SourceOutcome(source.key, "error", [], [], message)

    try:
        collector = get_collector(source.type)
        result = collector.fetch(
            SourceConfig(
                id=source.id, key=source.key, type=source.type, params=source.params,
                etag=source.etag, last_modified=source.last_modified,
                known_keys=repo.known_keys(conn, source.id),
            ),
            http,
        )
    except RobotsDisallowed as exc:
        return fail(str(exc), permanent=True)
    except FetchError as exc:
        return fail(str(exc), retry_after=exc.retry_after)
    except (ParseError, CollectorConfigError, KeyError) as exc:
        return fail(f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - une source ne doit jamais tout arrêter
        log.exception("ingest.unexpected", extra=ctx)
        return fail(f"erreur inattendue {type(exc).__name__}: {exc}")

    now = _iso(clock())
    next_at = _iso(next_fetch(started, interval, 0, config.max_backoff))
    if result.not_modified:
        with repo.transaction(conn):
            repo.update_source_state(conn, source.id, success=True, now=now,
                                     next_fetch_at=next_at, etag=result.etag,
                                     last_modified=result.last_modified)
            repo.record_fetch(conn, source_id=source.id, started_at=_iso(started),
                              status="not_modified", duration_ms=elapsed_ms())
        log.info("ingest.not_modified", extra=ctx)
        return SourceOutcome(source.key, "not_modified", [], [])

    # Première collecte réussie : on constitue l'historique sans notifier.
    baseline = source.last_success_at is None
    new_ids: list[int] = []
    updated_ids: list[int] = []
    with repo.transaction(conn):
        for raw in result.patches:
            if is_ptr(raw.title):
                log.debug("ingest.skipped", extra={**ctx, "title": raw.title, "reason": "PTR"})
                continue
            if not raw.trusted_patch:
                verdict = classify(raw.title, raw.tags, raw.body)
                if not verdict.is_patch:
                    log.debug("ingest.skipped", extra={**ctx, "title": raw.title,
                                                       "reason": verdict.reason})
                    continue
            try:
                patch = normalize(raw)
            except Exception as exc:  # noqa: BLE001 - un patch illisible n'arrête pas la source
                log.warning("ingest.normalize_failed",
                            extra={**ctx, "source_key": raw.source_key, "error": str(exc)})
                continue
            existing = repo.find_patch(conn, source.id, patch.source_key)
            if existing is None:
                if (repo.find_same_content(conn, source.game_id, patch.content_hash)
                        or repo.find_same_title(conn, source.game_id, patch.title,
                                                patch.published_at)):
                    log.info("ingest.duplicate_content",
                             extra={**ctx, "source_key": patch.source_key})
                    continue
                patch_id = repo.insert_patch(conn, game_id=source.game_id,
                                             source_id=source.id, patch=patch, now=now)
                new_ids.append(patch_id)
                if notify_channel and not baseline:
                    repo.queue_notification(conn, patch_id, notify_channel)
                log.info("ingest.new_patch", extra={**ctx, "patch_id": patch_id,
                                                    "title": patch.title,
                                                    "baseline": baseline})
            elif existing["content_hash"] != patch.content_hash:
                repo.update_patch(conn, existing["id"], patch, now)
                updated_ids.append(existing["id"])
                log.info("ingest.updated_patch", extra={**ctx, "patch_id": existing["id"],
                                                        "revision": existing["revision"] + 1})
        repo.update_source_state(conn, source.id, success=True, now=now,
                                 next_fetch_at=next_at, etag=result.etag,
                                 last_modified=result.last_modified)
        repo.record_fetch(conn, source_id=source.id, started_at=_iso(started), status="ok",
                          duration_ms=elapsed_ms(), new_count=len(new_ids),
                          updated_count=len(updated_ids))
    log.info("ingest.source_ok", extra={**ctx, "new": len(new_ids),
                                        "updated": len(updated_ids),
                                        "fetched": len(result.patches)})
    return SourceOutcome(source.key, "ok", new_ids, updated_ids)


def run_due(conn: sqlite3.Connection, http: HttpClient, config: Config, *,
            notify_channel: str | None, force: bool = False,
            only_key: str | None = None) -> list[SourceOutcome]:
    now = _iso(datetime.now(timezone.utc))
    if only_key:
        source = repo.get_source(conn, only_key)
        if source is None:
            raise KeyError(f"source inconnue : {only_key}")
        sources = [source]
    elif force:
        sources = [s for s in repo.list_sources(conn) if s.enabled and s.game_active]
    else:
        sources = repo.sources_due(conn, now)
    outcomes = []
    for source in sources:
        outcomes.append(run_source(conn, source, http, config, notify_channel=notify_channel))
    repo.prune_fetch_log(conn)
    return outcomes
