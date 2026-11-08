FROM python:3.14-slim

# The data folder is normally a bind mount of a host directory, so the user inside the container
# should have the same ids as the host user:
#   docker build --build-arg PUID=$(id -u) --build-arg PGID=$(id -g) .
ARG PUID=1000
ARG PGID=1000

# git is not a dependency of the app itself, but the dreaming job commits the wiki with it and the
# graph reads the commit history, so both services need it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN groupadd --gid "${PGID}" dreamscape \
    && useradd --uid "${PUID}" --gid "${PGID}" --create-home --shell /usr/bin/sh dreamscape

# A bind-mounted volume belongs to the host user, so git needs to be told that the wiki repositories
# inside /app/data are safe to work on. The commit identity comes from dream.py itself.
RUN git config --global --add safe.directory '*'

USER dreamscape

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python scripts/healthcheck.py

# Always binds 0.0.0.0, whatever HOST says in .env: inside a container that is the only way the
# port is reachable from outside.
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
