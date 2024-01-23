from datetime import datetime, timezone
import logging
import os
from typing import Any, Dict
import aiohttp

from celery import Celery, Task, shared_task
from celery.utils.log import get_task_logger
from flask import Flask

from apogee import config
from apogee.github import get_installation_github, update_pull_request
from apogee.model.db import db
from apogee.model import db as model
from apogee.model.github import Commit, CompareResponse, PullRequest
from apogee.model.gitlab import Job, Pipeline
from apogee.util import coroutine
from apogee.github import fetch_commits


logger = get_task_logger(__name__)


def celery_init_app(app: Flask) -> Celery:
    class FlaskTask(Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return super().__call__(*args, **kwargs)

    celery_app = Celery(
        app.import_name,
        task_cls=FlaskTask,
        broker=os.environ["CELERY_BROKER_URL"],
        backend=os.environ["CELERY_RESULT_BACKEND"],
    )
    celery_app.conf.broker_connection_retry_on_startup = True

    celery_app.conf.task_always_eager = (
        os.environ.get("CELERY_TASK_ALWAYS_EAGER", "0") == "1"
    )
    celery_app.conf.task_eager_propagates = celery_app.conf.task_always_eager

    if app.debug:
        logger.setLevel(logging.DEBUG)

    celery_app.set_default()
    app.extensions["celery"] = celery_app
    return celery_app


def proc_datetime(s: str) -> datetime | None:
    if s is None:
        return None
    d = datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
    return d.astimezone(tz=timezone.utc)


@shared_task(ignore_result=True)
def handle_pipeline_webhook(payload: Dict[str, Any]) -> None:
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
        updated_at=datetime.now(tz=timezone.utc),
        web_url=f"{config.GITLAB_URL}/{config.GITLAB_PROJECT}/-/pipelines/{data['id']}",
        variables={v["key"]: v["value"] for v in data["variables"]},
    )

    logger.info(
        "Updating pipeline %d with %d jobs", api_pipeline.id, len(payload["builds"])
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
        logger.info(
            "Ignoring pipeline %d because no `SOURCE_SHA` is set", api_pipeline.id
        )
        return

    logger.info("%s", api_pipeline.variables["SOURCE_SHA"])

    if (
        db.session.execute(
            db.select(model.Commit).filter_by(sha=api_pipeline.variables["SOURCE_SHA"])
        ).scalar_one_or_none()
        is None
    ):
        # ignore these, we don't care about these commits
        logger.info("Ignoring commit %s", api_pipeline.variables["SOURCE_SHA"])
        return

    db_pipeline = model.Pipeline.from_api(api_pipeline)
    db_pipeline.refreshed_at = datetime.utcnow()
    db_pipeline = db.session.merge(db_pipeline)
    db_pipeline.jobs = []

    for job in api_pipeline.jobs:
        db_job = model.Job.from_api(job)
        db_job.pipeline_id = db_pipeline.id
        db.session.merge(db_job)

    db.session.commit()


@shared_task(ignore_result=True)
def handle_job_webhook(payload: Dict[str, Any]) -> None:
    # we need to already know about this job's pipeline, otherwise it's pointless
    job = Job(
        id=payload["build_id"],
        status=payload["build_status"],
        stage=payload["build_stage"],
        name=payload["build_name"],
        ref=payload["ref"],
        allow_failure=payload["build_allow_failure"],
        created_at=proc_datetime(payload["build_created_at"]),
        started_at=proc_datetime(payload["build_started_at"]),
        finished_at=proc_datetime(payload["build_finished_at"]),
        web_url=f"{config.GITLAB_URL}/{config.GITLAB_PROJECT}/-/jobs/{payload['build_id']}",
        failure_reason=payload["build_failure_reason"],
    )

    pipeline_id = payload["pipeline_id"]
    logger.info("Handling job %s for pipeline %s", job.id, pipeline_id)

    if pipeline_id is None:
        logger.info("Cannot save job without pipeline id")
        return

    if pipeline := db.session.execute(
        db.select(model.Pipeline).filter_by(id=pipeline_id)
    ).scalar_one_or_none():
        pipeline.refreshed_at = datetime.utcnow()
    else:
        # ignore these, we don't care about these jobs
        logger.info("Ignoring job %s", job.id)
        return

    db_job = model.Job.from_api(job)
    db.session.merge(db_job)

    db.session.commit()


@shared_task(ignore_result=True)
@coroutine
async def handle_push(payload: Dict[str, Any]) -> None:
    installation_id = payload["installation"]["id"]
    head_commit_sha = payload["head_commit"]["id"]
    repo = payload["repository"]["full_name"]

    logger.info("Handling push %s for repo %s", head_commit_sha, repo)

    async with aiohttp.ClientSession() as session:
        gh = await get_installation_github(session, installation_id)

        await fetch_commits(gh)


@shared_task(ignore_result=True)
@coroutine
async def handle_pull_request(payload: Dict[str, Any]) -> None:
    installation_id = payload["installation"]["id"]
    pr = PullRequest(**payload["pull_request"])

    pr_compare: CompareResponse | None = None
    if payload["action"] in ("opened", "synchronize"):
        async with aiohttp.ClientSession() as session:
            gh = await get_installation_github(session, installation_id)

            pr_compare = CompareResponse(
                **await gh.getitem(
                    f"/repos/{config.REPOSITORY}/compare/{pr.base.sha}...{pr.head.sha}"
                )
            )

    update_pull_request(pr, pr_compare.commits if pr_compare else None)

    db.session.commit()
