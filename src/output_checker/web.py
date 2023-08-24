from datetime import datetime
import functools
import itertools
import html
from contextvars import ContextVar
import re
from typing import cast
from uuid import UUID
import flask
from flask import (
    flash,
    render_template,
    session as web_session,
    redirect,
    url_for,
    request,
)
from flask_session import Session
from werkzeug.local import LocalProxy
import markdown

from output_checker.forms import PatchForm
from output_checker.model.gitlab import Pipeline
from output_checker.model.record import ExtendedCommit, Patch

from output_checker.repository import Repository
from output_checker.repository.shelve import ShelveRepository

import humanize
from gidgethub.aiohttp import GitHubAPI
import gidgethub
from gidgetlab.aiohttp import GitLabAPI
import aiohttp

from output_checker import config
from output_checker.util import gather_limit

_repository_var = ContextVar("repository")
repository: Repository = cast(Repository, LocalProxy(_repository_var))

_is_htmx_var = ContextVar("is_htmx")
is_htmx: bool = cast(bool, LocalProxy(_is_htmx_var))


app = flask.Flask(__name__)

app.config.from_prefixed_env()
app.config["SESSION_TYPE"] = "filesystem"

Session(app)


@app.context_processor
def inject_is_htmx():
    return dict(is_htmx=is_htmx)


@app.context_processor
def inject_contents():
    return {
        "repository": repository,
        "pipelines": repository.pipelines(),
        "humanize": humanize,
        "zip": zip,
    }


@app.before_request
def before_request():
    repo = ShelveRepository(config.DB_PATH)
    _repository_var.set(repo)
    if "HX-Request" in request.headers:
        _is_htmx_var.set(True)


@app.template_filter("datefmt")
def datefmt(s):
    return s.strftime("%Y-%m-%d %H:%M:%S")


@app.template_filter("pr_links")
def pr_links(s):
    def sub(m):
        safe = html.escape(m.group(1))
        return (
            f'<a href="https://github.com/{config.REPOSITORY}/pull/{safe}">#{safe}</a>'
        )

    return re.sub(r"#(\d+)", sub, s)


@app.template_filter("markdown")
def render_markdown(s):
    return markdown.markdown(s)


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
            return redirect(url_for("login"))
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


@app.route("/")
@with_github
async def timeline(gh: GitHubAPI):
    commits = list(itertools.islice(repository.commit_sequence(), config.MAX_COMMITS))
    return render_template(
        "timeline.html",
        commits=commits,
        pipelines=repository.pipelines(),
    )


@app.route("/reload_commits", methods=["POST"])
@with_github
async def timeline_reload_commits(gh: GitHubAPI):
    seq = list(repository.commit_sequence())
    last_commit = seq[0] if len(seq) > 0 else None

    fetched_commits = []

    async for data in gh.getiter(f"/repos/{config.REPOSITORY}/commits"):
        if len(fetched_commits) >= config.MAX_COMMITS:
            break

        commit = ExtendedCommit(**data)

        if last_commit is not None and commit.sha == last_commit.sha:
            break

        fetched_commits.append(commit)

    for commit in fetched_commits:
        repository.add_commit(commit, update_on_conflict=True)
    seq = fetched_commits + seq

    repository.save_commit_sequence((commit.sha for commit in seq))

    flash(f"Fetched {len(fetched_commits)} commits", "success")

    return render_template(
        "commits.html",
        commits=seq[: config.MAX_COMMITS],
    )


