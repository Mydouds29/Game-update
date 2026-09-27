"""Page de détail d'un patch."""

from __future__ import annotations

from flask import Blueprint, abort, render_template

from .. import get_db
from ..db import repository as repo

bp = Blueprint("patch", __name__)


@bp.get("/patch/<int:patch_id>")
def detail(patch_id: int) -> str:
    patch = repo.get_patch(get_db(), patch_id)
    if patch is None:
        abort(404, description="Ce patch n'existe pas.")
    return render_template("patch.html", patch=patch)
