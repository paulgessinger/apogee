import math
from typing import Iterable, List, Tuple, cast

from flask import Blueprint, flash, render_template, request, url_for
from gidgethub.abc import GitHubAPI
from sqlalchemy.orm import joinedload, raiseload
import sqlalchemy.sql.functions as func
from apogee.github import update_pull_request

from apogee.util import gather_limit
from apogee.web.util import with_github
from apogee.cache import memoize
from apogee.model.github import Commit, CompareResponse, PullRequest
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


def get_open_pulls(
    page: int, per_page: int
) -> Tuple[Iterable[model.PullRequest], dict[str, model.Pipeline], int]:
    select = (
        db.select(model.PullRequest)
        .filter_by(state="open")
        .order_by(model.PullRequest.updated_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .options(
            joinedload(model.PullRequest.user),
            joinedload(model.PullRequest.patches),
            joinedload(model.PullRequest.commits).joinedload(
                model.PrCommitAssociation.commit
            ),
            raiseload("*"),
        )
    )

    open_pulls: Iterable[model.PullRequest] = (
        db.session.execute(select).unique().scalars().all()
    )

    total: int = cast(
        int,
        db.session.execute(
            db.select(func.count("*")).where(model.PullRequest.state == "open")
        ).scalar(),
    )

    pipeline_select = (
        db.select(model.Commit.sha, model.Pipeline, func.max(model.Pipeline.created_at))
        .where(
            model.PrCommitAssociation.pull_request_number.in_(
                [p.number for p in open_pulls]
            )
        )
        .join(model.PrCommitAssociation.commit)
        .join(model.Commit.pipelines)
        .group_by(model.Commit.sha)
    )

    pipelines = db.session.execute(pipeline_select).all()

    pipeline_by_commit = {sha: pipeline for sha, pipeline, _ in pipelines}
    return open_pulls, pipeline_by_commit, total


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

    all_compare = await gather_limit(
        15,
        *[
            gh.getitem(
                f"/repos/{config.REPOSITORY}/compare/{pr.base.sha}...{pr.head.sha}"
            )
            for pr in prs
        ],
    )

    all_commits = [CompareResponse(**compare).commits for compare in all_compare]

    for pr, commits in zip(prs, all_commits):
        update_pull_request(pr, commits)

    flash(f"{len(prs)} pull requests updated", "success")

    db.session.commit()

    return pull_index_view(frame=False)


def pull_index_view(frame: bool) -> str:
    page = int(request.args.get("page", 1))
    per_page = 20
    open_pulls, pipeline_by_commit, total = get_open_pulls(page, per_page)

    return render_template(
        "pulls.html" if frame else "pull_list.html",
        pulls=open_pulls,
        pipeline_by_commit=pipeline_by_commit,
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

    pipeline_select = (
        db.select(model.Commit.sha, model.Pipeline, func.max(model.Pipeline.created_at))
        .where(model.PrCommitAssociation.pull_request_number == pull.number)
        .join(model.PrCommitAssociation.commit)
        .join(model.Commit.pipelines)
        .group_by(model.Commit.sha)
    )

    pipelines = db.session.execute(pipeline_select).all()

    pipeline_by_commit = {sha: pipeline for sha, pipeline, _ in pipelines}

    return render_template(
        "single_pull.html", pull=pull, pipeline_by_commit=pipeline_by_commit
    )


@bp.route("/<int:number>/patches")
@with_github
async def patches(gh: GitHubAPI, number: int):
    ...
