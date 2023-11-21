import asyncio
import re
import aiohttp

from gidgetlab.abc import GitLabAPI
import click
from sshfs import SSHFileSystem
from webdav4.fsspec import WebdavFileSystem
from apogee import config

from apogee.model.db import db
from apogee.model.gitlab import Pipeline
from apogee.util import (
    execute_reference_update,
    get_pipeline_references,
    parse_pipeline_url,
)
from apogee.web.util import with_gitlab


@with_gitlab
async def update_references(
    session: aiohttp.ClientSession, gl: GitLabAPI, pipeline_url: str, dry_run: bool
):
    owner, repo, pipeline_id = parse_pipeline_url(pipeline_url)

    eos = WebdavFileSystem(
        "https://cernbox.cern.ch/cernbox/webdav/",
        auth=(config.EOS_USER_NAME, config.EOS_USER_PWD),
    )

    assert eos.exists(config.EOS_BASE_PATH), f"{config.EOS_BASE_PATH} does not exist"

    for job, qtest, version in await get_pipeline_references(
        gl, owner, repo, pipeline_id
    ):
        trace = await execute_reference_update(
            session, gl, eos, owner, repo, job, qtest, version, dry_run
        )

        print(trace)


def add_cli(app):
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

    @app.cli.command("update-references")
    @click.argument("pipeline_url")
    @click.option("--dry-run", is_flag=True)
    def _update_references(pipeline_url: str, dry_run: bool):
        asyncio.run(update_references(pipeline_url=pipeline_url, dry_run=dry_run))
