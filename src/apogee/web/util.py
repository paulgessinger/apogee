import asyncio
from datetime import datetime
import functools
import aiohttp
import inspect

from flask import session as web_session, redirect, url_for, g
from gidgethub.aiohttp import GitHubAPI
from gidgetlab.aiohttp import GitLabAPI

from apogee import config
from apogee.model.db import KeyValue, db


def with_session(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        async with aiohttp.ClientSession() as session:
            kwargs["session"] = session
            return await fn(*args, **kwargs)

    return wrapped


def require_login(fn):
    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        if "gh_token" not in web_session:
            return redirect(url_for("login_github"))

        if "gh_user" not in web_session:
            async with aiohttp.ClientSession() as session:
                gh = GitHubAPI(
                    session, "apogee", oauth_token=str(web_session["gh_token"])
                )
                web_session["gh_user"] = await gh.getitem("/user")
        g.gh_user = web_session["gh_user"]

        return await fn(*args, **kwargs)

    return wrapped


def with_github(fn):
    @functools.wraps(fn)
    @with_session
    @require_login
    async def wrapped(*args, session: aiohttp.ClientSession, **kwargs):
        gh = GitHubAPI(session, "username", oauth_token=str(web_session["gh_token"]))
        kwargs["gh"] = gh
        return await fn(*args, **kwargs)

    return wrapped


def with_gitlab(fn):
    signature = inspect.signature(fn)

    wants_session = "session" in signature.parameters

    @functools.wraps(fn)
    @with_session
    async def wrapped(*args, session: aiohttp.ClientSession, **kwargs):
        gl = GitLabAPI(
            session, "username", access_token=config.GITLAB_TOKEN, url=config.GITLAB_URL
        )
        kwargs["gl"] = gl
        if wants_session:
            kwargs["session"] = session
        return await fn(*args, **kwargs)

    return wrapped


def get_last_pipeline_refresh() -> datetime | None:
    obj = db.session.execute(
        db.select(KeyValue).where(KeyValue.key == "last_pipeline_refresh")
    ).scalar_one_or_none()
    if obj is None:
        return None
    return datetime.fromisoformat(obj.value)


def set_last_pipeline_refresh(to: datetime):
    obj = KeyValue(key="last_pipeline_refresh", value=to.isoformat())
    db.session.merge(obj)
    db.session.commit()
