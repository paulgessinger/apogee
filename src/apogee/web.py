from datetime import datetime
import copy
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
    g,
    render_template,
    session as web_session,
    redirect,
    url_for,
    request,
)
from flask_migrate import Migrate
from flask_session import Session
from werkzeug.local import LocalProxy
import markdown
from flask_github import GitHub
from flask_sqlalchemy import SQLAlchemy
import humanize
from gidgethub.aiohttp import GitHubAPI
import gidgethub
from gidgetlab.aiohttp import GitLabAPI
import aiohttp
import sqlalchemy

from apogee.forms import PatchForm
from apogee.model.gitlab import Pipeline
from apogee.model.github import Commit
from apogee.model.record import ExtendedCommit, Patch
from apogee.repository import Repository
from apogee.repository.shelve import ShelveRepository
from apogee.model.db import db
from apogee.model import db as model


from apogee import config
from apogee.util import gather_limit

_repository_var = ContextVar("repository")
repository: Repository = cast(Repository, LocalProxy(_repository_var))

_is_htmx_var = ContextVar("is_htmx")
is_htmx: bool = cast(bool, LocalProxy(_is_htmx_var))


app = flask.Flask(__name__)

app.config.from_prefixed_env()
app.config["SESSION_TYPE"] = "filesystem"

Session(app)
github = GitHub(app)

db.init_app(app)
migrate = Migrate(app, db)


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
        "user": g.user if "user" in g else None,
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


@app.route("/")
def index():
    if hasattr(g, "user") and g.user is not None:
        return redirect(url_for("timeline"))

    return render_template("index.html")


@app.route("/timeline")
@with_github
async def timeline(gh: GitHubAPI):
    commits = (
        db.session.execute(db.select(model.Commit).order_by(model.Commit.order.desc()))
        .scalars()
        .all()
    )
    return render_template(
        "timeline.html",
        commits=commits,
        pipelines=repository.pipelines(),
    )


