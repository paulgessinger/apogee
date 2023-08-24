from abc import ABC, abstractmethod
from collections.abc import Iterable, Generator
from typing import Dict, List
from output_checker.model import CommitHash

from output_checker.model.gitlab import Pipeline

from output_checker.model.record import ExtendedCommit


class Repository(ABC):
    @abstractmethod
    def close(self):
        ...

    @abstractmethod
    def commit_sequence(self) -> Generator[ExtendedCommit, None, None]:
        ...

    @abstractmethod
    def save_commit_sequence(self, commit: Iterable[ExtendedCommit]) -> None:
        ...

    @abstractmethod
    def add_commit(
        self, commit: ExtendedCommit, update_on_conflict: bool = False
    ) -> None:
        ...

    @abstractmethod
    def update_commit(self, commit: ExtendedCommit) -> None:
        ...

    @abstractmethod
    def get_commit(self, sha: CommitHash) -> ExtendedCommit:
        ...

    @abstractmethod
    def commits(self) -> Dict[CommitHash, ExtendedCommit]:
        ...

    @abstractmethod
    def add_pipeline(
        self, pipeline: Pipeline, update_on_conflict: bool = False
    ) -> None:
        ...

    @abstractmethod
    def update_pipeline(self, pipeline: Pipeline) -> None:
        ...

    @abstractmethod
    def get_pipeline(self, pipeline_id: int) -> Pipeline:
        ...

    @abstractmethod
    def pipelines(self) -> Dict[int, Pipeline]:
        ...

    def toggle_revert(self, sha: CommitHash) -> bool:
        commit = self.get_commit(sha)
        if hasattr(commit, "revert"):
            commit.revert = not commit.revert
        else:
            commit.revert = True
        self.update_commit(commit)
        return commit.revert

    def reset_reverts(self):
        for commit in self.commits().values():
            commit.revert = False
            self.update_commit(commit)


class DuplicationError(Exception):
    pass
