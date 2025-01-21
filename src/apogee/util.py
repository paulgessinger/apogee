from datetime import datetime
import tempfile
import shutil
import functools
from flask import current_app
from fsspec.implementations.zip import ZipFileSystem
import re
import os
import hashlib

import asyncio

import aiohttp
from gidgetlab.abc import GitLabAPI
import fsspec.spec
from apogee import config

from apogee.model.gitlab import Job, Pipeline as ApiPipeline


async def gather_limit(n, *coros):
    semaphore = asyncio.Semaphore(n)

    async def sem_coro(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(sem_coro(c) for c in coros))


def coroutine(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def parse_pipeline_url(url: str) -> tuple[str, str, int]:
    m = re.match(r"https://gitlab.cern.ch/([^/]+)/([^/]+)/-/pipelines/(\d+)/?", url)
    assert m is not None, "Pipeline url could not be parsed"
    owner = m.group(1)
    repo = m.group(2)
    pipeline_id = int(m.group(3))
    return owner, repo, pipeline_id


async def get_pipeline_references(
    gl: GitLabAPI,
    owner: str,
    repo: str,
    pipeline_id: int,
) -> list[tuple[Job, str, str]]:
    pipeline_data = await gl.getitem(
        f"/projects/{owner}%2F{repo}/pipelines/{pipeline_id}"
    )

    pipeline = ApiPipeline(**pipeline_data)
    await pipeline.fetch(gl)

    failed_jobs = [j for j in pipeline.jobs if j.status == "failed"]

    traces = await gather_limit(
        10,
        *(
            gl.getitem(f"/projects/{owner}%2F{repo}/jobs/{j.id}/trace")
            for j in failed_jobs
        ),
    )

    results = []

    for job, trace in zip(failed_jobs, traces):
        print("Job", f"#{job.id} {job.name}", "failed")
        m = re.search(
            rf"Checking for reference override at http.+/q(\d+)/v(\d+)/myAOD.pool.root",
            trace,
        )
        if m is None:
            print("Could not find reference override in trace, skipping this job")
            continue

        qtest, version = m.groups()

        print(f"Job was running q{qtest} and version v{version} of references")

        results.append((job, qtest, version))

    return results


async def get_object_counts_diffs(
    gl: GitLabAPI,
    owner: str,
    repo: str,
    pipeline_id: int,
) -> list[tuple[Job, str]]:
    pipeline_data = await gl.getitem(
        f"/projects/{owner}%2F{repo}/pipelines/{pipeline_id}"
    )

    pipeline = ApiPipeline(**pipeline_data)
    await pipeline.fetch(gl)

    failed_jobs = [j for j in pipeline.jobs if j.status == "failed"]

    traces = await gather_limit(
        10,
        *(
            gl.getitem(f"/projects/{owner}%2F{repo}/jobs/{j.id}/trace")
            for j in failed_jobs
        ),
    )

    results = []

    for job, trace in zip(failed_jobs, traces):
        print("Job", f"#{job.id} {job.name}", "failed")
        
        # Match from "Comparing against reference" to "FAILURE"
        m = re.search(
            r"(?ms)"  # Multiline and dot matches newline
            r"Comparing against reference\n"  # Start marker
            r"(--- .+?\.ref[^\n]*\n"  # Start capturing: Reference file ending in .ref
            r"\+\+\+ [^\n]+\n"  # New file
            r".*?)"  # Any content in between (non-greedy)
            r" -- FAILURE",  # End marker
            trace
        )
        
        if m is None:
            print("Could not find object counts diff in trace, skipping this job")
            continue

        diff = m.group(1)  # Get just the captured diff content
        results.append((job, diff))

    return results


def create_patch_from_diffs(
    diffs: list[str],  # list of diff contents
    author_name: str,
    author_email: str,
    subject: str,
) -> str:
    # Generate a fake but consistent commit hash from the content
    content_hash = hashlib.sha1(
        "\n".join(diffs).encode()
    ).hexdigest()
    
    now = datetime.now()
    
    # Start with the patch header
    lines = [
        f"From {content_hash} Mon Sep 17 00:00:00 2001",
        f"From: {author_name} <{author_email}>",
        f"Date: {now.strftime('%a, %d %b %Y %H:%M:%S %z')}",
        f"Subject: [PATCH] {subject}",
        "",
        "---"
    ]
    
    # Add the file summary
    file_changes = []
    total_plus = 0
    total_minus = 0
    
    processed_diffs = []
    for diff in diffs:
        # Extract filename from the diff header
        m = re.search(r"^--- (.+?)\.ref[\t\s]", diff, re.MULTILINE)
        if not m:
            continue
            
        input_path = m.group(1) + ".ref"
        
        # Validate and transform path
        if "ActsConfig/" not in input_path:
            print(f"Skipping diff for {input_path} - expected path containing ActsConfig/")
            continue
            
        # Transform path/to/ActsConfig/file.ref -> Tracking/Acts/ActsConfig/share/file.ref
        filename = "Tracking/Acts/ActsConfig/share/" + os.path.basename(input_path)
        
        plus = diff.count("\n+")
        minus = diff.count("\n-")
        file_changes.append(f" {filename:<50} | {plus + minus:>3} {plus:>3}+ {minus:>3}-")
        total_plus += plus
        total_minus += minus
        
        # Replace the paths in the diff content
        new_diff = diff.replace(
            f"--- {input_path}",
            f"--- a/{filename}"
        )
        new_diff = re.sub(
            r"\+\+\+ .+?(?=\t|\s|$)",  # Match +++ and everything until tab, space or end of line
            f"+++ b/{filename}",
            new_diff
        )
        
        processed_diffs.append((filename, new_diff))
    
    lines.extend(file_changes)
    lines.append(f" {len(processed_diffs)} files changed, {total_plus} insertions(+), {total_minus} deletions(-)")
    lines.append("")
    
    # Add each diff with git diff header
    for filename, diff_content in processed_diffs:
        lines.extend([
            f"diff --git a/{filename} b/{filename}",
            "index 0000000..0000000",  # Fake index
            diff_content.rstrip(),
            ""  # Empty line between diffs
        ])
    
    # Add git patch footer
    lines.extend([
        "-- ",
        "Apogee"
    ])
    
    return "\n".join(lines)


async def execute_reference_update(
    session: aiohttp.ClientSession,
    gl: GitLabAPI,
    eos: fsspec.spec.AbstractFileSystem,
    owner: str,
    repo: str,
    job: Job,
    qtest: str,
    version: str,
    dry_run: bool,
):
    eos_q_dir = f"{config.EOS_BASE_PATH}/q{qtest}"

    trace = []

    eos_version_dir = f"{eos_q_dir}/v{version}"
    if eos.exists(eos_version_dir):
        dest = f"{eos_version_dir}.pre_{datetime.now():%Y-%m-%dT%H-%M-%S}"
        trace += [f"Override dir {eos_version_dir} already exists, moving to {dest}"]
        current_app.logger.info("%s", trace[-1])
        if not dry_run:
            eos.mv(
                eos_version_dir,
                dest,
                recursive=True,
            )
    if not dry_run:
        eos.mkdir(eos_version_dir)
    url = f"{gl.api_url}/projects/{owner}%2F{repo}/jobs/{job.id}/artifacts"
    r = await session.get(url)
    with tempfile.TemporaryFile() as fh:
        trace += [f"Downloading artifact {url}"]
        current_app.logger.info("%s", trace[-1])
        async for data, _ in r.content.iter_chunks():
            fh.write(data)
        current_app.logger.info("Downloaded %d bytes", fh.tell())
        fh.seek(0)
        zipfs = ZipFileSystem(fh)
        run_path = f"run/run_q{qtest}"
        assert zipfs.exists(run_path), f"Could not find {run_path} in zip file"

        current_app.logger.info("Have zip file system")

        for name in ("AOD", "ESD"):
            full_name = f"{run_path}/my{name}.pool.root"
            full_target_name = f"{eos_version_dir}/my{name}.pool.root"

            assert zipfs.exists(full_name), f"Could not find {full_name} in zip file"
            if not dry_run:
                assert not eos.exists(
                    full_target_name
                ), f"{full_target_name} already exists"

            trace += [f"Copying {full_name} to {full_target_name}"]
            current_app.logger.info("%s", trace[-1])

            if not dry_run:
                with zipfs.open(full_name, "rb") as src, eos.open(
                    full_target_name, "wb"
                ) as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024 * 10)
            current_app.logger.info("Copy complete")

    return "\n".join(trace)
