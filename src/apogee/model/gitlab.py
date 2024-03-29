from datetime import datetime
from typing import List
from gidgetlab.abc import GitLabAPI
from pydantic import AwareDatetime, BaseModel, Field

from apogee.model import CommitHash


class Job(BaseModel):
    id: int
    status: str
    stage: str
    name: str
    ref: str
    allow_failure: bool
    created_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    web_url: str
    failure_reason: str | None = None


class Pipeline(BaseModel):
    id: int
    iid: int
    project_id: int
    sha: CommitHash
    ref: str
    status: str
    source: str
    created_at: datetime
    updated_at: datetime
    web_url: str

    jobs: list[Job] = Field(default_factory=list)

    variables: dict[str, str] = Field(default_factory=dict)

    last_refreshed_at: datetime = Field(default_factory=datetime.now)

    async def fetch(self, gl: GitLabAPI) -> None:
        self.jobs = [
            Job(**j)
            async for j in gl.getiter(
                f"/projects/{self.project_id}/pipelines/{self.id}/jobs"
            )
        ]

        variables = await gl.getitem(
            f"/projects/{self.project_id}/pipelines/{self.id}/variables"
        )

        self.variables = {v["key"]: v["value"] for v in variables}

    @property
    def last_refreshed_delta(self):
        return datetime.now() - self.last_refreshed_at


class CompareResult(BaseModel):
    class Commit(BaseModel):
        id: str
        short_id: str
        title: str
        message: str

    commits: List[Commit]
