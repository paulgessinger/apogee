from datetime import datetime
import re

from pydantic import BaseModel

from output_checker.model import CommitHash


class User(BaseModel):
    login: str
    id: int
    url: str
    html_url: str


class CommitAuthor(BaseModel):
    name: str
    email: str
    date: datetime


class Commit(BaseModel):
    class Commit(BaseModel):
        message: str
        url: str
        author: CommitAuthor
        committer: CommitAuthor

    sha: CommitHash
    url: str
    html_url: str

    author: User
    committer: User

    commit: Commit

    @property
    def pull_request(self) -> int | None:
        m = re.search(r"#(\d+)", self.commit.message)
        if m is None:
            return None
        else:
            return int(m.group(1))
