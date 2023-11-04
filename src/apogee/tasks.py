from datetime import datetime
import os
from typing import Any, Dict

from celery import Celery, Task, shared_task
from celery.utils.log import get_task_logger
from flask import Flask

from apogee import config
from apogee.model.db import db
from apogee.model import db as model
from apogee.model.gitlab import Job, Pipeline


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
    celery_app.set_default()
    app.extensions["celery"] = celery_app
    return celery_app


def proc_datetime(s: str) -> str | None:
    if s is None:
        return None
    date, time, tz = s.split(" ")
    return f"{date}T{time}{tz}"


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
        updated_at=datetime.now(),
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
    db_pipeline.refreshed_at = datetime.now()
    db_pipeline = db.session.merge(db_pipeline)
    db_pipeline.jobs = []

    for job in api_pipeline.jobs:
        db_job = model.Job.from_api(job)
        db_job.pipeline_id = db_pipeline.id
        db.session.merge(db_job)

    db.session.commit()
