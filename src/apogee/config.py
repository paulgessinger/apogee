from pathlib import Path
import os

MAX_COMMITS = 100
REPOSITORY = "acts-project/acts"

DB_PATH = Path.cwd() / "shelve"

GITLAB_URL = "https://gitlab.cern.ch"
GITLAB_PROJECT_ID = 153873
GITLAB_PIPELINES_LIMIT = 200

GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
GITLAB_TRIGGER_TOKEN = os.environ["GITLAB_TRIGGER_TOKEN"]
