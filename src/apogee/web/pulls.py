from dataclasses import dataclass
from typing import List, Tuple

from flask import Blueprint, flash, render_template
from gidgethub.abc import GitHubAPI
from sqlalchemy.orm import joinedload

from apogee.util import gather_limit
from apogee.web.util import with_github
from apogee.web import is_htmx
from apogee.cache import cache, memoize
from apogee.model.github import Commit, PullRequest
from apogee import config
from apogee.model.db import PrCommitAssociation, db
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


@bp.route("/reload_pulls", methods=["POST"])
@with_github
async def reload_pulls(gh: GitHubAPI):
    prs = await get_pulls(gh)

    # fetch PRs that are not in `pr` because they're not currently open, but we
    # still have them locally as open, and update them as well
    remote_current_open = [pr.number for pr in prs]

    local_current_open = db.session.execute(
        db.select(model.PullRequest).where(
            model.PullRequest.state == "open",
            model.PullRequest.number.not_in(remote_current_open),
        )
    ).scalars()

    prs += [
        PullRequest(**pr)
        for pr in await gather_limit(
            15,
            *[
                gh.getitem(f"/repos/{config.REPOSITORY}/pulls/{pr.number}")
                for pr in local_current_open
            ],
        )
    ]

    all_commits = await gather_limit(
        15,
        *[
            gh.getitem(f"/repos/{config.REPOSITORY}/pulls/{pr.number}/commits")
            for pr in prs
        ],
    )

    updated = set()

    for pr, commits in zip(prs, all_commits):
        for user in (pr.user, pr.head.user, pr.base.user):
            db.session.merge(model.GitHubUser.from_api(user))

        updated.add(pr.number)

        db_pr = model.PullRequest.from_api(pr)
        db.session.merge(db_pr)

        db.session.execute(
            db.delete(PrCommitAssociation).filter_by(pull_request_number=db_pr.number)
        )
        db_pr.commits.clear()

        for i, commit_data in enumerate(commits):
            commit = Commit(**commit_data)

            db_commit = model.Commit.from_api(commit)
            db_commit.order = -1
            db.session.merge(db_commit)

            assoc = PrCommitAssociation(
                pull_request_number=db_pr.number, commit_sha=commit.sha, order=i
            )
            db.session.add(assoc)

            db_pr.commits.append(assoc)

    flash(f"{len(updated)} pull requests updated", "success")

    db.session.commit()

    open_pulls = db.session.execute(
        db.select(model.PullRequest).filter_by(state="open")
    ).scalars()

    return render_template(
        "pull_list.html",
        pulls=open_pulls,
    )


@bp.route("/")
@with_github
async def index(gh: GitHubAPI):
    open_pulls = db.session.execute(
        db.select(model.PullRequest).filter_by(state="open")
    ).scalars()

    return render_template(
        "pulls.html" if not is_htmx else "pull_list.html",
        pulls=open_pulls,
    )
