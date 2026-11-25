# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PAPERDB_STORAGE_ROOT=/data/files

WORKDIR /app

# 允许在 PyPI 直连较慢的网络（例如国内 VPS）通过 build-arg 切换到镜像站；
# 默认仍用官方源，保证构建可复现/可审计。例如：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#                --build-arg UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG UV_INDEX_URL=https://pypi.org/simple

RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" "uv>=0.5,<1" \
    && groupadd --system --gid 10001 paperdb \
    && useradd --system --uid 10001 --gid paperdb --home-dir /app paperdb

COPY pyproject.toml uv.lock README.md ./
RUN UV_INDEX_URL="$UV_INDEX_URL" uv sync --frozen --no-dev \
    && chown -R paperdb:paperdb /app/.venv
# src is copied *after* the dependency sync so source-only deploys reuse the
# dependency layer. The first sync installs an empty project wheel (src is
# absent); the second sync below reinstalls the project from real source.
# It is network-free and completes in milliseconds.
COPY --chown=paperdb:paperdb src ./src
RUN chmod 644 /app/pyproject.toml /app/uv.lock /app/README.md \
    && UV_INDEX_URL="$UV_INDEX_URL" uv sync --frozen --no-dev \
    && chown -R paperdb:paperdb /app/.venv

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
