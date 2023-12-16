import datetime
from typing import Any, Optional

from flask_sqlalchemy import SQLAlchemy
import sqlalchemy.sql.functions as func
from sqlalchemy import (
    Column,
    MetaData,
    String,
    Integer,
    ForeignKey,
    JSON,
    event,
    null,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column, relationship, Session

from apogee.model.github import (
    Commit as ApiCommit,
    User as ApiUser,
    PullRequest as ApiPullRequest,
)
from apogee.model.gitlab import Pipeline as ApiPipeline, Job as ApiJob


db = SQLAlchemy(
    metadata=MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
)


#  @event.listens_for(Engine, "connect")
#  def set_sqlite_pragma(dbapi_connection, connection_record):
#  cursor = dbapi_connection.cursor()
#  cursor.execute("PRAGMA foreign_keys=ON")
#  cursor.close()


class Commit(db.Model):
    sha: Mapped[str] = mapped_column(String(length=40), primary_key=True)

    url: Mapped[str] = mapped_column()
    html_url: Mapped[str] = mapped_column()

    author_id: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id"))
    author: Mapped["GitHubUser"] = relationship(foreign_keys=[author_id])

    committer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id"))
    comitter: Mapped["GitHubUser"] = relationship(foreign_keys=[committer_id])

    commit_author: Mapped[str] = mapped_column()
    commit_committer: Mapped[str] = mapped_column()

    message: Mapped[str] = mapped_column()

    committed_date: Mapped[datetime.datetime] = mapped_column()
    authored_date: Mapped[datetime.datetime] = mapped_column()

    note: Mapped[str] = mapped_column(default="")

    revert: Mapped[bool] = mapped_column(default=False)

    order: Mapped[int] = mapped_column()

    pipelines: Mapped[list["Pipeline"]] = relationship(back_populates="commit")

    patches: Mapped[list["Patch"]] = relationship(cascade="all, delete-orphan")

    @classmethod
    def from_api(cls, commit: ApiCommit) -> "Commit":
        return cls(
            sha=commit.sha,
            url=commit.url,
            html_url=commit.html_url,
            message=commit.commit.message,
            commit_author=commit.commit.author.name,
            commit_committer=commit.commit.committer.name,
            committed_date=commit.commit.committer.date.replace(tzinfo=None),
            authored_date=commit.commit.author.date.replace(tzinfo=None),
        )

    @property
    def subject(self) -> str:
        return self.message.split("\n")[0]

    @property
    def latest_pipeline(self) -> Optional["Pipeline"]:
        session = Session.object_session(self)
        assert session is not None
        # @TODO: Refactor: slow
        return (
            session.execute(
                select(Pipeline)
                .filter_by(source_sha=self.sha)
                .order_by(Pipeline.created_at.desc())
                .limit(1)
            )
            .scalars()
            .one_or_none()
        )


class Patch(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column()

    commit_sha: Mapped[Optional[str]] = mapped_column(
        ForeignKey("commit.sha"), nullable=True
    )
    commit: Mapped[Optional["Commit"]] = relationship(
        foreign_keys=[commit_sha], back_populates="patches"
    )

    pull_request_number: Mapped[Optional[int]] = mapped_column(
        ForeignKey("pull_request.number"), nullable=True
    )
    pull_request: Mapped[Optional["PullRequest"]] = relationship(
        foreign_keys=[pull_request_number], back_populates="patches"
    )

    order: Mapped[int] = mapped_column(nullable=False)


class GitHubUser(db.Model):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(unique=True)
    url: Mapped[str] = mapped_column()
    html_url: Mapped[str] = mapped_column()
    avatar_url: Mapped[str] = mapped_column()

    @classmethod
    def from_api(cls, user: ApiUser) -> "GitHubUser":
        return cls(
            id=user.id,
            login=user.login,
            url=user.url,
            html_url=user.html_url,
            avatar_url=user.avatar_url,
        )


class PullRequest(db.Model):
    number: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column()
    html_url: Mapped[str] = mapped_column()
    state: Mapped[str] = mapped_column()
    title: Mapped[str] = mapped_column()
    body: Mapped[Optional[str]] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column()
    updated_at: Mapped[datetime.datetime] = mapped_column()
    closed_at: Mapped[Optional[datetime.datetime]] = mapped_column()
    merged_at: Mapped[Optional[datetime.datetime]] = mapped_column()
    merge_commit_sha: Mapped[Optional[str]] = mapped_column()

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    user: Mapped["GitHubUser"] = relationship(foreign_keys=[user_id])

    head_label: Mapped[str] = mapped_column()
    head_ref: Mapped[str] = mapped_column()
    head_sha: Mapped[str] = mapped_column()
    head_user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    head_user: Mapped["GitHubUser"] = relationship(foreign_keys=[head_user_id])
    head_repo_full_name: Mapped[str] = mapped_column()
    head_repo_html_url: Mapped[str] = mapped_column()
    head_repo_clone_url: Mapped[str] = mapped_column()

    base_label: Mapped[str] = mapped_column()
    base_ref: Mapped[str] = mapped_column()
    base_sha: Mapped[str] = mapped_column()
    base_user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    base_user: Mapped["GitHubUser"] = relationship(foreign_keys=[base_user_id])
    base_repo_full_name: Mapped[str] = mapped_column()
    base_repo_html_url: Mapped[str] = mapped_column()
    base_repo_clone_url: Mapped[str] = mapped_column()

    commits: Mapped[list["PrCommitAssociation"]] = relationship()

    mergeable: Mapped[bool] = mapped_column(server_default="t")

    patches: Mapped[list["Patch"]] = relationship(cascade="all, delete-orphan")

    @classmethod
    def from_api(cls, pull: ApiPullRequest) -> "PullRequest":
        return cls(
            **pull.dict(
                exclude={
                    "head",
                    "base",
                    "user",
                    "mergeable",
                    "created_at",
                    "updated_at",
                    "closed_at",
                    "merged_at",
                }
            ),
            user_id=pull.user.id,
            created_at=pull.created_at.replace(tzinfo=None),
            updated_at=pull.updated_at.replace(tzinfo=None),
            closed_at=pull.closed_at.replace(tzinfo=None) if pull.closed_at else None,
            merged_at=pull.merged_at.replace(tzinfo=None) if pull.merged_at else None,
            head_label=pull.head.label,
            head_ref=pull.head.ref,
            head_sha=pull.head.sha,
            head_user_id=pull.head.user.id,
            head_repo_full_name=pull.head.repo.full_name,
            head_repo_html_url=pull.head.repo.html_url,
            head_repo_clone_url=pull.head.repo.clone_url,
            base_label=pull.base.label,
            base_ref=pull.base.ref,
            base_sha=pull.base.sha,
            base_user_id=pull.base.user.id,
            base_repo_full_name=pull.base.repo.full_name,
            base_repo_html_url=pull.base.repo.html_url,
            base_repo_clone_url=pull.base.repo.clone_url,
            mergeable=pull.mergeable if pull.mergeable is not None else True,
        )

    @property
    def latest_pipeline(self) -> Optional["Pipeline"]:
        pipeline_select = (
            db.select(
                Commit.sha,
                PrCommitAssociation.order,
                Pipeline,
                func.max(Pipeline.created_at),
            )
            .where(PrCommitAssociation.pull_request_number == self.number)
            .join(PrCommitAssociation.commit)
            .join(Commit.pipelines)
            .group_by(model.Commit.sha, model.Pipeline.id)
            .order_by(PrCommitAssociation.order.desc())
            .limit(1)
        )

        result = db.session.execute(pipeline_select).one_or_none()

        if result is None:
            return None

        _, _, pipeline, _ = result
        return pipeline


class PrCommitAssociation(db.Model):
    pull_request_number: Mapped[int] = mapped_column(
        ForeignKey("pull_request.number"), primary_key=True
    )

    commit_sha: Mapped[str] = mapped_column(ForeignKey("commit.sha"), primary_key=True)
    commit: Mapped["Commit"] = relationship()

    order: Mapped[int] = mapped_column()


class Pipeline(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    iid: Mapped[int] = mapped_column(unique=True)
    project_id: Mapped[int] = mapped_column()
    sha: Mapped[str] = mapped_column()  # infra sha
    source_sha: Mapped[str] = mapped_column(ForeignKey("commit.sha"))
    commit: Mapped["Commit"] = relationship(
        foreign_keys=[source_sha], back_populates="pipelines"
    )
    ref: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column()
    source: Mapped[str] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column()
    updated_at: Mapped[datetime.datetime] = mapped_column()
    web_url: Mapped[str] = mapped_column()

    jobs: Mapped[list["Job"]] = relationship(
        back_populates="pipeline", cascade="all, delete-orphan"
    )

    variables: Mapped[dict[str, str]] = mapped_column(JSON)

    refreshed_at: Mapped[datetime.datetime] = mapped_column()

    @property
    def refreshed_delta(self):
        return datetime.datetime.utcnow() - self.refreshed_at

    @classmethod
    def from_api(cls, pipeline: ApiPipeline) -> "Pipeline":
        return cls(
            id=pipeline.id,
            iid=pipeline.iid,
            project_id=pipeline.project_id,
            sha=pipeline.sha,
            source_sha=pipeline.variables["SOURCE_SHA"],
            ref=pipeline.ref,
            status=pipeline.status,
            source=pipeline.source,
            created_at=pipeline.created_at.replace(tzinfo=None),
            updated_at=pipeline.updated_at.replace(tzinfo=None),
            web_url=pipeline.web_url,
            variables=pipeline.variables,
        )


class Job(db.Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column()
    stage: Mapped[str] = mapped_column()
    name: Mapped[str] = mapped_column()
    ref: Mapped[str] = mapped_column()
    allow_failure: Mapped[bool] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column()
    started_at: Mapped[datetime.datetime | None] = mapped_column()
    finished_at: Mapped[datetime.datetime | None] = mapped_column()
    web_url: Mapped[str] = mapped_column()
    failure_reason: Mapped[str | None] = mapped_column()

    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipeline.id"))
    pipeline: Mapped["Pipeline"] = relationship("Pipeline", back_populates="jobs")

    @classmethod
    def from_api(cls, job: ApiJob) -> "Job":
        return cls(
            id=job.id,
            status=job.status,
            stage=job.stage,
            name=job.name,
            ref=job.ref,
            allow_failure=job.allow_failure,
            created_at=job.created_at.replace(tzinfo=None),
            started_at=job.started_at.replace(tzinfo=None) if job.started_at else None,
            finished_at=job.finished_at.replace(tzinfo=None)
            if job.finished_at
            else None,
            web_url=job.web_url,
            failure_reason=job.failure_reason,
        )


class KeyValue(db.Model):
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
