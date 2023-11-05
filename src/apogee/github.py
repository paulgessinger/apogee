import datetime
from typing import cast

import gidgethub.apps
import aiohttp
from gidgethub.aiohttp import GitHubAPI
import gidgethub.abc
import pydantic

from apogee.cache import cache
from apogee import config
from apogee.model import db as model
from apogee.model.db import PrCommitAssociation, db
from apogee.model.github import Commit, PullRequest


class InstallationToken(pydantic.BaseModel):
    token: str
    expires_at: datetime.datetime


async def get_installation_token(repo: str, session: aiohttp.ClientSession) -> str:
    key = f"installation_token_{repo}"
    if key in cache:
        return cast(str, cache.get(key))

    jwt = gidgethub.apps.get_jwt(
        app_id=config.GITHUB_APP_ID, private_key=config.GITHUB_APP_PRIVATE_KEY
    )

    gh = GitHubAPI(session, "herald")
    installation = await gh.getitem(f"/repos/{repo}/installation", jwt=jwt)
    installation_id = installation["id"]

    response = InstallationToken(
        **await gidgethub.apps.get_installation_access_token(
            gh,
            app_id=config.GITHUB_APP_ID,
            installation_id=installation_id,
            private_key=config.GITHUB_APP_PRIVATE_KEY,
        )
    )

    cache.set(
        key,
        response.token,
        expire=(
            response.expires_at - datetime.datetime.now(tz=datetime.timezone.utc)
        ).total_seconds()
        - 60,
    )

    return response.token


async def get_installation_github(
    repo: str, session: aiohttp.ClientSession
) -> GitHubAPI:
    token = await get_installation_token(repo, session)
    return GitHubAPI(session, "apogee", oauth_token=token)


async def fetch_commits(gh: gidgethub.abc.GitHubAPI) -> int:
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

    return n_fetched


def update_pull_request(pr: PullRequest, commits: list[Commit]) -> None:
    for user in (pr.user, pr.head.user, pr.base.user):
        db.session.merge(model.GitHubUser.from_api(user))

    db_pr = model.PullRequest.from_api(pr)
    db.session.merge(db_pr)

    db.session.execute(
        db.delete(PrCommitAssociation).filter_by(pull_request_number=db_pr.number)
    )
    db_pr.commits.clear()

    for i, commit in enumerate(commits):
        db_commit = model.Commit.from_api(commit)
        db_commit.order = -1
        db.session.merge(db_commit)

        assoc = PrCommitAssociation(
            pull_request_number=db_pr.number, commit_sha=commit.sha, order=i
        )
        db.session.add(assoc)

        db_pr.commits.append(assoc)

    db.session.commit()

    updated = (
        db.session.execute(
            db.select(model.PullRequest).where(model.PullRequest.number == pr.number)
        )
        .scalars()
        .one_or_none()
    )
