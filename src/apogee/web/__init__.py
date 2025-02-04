from dataclasses import dataclass
import hashlib
from datetime import datetime, timedelta, timezone
import html
from contextvars import ContextVar
import re
from typing import Any, Dict, List, cast
from authlib.integrations.flask_client import OAuthError
import flask
from flask import Response
from flask import (
    abort,
    current_app,
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
from webdav4.fsspec import WebdavFileSystem
import redis
from werkzeug.local import LocalProxy
import html
import markdown
import humanize
from gidgethub.aiohttp import GitHubAPI
import gidgethub
from gidgetlab.aiohttp import GitLabAPI
import aiohttp
import sqlalchemy
import sqlalchemy.exc
import sqlalchemy.orm
import sqlalchemy.sql.functions as func
from werkzeug.middleware.proxy_fix import ProxyFix
import logging
from async_lru import alru_cache

from apogee.cli import add_cli
from apogee.model.github import User, UserResponse
from apogee.model.gitlab import CompareResult, Job, Pipeline
from apogee.model.record import Patch
from apogee.model.db import db
from apogee.model import db as model
from apogee.web.pulls import pull_index_view
from apogee.web.timeline import timeline_commits_view
from apogee.web.auth import oauth
from apogee.web.util import (
    set_last_pipeline_refresh,
    with_github,
    with_gitlab,
    with_session,
)
from apogee.tasks import (
    celery_init_app,
    handle_job_webhook,
    handle_pipeline_webhook,
    handle_pull_request,
    handle_push,
)


from apogee import config
from apogee.cache import cache
from apogee.util import (
    execute_reference_update,
    gather_limit,
    get_object_counts_diffs,
    create_patch_from_diffs,
    combine_diffs,
    get_pipeline_references,
    parse_pipeline_url,
)

logging.basicConfig(format="%(levelname)s %(name)s %(message)s")
logging.getLogger().setLevel(logging.INFO)

_is_htmx_var: ContextVar[bool] = ContextVar("is_htmx")
is_htmx: bool = cast(bool, LocalProxy(_is_htmx_var))


@dataclass
class PatchContent:
    author: str
    date: str
    subject: str


@alru_cache(maxsize=128)
async def load_patch(url: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            content = await resp.text()

    lines = content.split("\n")
    author, date = lines[1:3]

    subject_end = lines.index("---")

    subject = "\n".join(lines[3:subject_end])

    author = re.match(r"From: (.*)", author).group(1)
    date = re.match(r"Date: (.*)", date).group(1)
    subject = re.match(r"Subject: \[PATCH\] (.*)", subject, re.DOTALL).group(1)

    return PatchContent(author=author, date=date, subject=subject)


def create_app():
    app = flask.Flask(__name__)

    app.config.from_prefixed_env()
    app.config.setdefault("SESSION_TYPE", "sqlalchemy")
    if app.config["SESSION_TYPE"] == "sqlalchemy":
        app.config["SESSION_SQLALCHEMY"] = db
    elif app.config["SESSION_TYPE"] == "redis":
        app.config["SESSION_REDIS"] = redis.from_url(config.SESSION_REDIS_URL)

    #  logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)

    celery_init_app(app)

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)

    oauth.init_app(app)

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
        auth_endpoints = (
            "auth.login_github",
            "auth.login",
            "auth.github_callback",
            "auth.cern_login",
            "auth.cern_callback",
        )
        if (
            "gh_token" not in web_session or "cern_user" not in web_session
        ) and request.endpoint not in auth_endpoints:
            login_url = url_for("auth.login")
            return redirect(login_url), 302, {"HX-Location": login_url}

        if "gh_token" in web_session and request.endpoint not in auth_endpoints:
            # check token validity
            now = datetime.now(tz=timezone.utc).timestamp()
            if now > web_session["gh_token"]["expires_at"]:
                try:
                    #  I **think** this should refresh the token?
                    oauth.github.get("user")
                except OAuthError:
                    # log out from github
                    web_session.pop("gh_token")
                    web_session.pop("gh_user")
                    login_url = url_for("auth.login")
                    return redirect(login_url), 302, {"HX-Location": login_url}

        if "gh_user" not in web_session:
            if "gh_token" not in web_session:
                g.gh_user = None
            else:
                resp = oauth.github.get("user")
                resp.raise_for_status()
                user = UserResponse(**resp.json())

                web_session["gh_user"] = User(**user.dict())
                g.gh_user = web_session["gh_user"]
        else:
            g.gh_user = web_session["gh_user"]

        g.cern_user = web_session.get("cern_user")

    @app.template_filter("datefmt")
    def datefmt(s):
        return s.strftime("%Y-%m-%dT%H:%M:%S")

    @app.template_filter("datezulu")
    def datezulu(s):
        return s.strftime("%Y-%m-%dT%H:%M:%SZ")

    @app.template_filter("pr_links")
    def pr_links(s):
        def sub(m):
            safe = html.escape(m.group(1) or m.group(2))
            return f'<a href="https://github.com/{config.REPOSITORY}/pull/{safe}">#{safe}</a>'

        return re.sub(
            rf"(?:#(\d+)|https://github.com/{config.REPOSITORY}/pull/(\d+))", sub, s
        )

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
            db_pipeline.refreshed_at = datetime.utcnow()
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
        if source == "timeline":
            return timeline_commits_view(frame=False)
        else:
            return pull_index_view(frame=False)

    @app.route("/update_references/<int:pipeline_id>", methods=["GET", "POST"])
    @with_gitlab
    @with_session
    async def update_references(
        session: aiohttp.ClientSession, gl: GitLabAPI, pipeline_id: int
    ):

        current_app.logger.info(
            "Request to update references from pipeline %d", pipeline_id
        )
        pipeline = db.get_or_404(model.Pipeline, pipeline_id)

        owner, repo, _ = parse_pipeline_url(pipeline.web_url)

        refs = await get_pipeline_references(gl, owner, repo, pipeline.id)

        if request.method == "GET":
            return render_template(
                "update_references.html", pipeline=pipeline, refs=refs
            )
        else:
            eos = WebdavFileSystem(
                "https://cernbox.cern.ch/cernbox/webdav/",
                auth=(config.EOS_USER_NAME, config.EOS_USER_PWD),
            )

            assert eos.exists(
                config.EOS_BASE_PATH
            ), f"{config.EOS_BASE_PATH} does not exist"

            trace = ""
            for job, qtest, version in await get_pipeline_references(
                gl, owner, repo, pipeline_id
            ):
                trace += "\n" + await execute_reference_update(
                    session, gl, eos, owner, repo, job, qtest, version, dry_run=False
                )

            trace += "\nDone"
            print(trace)

            return render_template("update_reference_trace.html", trace=trace)

    @app.route("/update_object_counts/<int:pipeline_id>", methods=["GET", "POST"])
    @with_gitlab
    @with_session
    async def update_object_counts(
        session: aiohttp.ClientSession, gl: GitLabAPI, pipeline_id: int
    ):
        current_app.logger.info(
            "Request to update object counts from pipeline %d", pipeline_id
        )
        pipeline = db.get_or_404(model.Pipeline, pipeline_id)

        owner, repo, _ = parse_pipeline_url(pipeline.web_url)

        diffs = await get_object_counts_diffs(gl, owner, repo, pipeline_id)

        patch = create_patch_from_diffs(
            [diff for _, _, diff in diffs], "Apogee", "apogee@example.com", "Object counts"
        )


        patch_digest = hashlib.sha256(
            "\n".join([diff for _, _, diff in diffs]).encode()
        ).hexdigest()[:16]

        patch_cache_key = f"{config.OBJECT_COUNTS_CACHE_KEY_PREFIX}{patch_digest}.patch"
        cache.set(patch_cache_key, patch, expire=config.OBJECT_COUNTS_CACHE_EXPIRATION)

        combined_diff = combine_diffs([diff for _, _, diff in diffs])
        combined_diff_cache_key = f"{config.OBJECT_COUNTS_CACHE_KEY_PREFIX}{patch_digest}.diff"
        cache.set(combined_diff_cache_key, combined_diff, expire=config.OBJECT_COUNTS_CACHE_EXPIRATION)

        return render_template("update_object_counts.html", pipeline=pipeline, diffs=diffs, patch_digest=patch_digest)

    @app.get("/object_counts/<patch_digest>.<ext>")
    @unprotected
    def object_counts(patch_digest: str, ext: str):
        cache_key = f"{config.OBJECT_COUNTS_CACHE_KEY_PREFIX}{patch_digest}.{ext}"
        content = cache.get(cache_key)
        return Response(content, mimetype='text/plain')

    @app.get("/pipeline/<int:pipeline_id>")
    def pipeline(pipeline_id: int):
        pipeline = (
            db.session.execute(
                db.select(model.Pipeline)
                .where(model.Pipeline.id == pipeline_id)
                .options(
                    sqlalchemy.orm.joinedload(model.Pipeline.jobs),
                    sqlalchemy.orm.raiseload("*"),
                )
            )
            .unique()
            .scalar_one_or_none()
        )
        if pipeline is None:
            abort(404)

        expanded = request.args.get("detail", type=bool, default=False)

        return render_template("pipeline.html", pipeline=pipeline, expanded=expanded)

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
        pipeline.refreshed_at = datetime.utcnow()
        pipeline = db.session.merge(pipeline)

        pipeline.jobs = []

        for job in api_pipeline.jobs:
            db_job = model.Job.from_api(job)
            db_job.pipeline_id = pipeline.id
            db.session.merge(db_job)

        db.session.commit()

        return render_template("pipeline.html", pipeline=pipeline, expanded=True)

    @app.route("/reset_patches", methods=["POST"])
    async def reset_patches():
        db.session.execute(sqlalchemy.delete(model.Patch))
        db.session.commit()
        return "", 200, {"HX-Refresh": "true"}

    @app.route("/sync_patches", methods=["GET", "POST"])
    @with_gitlab
    async def sync_patches(gl: GitLabAPI):
        # find commits
        url = (
            f"/projects/{config.GITLAB_CANARY_PROJECT_ID}/repository/compare"
            + f"?from=main&to={config.GITLAB_CANARY_BRANCH}"
        )
        result = CompareResult(**await gl.getitem(url))

        pairs: list[tuple[CompareResult.Commit, model.Commit, str]] = []

        patterns = [
            r"#(\d+)",
            rf"https://github.com/{config.REPOSITORY}/pull/(\d+)",
        ]
        for commit in result.commits:
            pr_number = None
            for pat in patterns:
                if m := re.search(pat, commit.title):
                    pr_number = int(m.group(1))
                    break
            if pr_number is None:
                continue

            stmnt = db.select(model.Commit).where(
                model.Commit.message.contains(f"(#{pr_number})")
            )
            commits = db.session.execute(stmnt).scalars().all()
            target_commit = None
            for db_commit in commits:
                if db_commit.subject.endswith(f"(#{pr_number})"):
                    target_commit = db_commit
                    break
            if target_commit is None:
                print("Could not find target commit")
                continue

            patch_url = (
                f"{config.GITLAB_URL}/{config.GITLAB_CANARY_PROJECT}/"
                + f"-/commit/{commit.id}.patch"
            )

            pairs.append((commit, target_commit, patch_url))

        if request.method == "GET":
            return render_template("sync_patches.html", pairs=pairs)
        elif request.method == "POST":
            # clear all patches for this
            db.session.execute(sqlalchemy.delete(model.Patch))

            for commit, target_commit, patch_url in pairs:
                patch = model.Patch(
                    url=patch_url, commit_sha=target_commit.sha, order=0
                )
                db.session.add(patch)
                target_commit.patches.append(patch)

            db.session.commit()
            return (
                redirect(url_for("timeline.index")),
                200,
                {"HX-Location": url_for("timeline.index")},
            )

    @app.route("/commit/<sha>")
    async def commit_detail(sha: str) -> str:
        is_latest = request.args.get("latest", type=bool, default=False)
        commit = db.get_or_404(model.Commit, sha)
        pull: model.PullRequest | None = None
        if number := request.args.get("pull"):
            pull = db.get_or_404(model.PullRequest, int(number))

        return render_template(
            "commit_detail.html", commit=commit, pull=pull, is_latest=is_latest
        )

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

        is_toggle = "toggle" in request.args

        pr: model.PullRequest | None = None
        if pull is not None:
            pr = db.session.execute(
                db.select(model.PullRequest).filter_by(number=pull)
            ).scalar_one()
            assert pr is not None
            sha = pr.head_sha

        trigger_commit: model.Commit | None = db.session.execute(
            db.select(model.Commit).where(model.Commit.sha == sha)
        ).scalar_one_or_none()

        if trigger_commit is None:
            flash(f"Commit <code>{html.escape(sha)}</code> not found", "danger")
            code = 200 if "HX-Request" in request.headers else 404
            return render_template("error.html"), code

        num_total_patches = (
            db.session.execute(db.select(func.count(model.Patch.id))).scalars().first()
        ) or 0

        commit_select = (
            db.select(
                model.Commit,
            )
            .order_by(model.Commit.order.desc())
            .filter(model.Commit.patches.any())
            .options(sqlalchemy.orm.joinedload(model.Commit.patches))
        )

        if pull is None:
            commit_select = commit_select.where(
                model.Commit.order <= trigger_commit.order
            )

        commits = db.session.execute(commit_select).unique().scalars().all()
        patches: List[model.Patch] = []

        for commit in commits:
            patches.extend(sorted(commit.patches, key=lambda p: p.order))

        patches.reverse()

        if pr is not None:
            patches += sorted(pr.patches, key=lambda p: p.order)

        patch_contents: list[PatchContent] = await gather_limit(
            5, *[load_patch(p.url) for p in patches]
        )

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

        if len(patches) > 0:
            variables["PATCH_URLS"] = ",".join(p.url for p in patches)

        # Have patches, so no canary
        if num_total_patches > 0:
            variables["NO_CANARY"] = "1"

        if request.method == "GET":
            return render_template(
                "run_pipeline.html" if not is_toggle else "run_pipeline_inner.html",
                commit=trigger_commit,
                patches=patches,
                patch_contents=patch_contents,
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

            try:
                db_pipeline = model.Pipeline.from_api(pipeline)
                db_pipeline.commit = trigger_commit
                db_pipeline.refreshed_at = datetime.utcnow()
                db.session.add(db_pipeline)
                db.session.commit()
            except sqlalchemy.exc.IntegrityError:
                # This can happen because we'll concurrently get webhooks
                # for the same pipeline
                # It should result in the same DB state
                pass

            flash(f"Pipeline started: #{pipeline.id}", "success")
            return (
                "",
                200,
                {
                    "HX-Location": url_for(
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
            return "", 200, {"HX-Location": url_for("index")}
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

    @app.route("/webhook/github", methods=["POST"])
    @unprotected
    def github_webhook():
        body = request.json
        event = request.headers.get("X-GitHub-Event")

        if body is None or event is None:
            return "ok"

        if event == "push":
            handle_push.delay(body)
        elif event == "pull_request":
            handle_pull_request.delay(body)

        return "ok"

    return app
