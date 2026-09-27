"""Filtres Jinja : dates en français, pluriels."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, current_app

MONTHS_SHORT = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
                "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(current_app.config["GU"].timezone))


def fr_date(value: str | None, with_year: bool = False) -> str:
    dt = _parse(value)
    if dt is None:
        return ""
    now = datetime.now(dt.tzinfo)
    text = f"{dt.day} {MONTHS_SHORT[dt.month - 1]}"
    if with_year or dt.year != now.year:
        text += f" {dt.year}"
    return text


def fr_datetime(value: str | None) -> str:
    dt = _parse(value)
    if dt is None:
        return "—"
    return f"{fr_date(value, with_year=True)} à {dt:%H:%M}"


def patch_date(patch: dict) -> str:
    """Date de la source si connue, sinon date de détection."""
    return fr_date(patch.get("published_at") or patch.get("created_at"))


def announced_ahead(patch: dict) -> bool:
    """La source annonce une date nettement postérieure à la détection."""
    published = _parse(patch.get("published_at"))
    detected = _parse(patch.get("created_at"))
    return bool(published and detected and published - detected > timedelta(hours=36))


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    return f"{n} {singular if n <= 1 else (plural_form or singular + 's')}"


def register(app: Flask) -> None:
    app.jinja_env.filters.update(fr_date=fr_date, fr_datetime=fr_datetime,
                                 patch_date=patch_date, plural=plural)
    app.jinja_env.tests["announced_ahead"] = announced_ahead
