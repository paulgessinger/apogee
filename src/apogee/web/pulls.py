import math
from typing import Iterable, List, Tuple, cast

from flask import Blueprint, flash, render_template, request, url_for
from gidgethub.abc import GitHubAPI
from sqlalchemy.orm import joinedload
import sqlalchemy.sql.functions as func
from apogee.github import update_pull_request

from apogee.util import gather_limit
from apogee.web.util import with_github
from apogee.cache import memoize
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


def get_open_pulls(page: int, per_page: int) -> Tuple[Iterable[model.PullRequest], int]:
    open_pulls: Iterable[model.PullRequest] = (
        db.session.execute(
            db.select(model.PullRequest)
            .filter_by(state="open")
            .order_by(model.PullRequest.updated_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )

    total: int = cast(
        int,
        db.session.execute(
            db.select(func.count("*")).where(model.PullRequest.state == "open")
        ).scalar(),
    )

    return open_pulls, total


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

    all_commits = [[Commit(**commit) for commit in commits] for commits in all_commits]

    for pr, commits in zip(prs, all_commits):
        update_pull_request(pr, commits)

    flash(f"{len(prs)} pull requests updated", "success")

    db.session.commit()

    return pull_index_view(frame=False)


def pull_index_view(frame: bool) -> str:
    page = int(request.args.get("page", 1))
    per_page = 20
    open_pulls, total = get_open_pulls(page, per_page)

    return render_template(
        "pulls.html" if frame else "pull_list.html",
        pulls=open_pulls,
        page=page,
        per_page=per_page,
        num_pages=math.ceil(total / per_page),
        total=total,
    )


@bp.route("/")
@with_github
async def index(gh: GitHubAPI):
    return pull_index_view(frame=True)


@bp.route("/<int:number>")
@with_github
async def show(gh: GitHubAPI, number: int):
    pull = db.get_or_404(model.PullRequest, number)

    return render_template("single_pull.html", pull=pull)


@bp.route("/<int:number>/patches")
@with_github
async def patches(gh: GitHubAPI, number: int):
    ...
