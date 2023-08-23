from collections.abc import Iterable
from typing import Dict
from uuid import UUID
from pydantic import Field

from output_checker.model import CommitHash
from output_checker.model.github import Commit
from output_checker.model.gitlab import Pipeline

IdType = UUID | CommitHash


#  class Release(BaseModel):
#  id: UUID = Field(default_factory=uuid.uuid4)

#  class ReferenceUpdate(BaseModel):
#  id: UUID = Field(default_factory=uuid.uuid4)


class ExtendedCommit(Commit):
    pipelines: set[int] = Field(default_factory=set)

    def sorted_pipelines(
        self, all_pipelines: Dict[int, Pipeline]
    ) -> Iterable[Pipeline]:
        pipelines = (all_pipelines[pipeline_id] for pipeline_id in self.pipelines)
        return sorted(pipelines, key=lambda p: p.created_at, reverse=True)
