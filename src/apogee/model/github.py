from datetime import datetime
import re
import dataclasses

from pydantic import BaseModel, AwareDatetime

from apogee.model import CommitHash, URL


class UserResponse(BaseModel):
    login: str
    id: int
    url: str
    html_url: URL
    avatar_url: URL


@dataclasses.dataclass
class User:
    login: str
    id: int
    url: str
    html_url: URL
    avatar_url: URL


class CommitAuthor(BaseModel):
    name: str
    email: str
    date: datetime


class Commit(BaseModel):
    class Commit(BaseModel):
        message: str
        url: URL
        author: CommitAuthor
        committer: CommitAuthor

    sha: CommitHash
    url: URL
    html_url: URL

    author: User | None
    committer: User | None

    commit: Commit

    @property
    def pull_request(self) -> int | None:
        m = re.search(r"#(\d+)", self.commit.message)
        if m is None:
            return None
        else:
            return int(m.group(1))


class PRRef(BaseModel):
    label: str
    ref: str
    sha: CommitHash
    user: User
    repo: str


class Repository(BaseModel):
    id: int
    name: str
    full_name: str
    owner: User
    url: URL
    html_url: URL
    clone_url: URL


class PullRequest(BaseModel):
    class Source(BaseModel):
        label: str
        ref: str
        sha: CommitHash
        user: User
        repo: Repository

    url: URL
    html_url: URL
    user: User
    number: int
    state: str
    title: str
    body: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    closed_at: AwareDatetime | None
    merged_at: AwareDatetime | None
    merge_commit_sha: CommitHash | None
    head: Source
    base: Source

    mergeable: bool | None = None


class CompareResponse(BaseModel):
    url: str
    total_commits: int
    status: str
    ahead_by: int
    behind_by: int

    commits: list[Commit]
