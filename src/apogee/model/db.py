import datetime
from typing import Optional

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, String, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship 

from apogee.model.github import Commit as ApiCommit


db = SQLAlchemy()

class Commit(db.Model):
    sha: Mapped[str] = mapped_column(String(length=40), primary_key=True)

    url: Mapped[str] = mapped_column()
    html_url: Mapped[str] = mapped_column()

    author_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    author: Mapped["GitHubUser"] = relationship(foreign_keys=[author_id])

    committer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id"))
    comitter: Mapped["GitHubUser"] = relationship(foreign_keys=[committer_id])

    message: Mapped[str] = mapped_column()

    committed_date: Mapped[datetime.datetime] = mapped_column()
    authored_date: Mapped[datetime.datetime] = mapped_column()

    note: Mapped[str] = mapped_column(default="")


    @classmethod
    def from_api(cls, commit: ApiCommit) -> "Commit":
        return cls(
            sha=commit.sha,
            url = commit.url,
            html_url=commit.html_url,
            message=commit.commit.message,
            committed_date=commit.commit.committer.date,
            authored_date=commit.commit.author.date,
        )


class GitHubUser(db.Model):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(unique=True)