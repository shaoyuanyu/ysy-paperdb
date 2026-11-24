#!/bin/sh
set -eu

# Run migrations before serving. Safe to re-run: Alembic is idempotent.
alembic upgrade head

exec uvicorn paperdb.main:app \
  --host 0.0.0.0 \
  --port "${PAPERDB_PORT:-9720}" \
  --workers "${PAPERDB_WORKERS:-1}" \
  --proxy-headers \
  --forwarded-allow-ips "${PAPERDB_FORWARDED_ALLOW_IPS:-127.0.0.1}"
