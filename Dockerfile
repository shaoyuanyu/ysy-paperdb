# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PAPERDB_STORAGE_ROOT=/data/files

WORKDIR /app

RUN pip install --no-cache-dir "uv>=0.5,<1" \
    && groupadd --system --gid 10001 paperdb \
    && useradd --system --uid 10001 --gid paperdb --home-dir /app paperdb

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev \
    && chown -R paperdb:paperdb /app

COPY --chown=paperdb:paperdb alembic.ini ./
COPY --chown=paperdb:paperdb alembic ./alembic
COPY scripts/entrypoint.sh /usr/local/bin/paperdb-entrypoint
RUN chmod 0755 /usr/local/bin/paperdb-entrypoint \
    && mkdir -p /data/files \
    && chown -R paperdb:paperdb /data

ENV PATH="/app/.venv/bin:$PATH"

USER paperdb
EXPOSE 9720

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 9720), 3).close()"

ENTRYPOINT ["paperdb-entrypoint"]
