FROM python:3.11-slim-bookworm as builder

RUN pip install poetry

COPY pyproject.toml poetry.lock ./
RUN poetry export -o requirements.txt

FROM python:3.11-slim-bookworm

WORKDIR /app

COPY --from=builder /requirements.txt requirements.txt

RUN pip install --no-cache-dir -r requirements.txt
COPY src src
COPY migrations migrations
COPY pyproject.toml .
RUN pip install --no-cache-dir .

ENV FLASK_APP=apogee.web:create_app
ENV FLASK_RUN_PORT=5001
ENV FLASK_RUN_HOST=0.0.0.0
ENV TZ=Europe/Zurich

COPY start.sh .

RUN chgrp -R 0 /app && \
    chmod -R g=u /app

CMD ["bash", "./start.sh"]
