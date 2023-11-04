from datetime import datetime, timedelta, timezone
import html
from contextvars import ContextVar
import re
from typing import Any, Dict, List, cast
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
import humanize
from gidgethub.aiohttp import GitHubAPI
import gidgethub
from gidgetlab.aiohttp import GitLabAPI
import aiohttp
import sqlalchemy
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix
from apogee.cli import add_cli


from apogee.model.github import User
from apogee.model.gitlab import Job, Pipeline
from apogee.model.record import Patch
from apogee.model.db import db
from apogee.model import db as model
from apogee.web.util import (
    set_last_pipeline_refresh,
    with_github,
    with_gitlab,
    with_session,
)
from apogee.tasks import celery_init_app, handle_job_webhook, handle_pipeline_webhook


from apogee import config
from apogee.util import gather_limit

_is_htmx_var: ContextVar[bool] = ContextVar("is_htmx")
is_htmx: bool = cast(bool, LocalProxy(_is_htmx_var))


oauth = OAuth()
github = GitHub()

oauth.register(
    name="cern",
    server_metadata_url=config.CERN_AUTH_METADATA_URL,
    client_id=config.CERN_AUTH_CLIENT_ID,
    client_secret=config.CERN_AUTH_CLIENT_SECRET,
    client_kwargs={"scope": "openid email profile"},
)


