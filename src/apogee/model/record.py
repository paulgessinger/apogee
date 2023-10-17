from collections.abc import Iterable
from typing import Dict
from uuid import UUID, uuid4
from pydantic import BaseModel, Field

from apogee.model import CommitHash
from apogee.model.github import Commit
from apogee.model.gitlab import Pipeline

IdType = UUID | CommitHash


#  class Release(BaseModel):
#  id: UUID = Field(default_factory=uuid.uuid4)

#  class ReferenceUpdate(BaseModel):
#  id: UUID = Field(default_factory=uuid.uuid4)


class Patch(BaseModel):
    id: UUID | None = Field(default_factory=uuid4)
    url: str
