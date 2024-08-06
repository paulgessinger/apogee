#!/bin/bash

set -e

echo "Starting Apogee"
date

flask db upgrade

celery_workers=${CELERY_WORKERS:-4}

celery -A make_celery worker --concurrency ${CELERY_WORKERS} --loglevel=info &
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

gunicorn "apogee.web:create_app()" --workers $workers --bind "0.0.0.0:$port"
