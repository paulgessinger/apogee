import functools
import aiohttp

from flask import session as web_session, redirect, url_for, g
from gidgethub.aiohttp import GitHubAPI
from gidgetlab.aiohttp import GitLabAPI

from apogee import config


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
            return redirect(url_for("index"))

        if "gh_user" not in web_session:
            async with aiohttp.ClientSession() as session:
                gh = GitHubAPI(
                    session, "apogee", oauth_token=str(web_session["gh_token"])
                )
                web_session["gh_user"] = await gh.getitem("/user")
        g.user = web_session["gh_user"]

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
    @functools.wraps(fn)
    @with_session
    async def wrapped(*args, session: aiohttp.ClientSession, **kwargs):
        gl = GitLabAPI(
            session, "username", access_token=config.GITLAB_TOKEN, url=config.GITLAB_URL
        )
        kwargs["gl"] = gl
        return await fn(*args, **kwargs)

    return wrapped