@app.route("/reload_pipelines", methods=["POST"])
@require_login
@with_gitlab
async def timeline_reload_pipelines(gl: GitLabAPI):
    pipelines = []
    async for pipeline in gl.getiter(f"/projects/{config.GITLAB_PROJECT_ID}/pipelines"):
        if len(pipelines) >= config.GITLAB_PIPELINES_LIMIT:
            break
        pipelines.append(Pipeline(**pipeline))

    await gather_limit(15, *[pipeline.fetch(gl) for pipeline in pipelines])

    for pipeline in pipelines:
        repository.add_pipeline(pipeline, update_on_conflict=True)

    #  for pipeline in pipelines:
    #  print(pipeline.model_dump_json(indent=2))

    pipeline_by_source_sha = {}
    for pipeline in pipelines:
        if "SOURCE_SHA" in pipeline.variables:
            pipeline_by_source_sha[pipeline.variables["SOURCE_SHA"]] = pipeline

    commits = repository.commit_sequence()
    for commit in commits:
        if commit.sha in pipeline_by_source_sha:
            commit.pipelines.add(pipeline_by_source_sha[commit.sha].id)
            repository.update_commit(commit)

    flash(f"Fetched {len(pipelines)} pipelines", "success")
    return render_template(
        "commits.html",
        commits=itertools.islice(repository.commit_sequence(), config.MAX_COMMITS),
    )


@app.route("/reload_pipeline/<int:pipeline_id>", methods=["POST"])
@require_login
@with_gitlab
async def reload_pipeline(gl: GitLabAPI, pipeline_id):
    pipeline = repository.get_pipeline(pipeline_id)

    pipeline = Pipeline(
        **await gl.getitem(
            f"/projects/{config.GITLAB_PROJECT_ID}/pipelines/{pipeline.id}"
        )
    )
    await pipeline.fetch(gl)

    repository.update_pipeline(pipeline)

    return render_template("pipeline.html", pipeline=pipeline, expanded=True)


@app.route("/toggle_revert/<sha>", methods=["POST"])
def toggle_revert(sha):
    return f"""
<input
   type="checkbox"
   hx-post="{ url_for('toggle_revert', sha=sha) }"
   { 'checked' if repository.toggle_revert(sha) else ''}
>
"""


@app.route("/reset_reverts", methods=["POST"])
def reset_reverts():
    repository.reset_reverts()
    return render_template(
        "commits.html",
        commits=itertools.islice(repository.commit_sequence(), config.MAX_COMMITS),
    )


@app.route("/commit/<sha>")
def commit_detail(sha):
    commit = repository.get_commit(sha)

    return render_template("commit_detail.html", commit=commit)


@app.route("/commit/<sha>/patches", methods=["GET", "POST"])
def commit_patches(sha):
    commit = repository.get_commit(sha)

    if request.method == "GET":
        return render_template(
            "commit_patches.html",
            commit=commit,
            create_patch=Patch(id=None, url=""),
        )

    elif request.method == "POST":
        url = request.form.get("url", "").strip()
        if len(url) == 0:
            return render_template(
                "patch_form.html",
                commit=commit,
                patch=Patch(id=None, url=url),
                error=True,
            )
        patch = Patch(url=url)
        commit.patches.append(patch)
        repository.update_commit(commit)

        return render_template(
            "patch_form.html", commit=commit, patch=patch
        ) + render_template(
            "patch_form.html", commit=commit, patch=Patch(id=None, url="")
        )


@app.route("/commit/<sha>/patch/<patch>/move", methods=["PUT"])
def commit_patch_move(sha, patch):
    commit = repository.get_commit(sha)
    idx = [p.id for p in commit.patches].index(UUID(patch))

    direction = request.args["dir"]

    if (direction == "up" and idx == 0) or (
        direction == "down" and idx == len(commit.patches) - 1
    ):
        return "no", 400

    if direction == "up":
        a, b = commit.patches[idx], commit.patches[idx - 1]
        commit.patches[idx] = b
        commit.patches[idx - 1] = a
    elif direction == "down":
        a, b = commit.patches[idx], commit.patches[idx + 1]
        commit.patches[idx] = b
        commit.patches[idx + 1] = a
    repository.update_commit(commit)
    return render_template(
        "commit_patches.html",
        commit=commit,
        create_patch=Patch(id=None, url=""),
    )


