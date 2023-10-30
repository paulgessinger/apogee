import asyncio
from datetime import datetime
import os
import re
import tempfile
import aiohttp
import shutil

from gidgetlab.abc import GitLabAPI
import click
from sshfs import SSHFileSystem
from fsspec.implementations.zip import ZipFileSystem

from apogee.model.db import db
from apogee.model.gitlab import Pipeline
from apogee.util import gather_limit
from apogee.web.util import with_gitlab, with_session


@with_gitlab
async def update_references(
    session: aiohttp.ClientSession, gl: GitLabAPI, pipeline_url: str
):
    eos = SSHFileSystem(
        "lxplus.cern.ch",
        username=os.environ["EOS_USER_NAME"],
        password=os.environ["EOS_USER_PWD"],
    )

    eos_base_path = os.environ["EOS_BASE_PATH"]

    assert eos.exists(eos_base_path), f"{eos_base_path} does not exist"

    m = re.match(
        r"https://gitlab.cern.ch/([^/]+)/([^/]+)/-/pipelines/(\d+)/?", pipeline_url
    )
    assert m is not None, "Pipeline url could not be parsed"
    owner = m.group(1)
    repo = m.group(2)
    pipeline_id = int(m.group(3))
    pipeline_data = await gl.getitem(
        f"/projects/{owner}%2F{repo}/pipelines/{pipeline_id}"
    )

    pipeline = Pipeline(**pipeline_data)
    await pipeline.fetch(gl)

    failed_jobs = [j for j in pipeline.jobs if j.status == "failed"]

    traces = await gather_limit(
        10,
        *(
            gl.getitem(f"/projects/{owner}%2F{repo}/jobs/{j.id}/trace")
            for j in failed_jobs
        ),
    )

    for job, trace in zip(failed_jobs, traces):
        print("Job", f"#{job.id} {job.name}", "failed")
        m = re.search(
            rf"Checking for reference override at {os.environ['OVERRIDE_BASE_URL']}/q(\d+)/v(\d+)/myAOD.pool.root",
            trace,
        )
        assert m is not None, "Could not find reference override in trace"
        qtest, version = m.groups()

        print(f"Job was running q{qtest} and version v{version} of references")

        r = await session.get(
            f"{gl.api_url}/projects/{owner}%2F{repo}/jobs/{job.id}/artifacts"
        )

        eos_q_dir = f"{eos_base_path}/q{qtest}"
        #  print(eos.ls(eos_q_dir))

        eos_version_dir = f"{eos_q_dir}/v{version}"
        if eos.exists(eos_version_dir):
            dest = f"{eos_version_dir}.pre_{datetime.now():%Y-%m-%dT%H-%M-%S}"
            print("Override dir", eos_version_dir, "already exists, moving to", dest)
            eos.mv(
                eos_version_dir,
                dest,
            )
        eos.mkdir(eos_version_dir)

        with tempfile.TemporaryFile() as fh:
            print("Downloading artifact")
            async for data, _ in r.content.iter_chunks():
                fh.write(data)
            fh.seek(0)
            zipfs = ZipFileSystem(fh)
            run_path = f"run/run_q{qtest}"
            assert zipfs.exists(run_path), f"Could not find {run_path} in zip file"

            for name in ("AOD", "ESD"):
                full_name = f"{run_path}/my{name}.pool.root"
                full_target_name = f"{eos_version_dir}/my{name}.pool.root"

                assert zipfs.exists(
                    full_name
                ), f"Could not find {full_name} in zip file"
                assert not eos.exists(
                    full_target_name
                ), f"{full_target_name} already exists"

                print("Copying", full_name, "to", full_target_name)

                with zipfs.open(full_name, "rb") as src, eos.open(
                    full_target_name, "wb"
                ) as dst:
                    shutil.copyfileobj(src, dst)

                print("Done")


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
    def _update_references(pipeline_url: str):
        asyncio.run(update_references(pipeline_url=pipeline_url))
