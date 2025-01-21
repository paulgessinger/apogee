FROM python:3.11-slim-bookworm

RUN pip install --no-cache-dir uv

RUN mkdir /app
WORKDIR /app

COPY . /app

ENV PATH=/home/$USER/.local/bin:$PATH

# needed so we can run the migrations
ENV FLASK_APP=apogee.web:create_app
ENV TZ=Europe/Zurich

RUN uv sync --frozen --no-editable
ENV PATH="/app/.venv/bin:$PATH"

RUN chgrp -R 0 /app && \
    chmod -R g=u /app

CMD ["bash", "./start.sh"]