@app.route("/commit/<sha>/patch/<patch>", methods=["PUT", "DELETE"])
def commit_patch(sha, patch):
    commit = repository.get_commit(sha)

    if request.method == "PUT":
        url = request.form.get("url", "").strip()
        valid = len(url) > 0
        idx = [p.id for p in commit.patches].index(UUID(patch))
        commit.patches[idx].url = url
        patch = commit.patches[idx]
        if valid:
            repository.update_commit(commit)
        return render_template(
            "patch_form.html", commit=commit, patch=patch, error=not valid, saved=valid
        )
    if request.method == "DELETE":
        idx = [p.id for p in commit.patches].index(UUID(patch))
        del commit.patches[idx]
        repository.update_commit(commit)
        return ""


@app.route("/commit/<sha>/note")
def commit_note(sha):
    commit = repository.get_commit(sha)

    return render_template("commit_note.html", commit=commit)


@app.route("/commit/<sha>/note/edit", methods=["GET", "POST"])
def commit_note_edit(sha):
    commit = repository.get_commit(sha)

    if request.method == "POST":
        content = request.form.get("content", "")
        commit.notes = content
        repository.update_commit(commit)

        return render_template("commit_note.html", commit=commit)

    content = getattr(commit, "notes", "")

    return render_template("commit_note_form.html", commit=commit, content=content)


@app.route("/run_pipeline/<sha>", methods=["GET", "POST"])
@require_login
@with_gitlab
@with_session
async def run_pipeline(gl: GitLabAPI, session: aiohttp.ClientSession, sha):
    commit = repository.get_commit(sha)

    seq = list(repository.commit_sequence())
    seq = seq[seq.index(commit) :]

    reverts = [c for c in seq if c.revert]
    reverts.reverse()
    patches = sum([c.patches for c in seq], [])
    patches.reverse()

    if request.method == "GET":
        return render_template(
            "run_pipeline.html", commit=commit, reverts=reverts, patches=patches
        )
    elif request.method == "POST":
        url = f"{config.GITLAB_URL}/api/v4/projects/{config.GITLAB_PROJECT_ID}/trigger/pipeline"

        variables = {
            "SOURCE_SHA": sha,
            "NO_REPORT": "1",
            "NO_CANARY": "1",
        }

        if len(reverts) > 0:
            variables["REVERT_SHAS"] = ",".join(c.sha for c in reverts)

        if len(patches) > 0:
            variables["PATCH_URLS"] = ",".join(p.url for p in patches)

        async with session.post(
            url,
            data={
                "token": config.GITLAB_TRIGGER_TOKEN,
                "ref": "main",
                **{f"variables[{k}]": v for k, v in variables.items()},
            },
        ) as resp:
            resp.raise_for_status()
            pipeline = Pipeline(**await resp.json())

        await pipeline.fetch(gl)

        commit.pipelines.add(pipeline.id)
        repository.update_commit(commit)
        repository.add_pipeline(pipeline)
        flash(f"Pipeline started: #{pipeline.id}", "success")
        return "", 200, {"HX-Redirect": url_for("commit_detail", sha=sha)}
        #  return redirect(url_for("commit_detail", sha=sha))
        #  return (
        #  render_template("commit_detail.html", commit=commit),
        #  200,
        #  {"HX-Push": url_for("commit_detail", sha=sha)},
        #  )


async def token_valid(token):
    async with aiohttp.ClientSession() as session:
        gh = GitHubAPI(session, "username", oauth_token=token)
        try:
            await gh.getitem("/user")
            return True
        except gidgethub.BadRequest:
            return False


@app.route("/login", methods=["GET", "POST"])
async def login():
    error = None
    token = None
    if request.method == "POST":
        token = request.form.get("gh_token")
        if token is None or len(token) == "" or not await token_valid(token):
            error = "Invalid token"
        else:
            web_session["gh_token"] = token
            return redirect(url_for("index"))
    return render_template("login.html", error=error, token=token)
