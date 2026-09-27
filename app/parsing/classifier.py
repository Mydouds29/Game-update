"""Distingue une patch note / un hotfix d'une annonce promo, d'un événement
ou d'une vente."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Signaux forts : suffisent à eux seuls.
_STRONG_POSITIVE = re.compile(
    r"\b(patch\s*notes?|hotfix(es)?|patch\s+v?\d|v?\d+\.\d+(\.\d+)*\s+patch|bug\s*fix(es)?)\b",
    re.I,
)
# Signaux moyens : comptent, mais peuvent être contredits.
_POSITIVE = re.compile(
    r"\b(patch|update|changelog|fix(es|ed)?|build|maintenance|balance\s+changes?)\b", re.I
)
_VERSION = re.compile(r"(?<![\w.])v?\d+\.\d+(\.\d+){0,2}(?![\w.])")
_NEGATIVE = re.compile(
    r"(\b(sale|discount|deals?|bundle|free\s+weekend|giveaway|contest|sweepstakes?|"
    r"livestream|stream|twitch\s+drops?|trailer|merch(andise)?|pre-?order|wishlist|"
    r"out\s+now|now\s+available|launch(es|ed)?|announc(e|es|ed|ement)|showcase|"
    r"dev\s*(diary|blog)|developer\s+update|roadmap|community\s+(update|spotlight)|"
    r"event|festival|anniversary|cosplay|fan\s*art|survey|recap|interview|q\s*&\s*a|"
    r"season\s+pass|battle\s+pass|shop|store)\b|\d+\s*%\s*off)",
    re.I,
)

PATCH_TAGS = frozenset({"patchnotes", "patch_notes", "patchnote"})


@dataclass(frozen=True)
class Classification:
    is_patch: bool
    score: int
    reason: str


def classify(title: str, tags: list[str] | None = None, body: str = "") -> Classification:
    """Score simple et explicable : >= 2 => patch note."""
    normalized_tags = {t.lower().replace(" ", "_") for t in tags or []}
    if normalized_tags & PATCH_TAGS:
        return Classification(True, 10, "tag patchnotes")

    title = title or ""
    score = 0
    reasons = []
    if _STRONG_POSITIVE.search(title):
        score += 3
        reasons.append("mot-clé fort")
    elif _POSITIVE.search(title):
        score += 1
        reasons.append("mot-clé")
    if _VERSION.search(title):
        score += 1
        reasons.append("numéro de version")
    if _NEGATIVE.search(title):
        score -= 2
        reasons.append("mot-clé promo/événement")
    # Corps court sans liste : rarement une patch note.
    if body and score == 1 and re.search(r"(\[\*\]|<li\b)", body, re.I) and re.search(
        r"\bfix(ed|es)?\b", body, re.I
    ):
        score += 1
        reasons.append("liste de correctifs dans le corps")
    return Classification(score >= 2, score, ", ".join(reasons) or "aucun signal")


def looks_like_patch_title(title: str) -> bool:
    """Pré-filtre sur le seul titre, avant de télécharger un article."""
    return classify(title).is_patch
