from dataclasses import dataclass
from typing import List, Tuple

from flask import Blueprint, render_template
from gidgethub.abc import GitHubAPI
from apogee.util import gather_limit

from apogee.web.util import with_github
from apogee.cache import cache, memoize
from apogee.model.github import Commit, PullRequest
from apogee import config
from apogee.model.db import db
from apogee.model import db as model

bp = Blueprint("pulls", __name__, url_prefix="/pulls")


@memoize(key="pulls", expire=60 * 5)
async def get_pulls(gh: GitHubAPI) -> List[PullRequest]:
    prs: List[PullRequest] = []
    async for data in gh.getiter(f"/repos/{config.REPOSITORY}/pulls"):
        pr = PullRequest(**data)

        prs.append(pr)

    return prs


class ExtendedPullRequest(PullRequest):
    class Config:
        arbitrary_types_allowed = True

    commit: model.Commit


@bp.route("/")
@with_github
async def index(gh: GitHubAPI):
    prs = await get_pulls(gh)

    missing: List[PullRequest] = []

    for pr in prs:
        commit = db.session.execute(
            db.select(model.Commit).filter_by(sha=pr.head.sha)
        ).scalar_one_or_none()

        if commit is None:
            missing.append(pr)

    print(len(missing))

    missing_commits_data = await gather_limit(
        15, *[gh.getitem(f"{pr.head.repo.url}/commits/{pr.head.sha}") for pr in missing]
    )

    for data in missing_commits_data:
        commit = Commit(**data)

        db_commit = model.Commit.from_api(commit)
        db_commit.order = -1
        db.session.merge(db_commit)

    db.session.commit()

    combined = []
    for pr in prs:
        commit = db.session.execute(
            db.select(model.Commit).filter_by(sha=pr.head.sha)
        ).scalar()

        combined.append(ExtendedPullRequest(commit=commit, **pr.dict()))

    return render_template(
        "pulls.html",
        pulls=combined,
    )