def create_app():
    app = flask.Flask(__name__)

    app.config.from_prefixed_env()
    app.config["SESSION_TYPE"] = "sqlalchemy"
    app.config["SESSION_SQLALCHEMY"] = db

    celery_init_app(app)

    #  app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)

    oauth.init_app(app)

    github.init_app(app)

    db.init_app(app)
    Migrate(app, db)

    Session(app)

    from apogee.web.timeline import bp as timeline_bp
    from apogee.web.pulls import bp as pulls_bp
    from apogee.web.auth import bp as auth_bp

    add_cli(app)

    app.register_blueprint(timeline_bp)
    app.register_blueprint(pulls_bp)
    app.register_blueprint(auth_bp)

    @app.context_processor
    def inject_is_htmx():
        return dict(is_htmx=is_htmx)

    @app.context_processor
    def inject_contents():
        return {
            "humanize": humanize,
            "zip": zip,
            "gh_user": g.gh_user if "gh_user" in g else None,
            "cern_user": g.cern_user if "cern_user" in g else None,
        }

    @app.before_request
    def before_request():
        if "HX-Request" in request.headers and "HX-Boosted" not in request.headers:
            _is_htmx_var.set(True)

    unprotected_endpoints = {"static"}

    def unprotected(fn):
        unprotected_endpoints.add(fn.__name__)
        return fn

    @app.before_request
    async def login_required():
        if request.endpoint in unprotected_endpoints or request.path == "/favicon.ico":
            return
        if (
            "gh_token" not in web_session or "cern_user" not in web_session
        ) and request.endpoint not in (
            "auth.login_github",
            "auth.login",
            "auth.github_callback",
            "auth.cern_login",
            "auth.cern_callback",
        ):
            login_url = url_for("auth.login")
            return redirect(login_url), 302, {"HX-Redirect": login_url}

        if "gh_user" not in web_session:
            if "gh_token" not in web_session:
                g.gh_user = None
            else:
                async with aiohttp.ClientSession() as session:
                    gh = GitHubAPI(
                        session, "apogee", oauth_token=str(web_session["gh_token"])
                    )
                    web_session["gh_user"] = User(**await gh.getitem("/user"))
                g.gh_user = web_session["gh_user"]
        else:
            g.gh_user = web_session["gh_user"]

        g.cern_user = web_session.get("cern_user")

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

    #  @app.errorhandler(Exception)
    #  def htmx_error_handler(e):
    #  if is_htmx:
    #  current_app.logger.error("Handling error", exc_info=e)
    #  flash(str(e), "danger")
    #  return (
    #  render_template("notifications.html", swap=True),
    #  200,
    #  {"HX-Reswap": "none"},
    #  )
    #  #  raise e

    @app.route("/status")
    @unprotected
    def status():
        return "ok"

    @app.route("/")
    async def index():
        return redirect(url_for("timeline.index"))

    @app.route("/reload_pipelines", methods=["POST"])
    @with_gitlab
    async def reload_pipelines(gl: GitLabAPI):
        updated_after = datetime.now(tz=timezone.utc) - timedelta(
            days=config.GITLAB_PIPELINES_WINDOW_DAYS
        )

        pipelines: List[Pipeline] = []

        url = (
            f"/projects/{config.GITLAB_PROJECT_ID}/pipelines"
            + f"?updated_after={updated_after:%Y-%m-%dT%H:%M:%SZ}"
        )
        async for pipeline in gl.getiter(url):
            pipelines.append(Pipeline(**pipeline))

        # let's add pipelines that we currently have as running
        updated_ids = {p.id for p in pipelines}

        for pipeline in db.session.execute(
            db.select(model.Pipeline).where(
                model.Pipeline.status.not_in(
                    ["success", "failed", "skipped", "canceled"]
                )
            )
        ).scalars():
            if pipeline.id in updated_ids:
                continue
            pipelines.append(
                Pipeline(
                    id=pipeline.id,
                    iid=pipeline.iid,
                    project_id=pipeline.project_id,
                    sha=pipeline.sha,
                    ref=pipeline.ref,
                    status=pipeline.status,
                    source=pipeline.source,
                    created_at=pipeline.created_at,
                    updated_at=pipeline.updated_at,
                    web_url=pipeline.web_url,
                    variables=pipeline.variables,
                )
            )

        await gather_limit(
            config.GITLAB_CONCURRENCY_LIMIT,
            *[pipeline.fetch(gl) for pipeline in pipelines],
        )

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

            db_pipeline = model.Pipeline.from_api(pipeline)
            db_pipeline.refreshed_at = datetime.now()
            db.session.merge(db_pipeline)

            db_pipeline.jobs = []

            for job in pipeline.jobs:
                db_job = model.Job.from_api(job)
                db_job.pipeline_id = db_pipeline.id
                db.session.merge(db_job)

        db.session.commit()

        set_last_pipeline_refresh(datetime.now(tz=timezone.utc))

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

        pipeline = model.Pipeline.from_api(api_pipeline)
        pipeline.refreshed_at = datetime.now()
        pipeline = db.session.merge(pipeline)

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
        return "", 200, {"HX-Refresh": "true"}

    @app.route("/reset_patches", methods=["POST"])
    async def reset_patches():
        db.session.execute(sqlalchemy.delete(model.Patch))
        db.session.commit()
        return "", 200, {"HX-Refresh": "true"}

    @app.route("/commit/<sha>")
    async def commit_detail(sha: str) -> str:
        commit = db.get_or_404(model.Commit, sha)
        pull: model.PullRequest | None = None
        if number := request.args.get("pull"):
            pull = db.get_or_404(model.PullRequest, int(number))

        print(pull)

        return render_template("commit_detail.html", commit=commit, pull=pull)

    @app.route("/edit_patches", methods=["GET", "POST"])
    async def edit_patches():
        sha = request.args.get("sha")
        pull = request.args.get("pull")

        obj: model.Commit | model.PullRequest
        render_arg = []
        if sha is not None:
            obj = db.get_or_404(model.Commit, sha)
            render_args = {
                "template_name_or_list": "commit_patches.html",
                "commit": obj,
            }
        elif pull is not None:
            obj = db.get_or_404(model.PullRequest, int(pull))
            render_args = {
                "template_name_or_list": "pull_patches.html",
                "pull": obj,
            }
        else:
            abort(404)

        if request.method == "GET":
            return render_template(
                **render_args,
                create_patch=Patch(id=None, url=""),
            )

        elif request.method == "POST":
            url = request.form.get("url", "").strip()
            if len(url) == 0:
                return render_template(
                    **render_args,
                    patch=Patch(id=None, url=url),
                    error=True,
                )
            patches = list(obj.patches)
            max_order = (max([p.order for p in patches]) + 1) if len(patches) > 0 else 0
            if sha is not None:
                patch = model.Patch(url=url, commit_sha=sha, order=max_order)
            else:
                patch = model.Patch(
                    url=url, pull_request_number=obj.number, order=max_order
                )
            db.session.add(patch)
            db.session.commit()

            return (
                render_template(
                    **render_args,
                    create_patch=Patch(id=None, url=""),
                ),
                200,
                {"HX-Retarget": "body"},
            )

    @app.route("/patch/<patch>/move", methods=["PUT"])
    def patch_move(patch):
        sha = request.args.get("sha")
        pull = request.args.get("pull")

        obj: model.Commit | model.PullRequest
        if sha is not None:
            obj = db.get_or_404(model.Commit, sha)
        elif pull is not None:
            obj = db.get_or_404(model.PullRequest, int(pull))
        else:
            abort(404)

        patch = db.get_or_404(model.Patch, patch)

        patches = obj.patches
        patches.sort(key=lambda p: p.order)
        idx = [p.id for p in patches].index(patch.id)

        direction = request.args["dir"]

        if (direction == "up" and idx == 0) or (
            direction == "down" and idx == len(patches) - 1
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

        obj.patches = []
        obj.patches = patches

        db.session.commit()
        if sha is not None:
            return render_template(
                "commit_patches.html",
                commit=obj,
                create_patch=Patch(id=None, url=""),
            )
        else:
            return render_template(
                "pull_patches.html",
                pull=obj,
                create_patch=Patch(id=None, url=""),
            )

    @app.route("/patch/<patch>", methods=["PUT", "DELETE"])
    def patch(patch):
        sha = request.args.get("sha")
        pull = request.args.get("pull")

        obj: model.Commit | model.PullRequest
        if sha is not None:
            obj = db.get_or_404(model.Commit, sha)
        elif pull is not None:
            obj = db.get_or_404(model.PullRequest, int(pull))
        else:
            abort(404)

        patch = db.get_or_404(model.Patch, patch)

        if request.method == "PUT":
            url = request.form.get("url", "").strip()
            valid = len(url) > 0
            if valid:
                patch.url = url
                db.session.commit()

            all_patches = list(obj.patches)
            first = all_patches[0] == patch
            last = all_patches[-1] == patch

            kwargs = {}
            if sha is not None:
                kwargs["commit"] = obj
            else:
                kwargs["pull"] = obj
            return render_template(
                "patch_form.html",
                patch=patch,
                error=not valid,
                saved=valid,
                first=first,
                last=last,
                **kwargs,
            )
        if request.method == "DELETE":
            db.session.delete(patch)
            db.session.commit()

            if sha is not None:
                return render_template(
                    "commit_patches.html",
                    commit=obj,
                    create_patch=Patch(id=None, url=""),
                )
            else:
                return render_template(
                    "pull_patches.html",
                    pull=obj,
                    create_patch=Patch(id=None, url=""),
                )

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

        # @TODO: In pull case take all of main branch
        commit_select = db.select(model.Commit).order_by(model.Commit.order.desc())

        if pull is None:
            commit_select = commit_select.where(
                model.Commit.order <= trigger_commit.order
            )

        commits = db.session.execute(commit_select).scalars().all()

        reverts = []
        patches: List[model.Patch] = []

        for commit in commits:
            #  print(commit.sha, commit.committed_date, commit.subject)
            if commit.revert:
                reverts.append(commit)
            patches.extend(reversed(sorted(commit.patches, key=lambda p: p.order)))

        patches.reverse()

        if pr is not None:
            patches += sorted(pr.patches, key=lambda p: p.order)

        reverts.reverse()

        variables = {
            "SOURCE_SHA": sha,
        }

        do_report = pr is not None
        if arg := request.args.get("do_report"):
            do_report = arg == "1"
        if not do_report:
            variables["NO_REPORT"] = "1"

        if pr is not None:
            variables["ACTS_GIT_REPO"] = pr.head_repo_clone_url
            variables["ACTS_REF"] = pr.head_ref
            variables["SOURCE_PULL"] = str(pr.number)

        if len(reverts) > 0:
            variables["REVERT_SHAS"] = ",".join(c.sha for c in reverts)

        if len(patches) > 0:
            variables["PATCH_URLS"] = ",".join(p.url for p in patches)
            variables["NO_CANARY"] = "1"

        if request.method == "GET":
            return render_template(
                "run_pipeline.html" if not is_htmx else "run_pipeline_inner.html",
                commit=trigger_commit,
                reverts=reverts,
                patches=patches,
                pr=pr,
                variables=variables,
                do_report=do_report,
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
                if resp.status != 201:
                    info = await resp.json()
                    raise RuntimeError(info["message"])
                resp.raise_for_status()
                pipeline = Pipeline(**await resp.json())

            await pipeline.fetch(gl)

            db_pipeline = model.Pipeline.from_api(pipeline)
            db_pipeline.commit = trigger_commit
            db_pipeline.refreshed_at = datetime.now()
            db.session.add(db_pipeline)
            db.session.commit()

            flash(f"Pipeline started: #{pipeline.id}", "success")
            return (
                "",
                200,
                {
                    "HX-Redirect": url_for(
                        "commit_detail",
                        sha=sha,
                        pull=pr.number if pr is not None else None,
                    )
                },
            )

    async def token_valid(token):
        async with aiohttp.ClientSession() as session:
            gh = GitHubAPI(session, "username", oauth_token=token)
            try:
                await gh.getitem("/user")
                return True
            except gidgethub.BadRequest:
                return False

    @app.route("/logout", methods=["POST"])
    def logout():
        g.gh_user = None
        g.cern_user = None
        web_session.pop("gh_token")
        web_session.pop("gh_user")
        web_session.pop("cern_user")
        if is_htmx:
            return "", 200, {"HX-Redirect": url_for("index")}
        return redirect(url_for("index"))

    @app.route("/webhook/gitlab", methods=["POST"])
    @unprotected
    def gitlab_webhook():
        body = request.json
        if body is None:
            return "ok"
        if body.get("object_kind") == "pipeline":
            handle_pipeline_webhook.delay(body)
        if body.get("object_kind") == "build":
            handle_job_webhook.delay(body)

        return "ok"

    return app
