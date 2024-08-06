#!/bin/bash

set -e

function run() {-
│   set -x
│   "$@"-
│   { set +x;  } 2> /dev/null
}

echo "Starting Apogee"
date

run flask db upgrade

CELERY_WORKERS=${CELERY_WORKERS:-4}

run celery -A make_celery worker --concurrency ${CELERY_WORKERS} --loglevel=info &
pid=$!

function teardown() {
    echo "Shutting down celery worker"
    kill -TERM $pid
    wait $pid
    echo "Celery worker stopped"
}

trap teardown EXIT

workers=${GUNICORN_WORKERS:-4}
port=${PORT:-5001}

run gunicorn "apogee.web:create_app()" --workers $workers --bind "0.0.0.0:$port"
