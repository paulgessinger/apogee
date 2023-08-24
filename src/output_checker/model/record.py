from collections.abc import Iterable
from typing import Dict
from uuid import UUID, uuid4
from pydantic import BaseModel, Field

from output_checker.model import CommitHash
from output_checker.model.github import Commit
from output_checker.model.gitlab import Pipeline

IdType = UUID | CommitHash


#  class Release(BaseModel):
#  id: UUID = Field(default_factory=uuid.uuid4)

#  class ReferenceUpdate(BaseModel):
#  id: UUID = Field(default_factory=uuid.uuid4)


class Patch(BaseModel):
    id: UUID | None = Field(default_factory=uuid4)
    url: str


class ExtendedCommit(Commit):
    pipelines: set[int] = Field(default_factory=set)

    revert: bool = False

    patches: list[Patch] = Field(default_factory=list)

    notes: str = ""

    def sorted_pipelines(
        self, all_pipelines: Dict[int, Pipeline]
    ) -> Iterable[Pipeline]:
        pipelines = (all_pipelines[pipeline_id] for pipeline_id in self.pipelines)
        return sorted(pipelines, key=lambda p: p.created_at, reverse=True)
