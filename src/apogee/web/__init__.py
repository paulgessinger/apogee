from datetime import datetime
import copy
import functools
import itertools
import html
from contextvars import ContextVar
import re
from typing import Any, Dict, cast
from uuid import UUID
import flask
from flask import (
    abort,
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
import click
import threading

from apogee.forms import PatchForm
from apogee.model.github import PullRequest
from apogee.model.gitlab import Job, Pipeline
from apogee.model.record import Patch
from apogee.model.db import db
from apogee.model import db as model
from apogee.web.util import require_login, with_github, with_gitlab, with_session


from apogee import config
from apogee.util import gather_limit

_is_htmx_var = ContextVar("is_htmx")
is_htmx: bool = cast(bool, LocalProxy(_is_htmx_var))


def create_app():
    app = flask.Flask(__name__)

    app.config.from_prefixed_env()
    app.config["SESSION_TYPE"] = "filesystem"

    Session(app)
    github = GitHub(app)

    db.init_app(app)
    migrate = Migrate(app, db)

    from apogee.web.timeline import bp as timeline_bp
    from apogee.web.pulls import bp as pulls_bp

    app.register_blueprint(timeline_bp)
    app.register_blueprint(pulls_bp)

    @app.cli.command("import")
    @click.argument("path")
    def _import(path):
        import json

        with open(path) as f:
            data = json.load(f)

        for sha, commit in data.items():
            c = db.session.execute(
                db.select(model.Commit).filter_by(sha=sha)
            ).scalar_one()
            if c is None:
                print(f"Skipping {sha}")
                continue
            c.note = commit["notes"]

            for i, patch in enumerate(commit["patches"]):
                db.session.add(model.Patch(url=patch, commit=c, order=i))
        db.session.commit()

    @app.context_processor
    def inject_is_htmx():
        return dict(is_htmx=is_htmx)

    @app.context_processor
    def inject_contents():
        return {
            "humanize": humanize,
            "zip": zip,
            "user": g.user if "user" in g else None,
        }

    @app.before_request
    def before_request():
        if "HX-Request" in request.headers and "HX-Boosted" not in request.headers:
            _is_htmx_var.set(True)

    @app.before_request
    async def recover_login():
        if "gh_token" not in web_session:
            return

        if "gh_user" not in web_session:
            async with aiohttp.ClientSession() as session:
                gh = GitHubAPI(
                    session, "apogee", oauth_token=str(web_session["gh_token"])
                )
                web_session["gh_user"] = await gh.getitem("/user")
        g.user = web_session["gh_user"]

    @app.template_filter("datefmt")
    def datefmt(s):
        return s.strftime("%Y-%m-%d %H:%M:%S")

    @app.template_filter("pr_links")
    def pr_links(s):
        def sub(m):
            safe = html.escape(m.group(1))
            return f'<a href="https://github.com/{config.REPOSITORY}/pull/{safe}">#{safe}</a>'

        return re.sub(r"#(\d+)", sub, s)

    @app.template_filter("markdown")
    def render_markdown(s):
        return markdown.markdown(s)

    @app.route("/")
    def index():
        if hasattr(g, "user") and g.user is not None:
            return redirect(url_for("timeline.index"))

        return render_template("index.html")

    @app.route("/reload_pipelines", methods=["POST"])
    @with_gitlab
    async def reload_pipelines(gl: GitLabAPI):
        pipelines = []
        async for pipeline in gl.getiter(
            f"/projects/{config.GITLAB_PROJECT_ID}/pipelines"
        ):
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
                    db.select(model.Commit).filter_by(
                        sha=pipeline.variables["SOURCE_SHA"]
                    )
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
            db.session.execute(
                db.select(model.Commit).order_by(model.Commit.order.desc())
            )
            .scalars()
            .all()
        )

        source = request.args["source"]
        if source not in ("timeline", "pulls"):
            abort(400)
        flash(f"Fetched {len(pipelines)} pipelines", "success")
        return redirect(url_for(f"{source}.index"))

    @app.route("/reload_pipeline/<int:pipeline_id>", methods=["POST"])
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
    async def toggle_revert(sha):
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
    async def reset_reverts():
        db.session.execute(sqlalchemy.update(model.Commit).values(revert=False))
        db.session.commit()
        commits = (
            db.session.execute(
                db.select(model.Commit).order_by(model.Commit.order.desc())
            )
            .scalars()
            .all()
        )
        return render_template(
            "commits.html",
            commits=commits,
        )

    @app.route("/reset_patches", methods=["POST"])
    async def reset_patches():
        db.session.execute(sqlalchemy.delete(model.Patch))
        db.session.commit()
        commits = (
            db.session.execute(
                db.select(model.Commit).order_by(model.Commit.order.desc())
            )
            .scalars()
            .all()
        )
        return render_template(
            "commits.html",
            commits=commits,
        )

    @app.route("/commit/<sha>")
    async def commit_detail(sha: str) -> str:
        commit = db.get_or_404(model.Commit, sha)
        pull: model.PullRequest | None = None
        if number := request.args.get("pull"):
            pull = db.get_or_404(model.PullRequest, int(number))

        print(pull)

        return render_template("commit_detail.html", commit=commit, pull=pull)

    @app.route("/commit/<sha>/patches", methods=["GET", "POST"])
    async def commit_patches(sha):
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
            return render_template(
                "patch_form.html",
                commit=commit,
                patch=patch,
                error=not valid,
                saved=valid,
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

    @app.route("/run_pipeline", methods=["GET", "POST"])
    @with_gitlab
    @with_github
    @with_session
    async def run_pipeline(
        gl: GitLabAPI, gh: GitHubAPI, session: aiohttp.ClientSession
    ):
        sha = request.args.get("sha")
        pull = request.args.get("pull")
        if sha is None and pull is None:
            abort(404)

        pr: model.PullRequest | None = None
        if pull is not None:
            pr = db.session.execute(
                db.select(model.PullRequest).filter_by(number=pull)
            ).scalar_one()
            assert pr is not None
            sha = pr.head_sha

        trigger_commit = db.get_or_404(model.Commit, sha)

        commits = (
            db.session.execute(
                db.select(model.Commit)
                .order_by(model.Commit.order.desc())
                .where(model.Commit.order <= trigger_commit.order)
            )
            .scalars()
            .all()
        )

        reverts = []
        patches = []

        for commit in commits:
            #  print(commit.sha, commit.committed_date, commit.subject)
            if commit.revert:
                reverts.append(commit)
            patches.extend(commit.patches)

        reverts.reverse()
        patches.reverse()

        variables = {
            "SOURCE_SHA": sha,
            "NO_REPORT": "1",
        }

        if len(reverts) > 0:
            variables["REVERT_SHAS"] = ",".join(c.sha for c in reverts)

        if len(patches) > 0:
            variables["PATCH_URLS"] = ",".join(p.url for p in patches)
            variables["NO_CANARY"] = "1"

        if request.method == "GET":
            return render_template(
                "run_pipeline.html",
                commit=trigger_commit,
                reverts=reverts,
                patches=patches,
                pr=pr,
                variables=variables,
            )
        elif request.method == "POST":
            url = f"{config.GITLAB_URL}/api/v4/projects/{config.GITLAB_PROJECT_ID}/trigger/pipeline"

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

            db_pipeline = model.Pipeline.from_api(pipeline)
            db_pipeline.commit = trigger_commit
            db.session.add(db_pipeline)
            db.session.commit()

            flash(f"Pipeline started: #{pipeline.id}", "success")
            return "", 200, {"HX-Redirect": url_for("commit_detail", sha=sha)}

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

    def update_pipeline(payload: Dict[str, Any]):
        def proc_datetime(s):
            if s is None:
                return None
            date, time, tz = s.split(" ")
            return f"{date}T{time}{tz}"

        with app.app_context():
            data = payload["object_attributes"]
            api_pipeline = Pipeline(
                id=data["id"],
                iid=data["iid"],
                project_id=payload["project"]["id"],
                sha=data["sha"],
                ref=data["ref"],
                status=data["status"],
                source=data["source"],
                created_at=proc_datetime(data["created_at"]),
                updated_at=datetime.now(),
                web_url=f"{config.GITLAB_URL}/{config.GITLAB_PROJECT}/-/pipelines/{data['id']}",
                variables={v["key"]: v["value"] for v in data["variables"]},
            )
            api_pipeline.jobs = []

            for j in payload["builds"]:
                api_pipeline.jobs.append(
                    Job(
                        id=j["id"],
                        status=j["status"],
                        stage=j["stage"],
                        name=j["name"],
                        ref=data["ref"],
                        allow_failure=j["allow_failure"],
                        created_at=proc_datetime(j["created_at"]),
                        started_at=proc_datetime(j["started_at"]),
                        finished_at=proc_datetime(j["finished_at"]),
                        web_url=f"{config.GITLAB_URL}/{config.GITLAB_PROJECT}/-/jobs/{j['id']}",
                        failure_reason=j["failure_reason"],
                    )
                )

            if "SOURCE_SHA" not in api_pipeline.variables:
                # can't associate with commit
                return

            if (
                db.session.execute(
                    db.select(model.Commit).filter_by(
                        sha=api_pipeline.variables["SOURCE_SHA"]
                    )
                ).scalar_one_or_none()
                is None
            ):
                # ignore these, we don't care about these commits
                return

            db_pipeline = db.session.merge(model.Pipeline.from_api(api_pipeline))
            db_pipeline.jobs = []

            for job in api_pipeline.jobs:
                db_job = model.Job.from_api(job)
                db_job.pipeline_id = db_pipeline.id
                db.session.merge(db_job)

            db.session.commit()

    @app.route("/webhook/gitlab", methods=["POST"])
    def gitlab_webhook():
        body = request.json
        if body is None:
            return "ok"
        if body.get("object_kind") == "pipeline":
            t = threading.Thread(target=update_pipeline, args=(body,))
            t.start()

        return "ok"

    @app.route("/github-callback")
    @github.authorized_handler
    def authorized(oauth_token):
        next_url = request.args.get("next") or url_for("timeline.index")

        web_session["gh_token"] = oauth_token

        return redirect(next_url)

    return app
