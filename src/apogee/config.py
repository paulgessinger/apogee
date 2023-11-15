from pathlib import Path
import os

MAX_COMMITS = 100
REPOSITORY = "acts-project/acts"

GITLAB_URL = "https://gitlab.cern.ch"
GITLAB_PROJECT = "acts/acts-athena-ci"
GITLAB_PROJECT_ID = 153873
GITLAB_PIPELINES_WINDOW_DAYS = 4
GITLAB_CONCURRENCY_LIMIT = 50

GITLAB_CANARY_PROJECT_ID = 66770
GITLAB_CANARY_PROJECT = "acts/athena"
GITLAB_CANARY_BRANCH = "canary"

GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
GITLAB_TRIGGER_TOKEN = os.environ["GITLAB_TRIGGER_TOKEN"]

CACHE_DIR = Path(os.environ["CACHE_DIR"])

CERN_AUTH_METADATA_URL = (
    "https://auth.cern.ch/auth/realms/cern/.well-known/openid-configuration"
)
CERN_AUTH_CLIENT_ID = os.environ["CERN_AUTH_CLIENT_ID"]
CERN_AUTH_CLIENT_SECRET = os.environ["CERN_AUTH_CLIENT_SECRET"]

GITHUB_APP_ID = os.environ["GITHUB_APP_ID"]
GITHUB_APP_PRIVATE_KEY = os.environ["GITHUB_APP_PRIVATE_KEY"]

GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]
GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
