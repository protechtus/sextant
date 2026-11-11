# Sextant -- self-hosted AI memory + orchestration.
# One image, three roles: the compose file runs it as mcp / web / sync.
#   docker build -t sextant .
#   docker compose up -d        (see docker-compose.yml + docs/HOWTO-docker.md)

FROM python:3.11-slim

RUN useradd --create-home --uid 1000 sextant
WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir ".[telegram,summarizer]"

# Corpus + derived state live on ONE mounted volume; nothing else persists.
ENV ATLAS_WORKSPACE_ROOT=/data \
    ATLAS_MEMORY_ROOT=/data/memory \
    ATLAS_STATE_DIR=/data/state \
    PYTHONUNBUFFERED=1
# The runtime user must own the volume mount point or the first write fails (Linode
# packaging lane 2026-11-09). A pre-existing host bind-mount keeps its own ownership.
RUN mkdir -p /data && chown sextant:sextant /data
VOLUME /data

USER sextant
EXPOSE 8765 8766 8767 8768

# Liveness against whichever role runs; HEALTH_PORT/HEALTH_PATH set per service in compose
# (every role answers /health; the sync role also serves /sync/v1/health).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD \
    python -c "import os,urllib.request;port=os.environ.get('HEALTH_PORT','8765');path=os.environ.get('HEALTH_PATH','/health');urllib.request.urlopen(f'http://127.0.0.1:{port}{path}',timeout=4)" || exit 1

CMD ["sextant-mcp"]
