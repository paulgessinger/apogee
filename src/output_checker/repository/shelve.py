from collections.abc import Generator, Iterable
import shelve
import contextlib
from pathlib import Path
from typing import Dict, List

import filelock
from output_checker.model import CommitHash
from output_checker.model.gitlab import Pipeline
from output_checker.model.record import ExtendedCommit

from output_checker.repository import DuplicationError, Repository


class ShelveRepository(Repository):
    path: Path
    lock: filelock.FileLock

    def __init__(self, path: Path):
        self.path = path
        self.lock = filelock.FileLock(str(path.parent / (path.stem + ".lock")))

    @contextlib.contextmanager
    def _shelf(self) -> Generator[shelve.Shelf, None, None]:
        with self.lock:
            with shelve.open(str(self.path)) as shelf:
                yield shelf

    def close(self):
        pass

    def commit_sequence(self) -> Generator[ExtendedCommit, None, None]:
        with self._shelf() as shelf:
            commits: list[CommitHash] = shelf.get("commits_seq", [])
        for commit in commits:
            yield self.get_commit(commit)

    def save_commit_sequence(self, commits: Iterable[ExtendedCommit]):
        with self._shelf() as shelf:
            shelf["commits_seq"] = list(commits)

    def add_commit(
        self, commit: ExtendedCommit, update_on_conflict: bool = False
    ) -> None:
        try:
            with self._shelf() as shelf:
                commits = shelf.get("commits", {})
                if commit.sha in commits:
                    raise DuplicationError(f"Commit {commit.sha} already exists")
                commits[commit.sha] = commit
                shelf["commits"] = commits
        except DuplicationError:
            if update_on_conflict:
                self.update_commit(commit)
            else:
                raise

    def update_commit(self, commit: ExtendedCommit):
        with self._shelf() as shelf:
            commits = shelf.get("commits", {})
            if commit.sha not in commits:
                raise ValueError(f"Commit {commit.sha} does not exist")
            commits[commit.sha] = commit
            shelf["commits"] = commits

    def get_commit(self, sha: CommitHash) -> ExtendedCommit:
        with self._shelf() as shelf:
            commits = shelf.get("commits", {})
            if sha not in commits:
                raise ValueError(f"Commit {sha} does not exist")
            return commits[sha]

    def commits(self) -> Dict[CommitHash, ExtendedCommit]:
        with self._shelf() as shelf:
            return shelf.get("commits", {})

    def add_pipeline(self, pipeline: Pipeline, update_on_conflict: bool = False):
        try:
            with self._shelf() as shelf:
                pipelines = shelf.get("pipelines", {})
                if pipeline.id in pipelines:
                    raise DuplicationError(f"Pipeline #{pipeline.id} already exists")
                pipelines[pipeline.id] = pipeline
                shelf["pipelines"] = pipelines
        except DuplicationError:
            if update_on_conflict:
                self.update_pipeline(pipeline)
            else:
                raise

    def update_pipeline(self, pipeline: Pipeline):
        with self._shelf() as shelf:
            pipelines = shelf.get("pipelines", {})
            if pipeline.id not in pipelines:
                raise ValueError(f"Pipeline #{pipeline.id} does not exist")
            pipelines[pipeline.id] = pipeline
            shelf["pipelines"] = pipelines

    def get_pipeline(self, pipeline_id: int) -> Pipeline:
        with self._shelf() as shelf:
            pipelines = shelf.get("pipelines", {})
            if pipeline_id not in pipelines:
                raise ValueError(f"Pipeline #{pipeline_id} does not exist")
            return pipelines[pipeline_id]

    def pipelines(self) -> Dict[int, Pipeline]:
        with self._shelf() as shelf:
            return shelf.get("pipelines", {})
