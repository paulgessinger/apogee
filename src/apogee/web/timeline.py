from typing import cast

from flask import Blueprint, render_template, flash, request
from gidgethub.abc import GitHubAPI
import sqlalchemy.sql.functions as func

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
        total=total,
    )


@bp.route("/")
def index():
    return timeline_commits_view(frame=True)


@bp.route("/reload_commits", methods=["POST"])
@with_github
async def reload_commits(gh: GitHubAPI):
    n_fetched = 0

    # get highest order of commit
    latest_commit: model.Commit | None = db.session.execute(
        db.select(model.Commit).order_by(model.Commit.order.desc()).limit(1)
    ).scalar_one_or_none()
    latest_order = latest_commit.order if latest_commit is not None else 0

    fetched_commits: list[Commit] = []

    async for data in gh.getiter(f"/repos/{config.REPOSITORY}/commits"):
        if n_fetched >= config.MAX_COMMITS:
            break

        api_commit = Commit(**data)

        if latest_commit is not None and api_commit.sha == latest_commit.sha:
            break

        n_fetched += 1
        fetched_commits.append(api_commit)

    for index, api_commit in enumerate(reversed(fetched_commits)):
        author: model.GitHubUser | None = None
        committer: model.GitHubUser | None = None

        if api_commit.author is not None:
            author = model.GitHubUser.from_api(api_commit.author)
            db.session.merge(author)

        if api_commit.committer is not None:
            committer = model.GitHubUser.from_api(api_commit.committer)
            db.session.merge(committer)

        commit = model.Commit.from_api(api_commit)
        commit.author = author
        commit.committer = committer
        commit.order = latest_order + index + 1
        db.session.merge(commit)

    db.session.commit()

    flash(f"Fetched {n_fetched} commits", "success")

    return timeline_commits_view(frame=False)
