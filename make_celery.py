from celery import Celery
from apogee.web import create_app

flask_app = create_app()
celery_app: Celery = flask_app.extensions["celery"]
