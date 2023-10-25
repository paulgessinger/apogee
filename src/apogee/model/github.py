from datetime import datetime
import re

from pydantic import BaseModel

from apogee.model import CommitHash, URL


class User(BaseModel):
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
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    merged_at: datetime | None
    merge_commit_sha: CommitHash | None
    head: Source
    base: Source

    mergeable: bool | None = None