@app.route("/reload_commits", methods=["POST"])
@with_github
async def timeline_reload_commits(gh: GitHubAPI):
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
        author = db.session.execute(
            db.select(model.GitHubUser).filter_by(id=api_commit.author.id)
        ).scalar_one_or_none()

        if author is None:
            author = model.GitHubUser(
                id=api_commit.author.id, login=api_commit.author.login
            )
            db.session.add(author)

        committer = db.session.execute(
            db.select(model.GitHubUser).filter_by(id=api_commit.committer.id)
        ).scalar_one_or_none()

        if committer is None:
            committer = model.GitHubUser(
                id=api_commit.committer.id, login=api_commit.committer.login
            )
            db.session.add(committer)

        commit = model.Commit.from_api(api_commit)
        commit.author = author
        commit.committer = committer
        commit.order = latest_order + index + 1
        db.session.merge(commit)

    db.session.commit()

    flash(f"Fetched {n_fetched} commits", "success")

    commits = (
        db.session.execute(db.select(model.Commit).order_by(model.Commit.order.desc()))
        .scalars()
        .all()
    )

    return render_template(
        "commits.html",
        commits=commits,
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
        if "SOURCE_SHA" not in pipeline.variables:
            # can't associate with commit
            continue

        if (
            db.session.execute(
                db.select(model.Commit).filter_by(sha=pipeline.variables["SOURCE_SHA"])
            ).scalar_one_or_none()
            is None
        ):
            # ignore these, we don't care about these commits
            continue

        db_pipeline = db.session.merge(model.Pipeline.from_api(pipeline))

        db_pipeline.jobs = []

        for job in pipeline.jobs:
            db_job = model.Job.from_api(job)
            db_job.pipeline_id = db_pipeline.id
            db.session.merge(db_job)

    db.session.commit()

    commits = (
        db.session.execute(db.select(model.Commit).order_by(model.Commit.order.desc()))
        .scalars()
        .all()
    )

    flash(f"Fetched {len(pipelines)} pipelines", "success")
    return render_template(
        "commits.html",
        commits=commits,
    )


@app.route("/reload_pipeline/<int:pipeline_id>", methods=["POST"])
@require_login
@with_gitlab
async def reload_pipeline(gl: GitLabAPI, pipeline_id):
    pipeline = db.get_or_404(model.Pipeline, pipeline_id)

    api_pipeline = Pipeline(
        **await gl.getitem(
            f"/projects/{config.GITLAB_PROJECT_ID}/pipelines/{pipeline.id}"
        )
    )
    await api_pipeline.fetch(gl)

    pipeline = db.session.merge(model.Pipeline.from_api(api_pipeline))

    pipeline.jobs = []

    for job in api_pipeline.jobs:
        db_job = model.Job.from_api(job)
        db_job.pipeline_id = pipeline.id
        db.session.merge(db_job)

    db.session.commit()

    return render_template("pipeline.html", pipeline=pipeline, expanded=True)


@app.route("/toggle_revert/<sha>", methods=["POST"])
def toggle_revert(sha):
    commit = db.get_or_404(model.Commit, sha)
    commit.revert = not commit.revert
    db.session.commit()

    return f"""
<input
   type="checkbox"
   hx-post="{ url_for('toggle_revert', sha=sha) }"
   { 'checked' if commit.revert else ''}
>
"""


@app.route("/reset_reverts", methods=["POST"])
def reset_reverts():
    db.session.execute(sqlalchemy.update(model.Commit).values(revert=False))
    db.session.commit()
    commits = (
        db.session.execute(db.select(model.Commit).order_by(model.Commit.order.desc()))
        .scalars()
        .all()
    )
    return render_template(
        "commits.html",
        commits=commits,
    )


@app.route("/commit/<sha>")
def commit_detail(sha):
    commit = db.get_or_404(model.Commit, sha)

    return render_template("commit_detail.html", commit=commit)


@app.route("/commit/<sha>/patches", methods=["GET", "POST"])
def commit_patches(sha):
    commit = db.get_or_404(model.Commit, sha)

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
        max_order = (
            (max([p.order for p in commit.patches]) + 1)
            if len(commit.patches) > 0
            else 0
        )
        patch = model.Patch(url=url, commit_sha=commit.sha, order=max_order)
        db.session.add(patch)
        db.session.commit()

        return (
            render_template(
                "commit_patches.html",
                commit=commit,
                create_patch=Patch(id=None, url=""),
            ),
            200,
            {"HX-Retarget": "body"},
        )


@app.route("/commit/<sha>/patch/<patch>/move", methods=["PUT"])
def commit_patch_move(sha, patch):
    commit = db.get_or_404(model.Commit, sha)
    patch = db.get_or_404(model.Patch, patch)
    patches = commit.patches
    #  patches = [copy.deepcopy(p) for p in commit.patches]
    patches.sort(key=lambda p: p.order)
    idx = [p.id for p in patches].index(patch.id)

    direction = request.args["dir"]

    if (direction == "up" and idx == 0) or (
        direction == "down" and idx == len(commit.patches) - 1
    ):
        return "no", 400

    print([(p.url, p.order) for p in patches])

    if direction == "up":
        a, b = patches[idx - 1], patches[idx]
        patches[idx - 1] = b
        patches[idx] = a

    elif direction == "down":
        a, b = patches[idx], patches[idx + 1]
        patches[idx] = b
        patches[idx + 1] = a

    for i, p in enumerate(patches):
        p.order = i

    commit.patches = []
    commit.patches = patches

    db.session.commit()
    return render_template(
        "commit_patches.html",
        commit=commit,
        create_patch=Patch(id=None, url=""),
    )


@app.route("/commit/<sha>/patch/<patch>", methods=["PUT", "DELETE"])
def commit_patch(sha, patch):
    commit = db.get_or_404(model.Commit, sha)
    patch = db.get_or_404(model.Patch, patch)

    if request.method == "PUT":
        url = request.form.get("url", "").strip()
        valid = len(url) > 0
        #  idx = [p.id for p in commit.patches].index(UUID(patch))
        #  commit.patches[idx].url = url
        #  patch = commit.patches[idx]
        if valid:
            patch.url = url
            db.session.commit()
            #  repository.update_commit(commit)
        return render_template(
            "patch_form.html", commit=commit, patch=patch, error=not valid, saved=valid
        )
    if request.method == "DELETE":
        db.session.delete(patch)
        db.session.commit()
        return ""


@app.route("/commit/<sha>/note")
def commit_note(sha):
    commit = db.get_or_404(model.Commit, sha)

    return render_template("commit_note.html", commit=commit)


@app.route("/commit/<sha>/note/edit", methods=["GET", "POST"])
def commit_note_edit(sha):
    commit = db.get_or_404(model.Commit, sha)

    if request.method == "POST":
        content = request.form.get("content", "")
        commit.note = content
        db.session.commit()

        return render_template("commit_note.html", commit=commit)

    content = commit.note

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
    return github.authorize()


@app.route("/logout", methods=["POST"])
def logout():
    g.user = None
    del web_session["gh_token"]
    del web_session["gh_user"]
    if is_htmx:
        return "", 200, {"HX-Redirect": url_for("index")}
    return redirect(url_for("index"))


@app.route("/github-callback")
@github.authorized_handler
def authorized(oauth_token):
    next_url = request.args.get("next") or url_for("timeline")

    web_session["gh_token"] = oauth_token

    return redirect(next_url)

    #  if oauth_token is None:
    #  flash("Authorization failed.")
    #  return redirect(next_url)

    #  user = User.query.filter_by(github_access_token=oauth_token).first()
    #  if user is None:
    #  user = User(oauth_token)
    #  db_session.add(user)

    #  user.github_access_token = oauth_token
    #  db_session.commit()
    #  return redirect(next_url)
