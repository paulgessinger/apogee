from typing import cast
import math

from flask import Blueprint, render_template, flash, request
from gidgethub.abc import GitHubAPI
import sqlalchemy.sql.functions as func
from apogee.github import fetch_commits

from apogee.web.util import with_github
from apogee.model.db import db
from apogee.model import db as model
from apogee.model.github import Commit
from apogee import config

bp = Blueprint("timeline", __name__, url_prefix="/timeline")


def timeline_commits_view(frame: bool) -> str:
    page = int(request.args.get("page", 1))
    per_page = 20

    commits = (
        db.session.execute(
            db.select(model.Commit)
            .filter(model.Commit.order >= 0)
            .order_by(model.Commit.order.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )

    total: int = cast(
        int,
        db.session.execute(
            db.select(func.count("*")).where(model.Commit.order >= 0)
        ).scalar(),
    )

    return render_template(
        "timeline.html" if frame else "commits.html",
        commits=commits,
        page=page,
        per_page=per_page,
        num_pages=math.ceil(total / per_page),
        total=total,
    )


@bp.route("/")
def index():
    return timeline_commits_view(frame=True)


@bp.route("/reload_commits", methods=["POST"])
@with_github
async def reload_commits(gh: GitHubAPI):
    n_fetched = await fetch_commits(gh)

    flash(f"Fetched {n_fetched} commits", "success")

    return timeline_commits_view(frame=False)
