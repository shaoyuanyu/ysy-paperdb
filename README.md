# paperdb

CS-native literature library backend. M1 scaffold: FastAPI + async SQLAlchemy +
PostgreSQL, bearer-token auth, local content-addressed storage, CCF/CAS dataset
import, and minimal papers/venues/authors CRUD.

See [docs/README.md](docs/README.md) for the full design.

## Quickstart (local, Podman Postgres)

Prerequisites: Python 3.12 (project-local via uv is fine), Podman, `uv`.

```bash
# 1. Postgres 17 via Podman
podman run -d --name paperdb-pg \
  -e POSTGRES_USER=paperdb -e POSTGRES_PASSWORD=paperdb_dev_password \
  -e POSTGRES_DB=paperdb -p 127.0.0.1:5432:5432 docker.io/library/postgres:17-alpine
podman exec paperdb-pg createdb -U paperdb paperdb_test || true

# 2. Python env
cp .env.example .env
UV_CACHE_DIR=$PWD/.uv-cache UV_PYTHON_INSTALL_DIR=$PWD/.uv-python uv sync --extra dev

# 3. Schema + datasets
UV_CACHE_DIR=$PWD/.uv-cache uv run alembic upgrade head
UV_CACHE_DIR=$PWD/.uv-cache uv run paperdb datasets import \
  --scheme ccf --version 2026 --file datasets/ccf-2026.json --valid-from 2026
UV_CACHE_DIR=$PWD/.uv-cache uv run paperdb datasets import \
  --scheme cas --version 2025 --file datasets/cas-2025.json --valid-from 2025

# 4. Run
UV_CACHE_DIR=$PWD/.uv-cache uv run uvicorn paperdb.main:app --reload --port 9720
```

OpenAPI: <http://127.0.0.1:9720/docs>.

## Compose (API + Postgres)

```bash
cp .env.example .env   # set PAPERDB_SERVICE_TOKEN / PAPERDB_TOKEN_PEPPER / POSTGRES_PASSWORD
podman compose up -d --build   # or: docker compose up -d --build
curl -s http://127.0.0.1:9720/readyz
```

The entrypoint runs `alembic upgrade head` before Uvicorn. The image intentionally
excludes `datasets/`; compose mounts it read-only so the import command can run:

```bash
podman compose exec paperdb-api paperdb datasets import --scheme ccf --version 2026 \
  --file /app/datasets/ccf-2026.json --valid-from 2026
```

For production set `PAPERDB_NETWORK=ysy-homepage-web_web` so the homepage
containers can resolve `paperdb-api:9720`; the site Nginx needs its
`/api/library/` upload limit raised separately (see docs/README.md §6.4).

## Authentication

The service token comes from `PAPERDB_SERVICE_TOKEN` (`pdb_svc_...`) and has all
scopes. Personal tokens are created through `POST /v1/tokens` (admin scope) and
shown exactly once.

```bash
export PAPERDB_TOKEN=pdb_svc_dev0123456789abcdef0123456789abcdef
curl -s http://127.0.0.1:9720/v1/meta \
  -H "Authorization: Bearer $PAPERDB_TOKEN" | jq
```

## Layout

```text
src/paperdb/
  main.py            FastAPI app, problem+json errors, readiness
  config.py          pydantic-settings (PAPERDB_*)
  db/                SQLAlchemy models/session (Postgres 17)
  auth/              service/personal/export token hashing, scopes
  storage/           StorageBackend + content-addressed local backend
  venues/            name/ISSN normalization, CCF/CAS import, rank resolver
  papers/            paper CRUD, citekey, evaluation serialization
  api/v1/            routers + Pydantic schemas
  cli.py             paperdb datasets/db/serve commands
alembic/             migrations
datasets/            CCF 2026 / CAS 2025 snapshots (do not publish)
docs/                design docs (moved from the homepage repo)
tests/               unit + Postgres integration tests
```

## M1 status

Done and verified locally against PostgreSQL 17:

- service/personal Bearer tokens + scope enforcement, token revoke; export token + `.bib` URL (library/paper scopes, ETag/304)
- CCF 2026 import: 677 venues / 681 rank rows (DBLP-key collisions handled;
  multi-field ranks kept as non-primary)
- CAS 2025 import: 547 new venues + 211 exact-name matches / 2060 rank rows
- venue/author/paper CRUD, citekey generation, CCF/CAS evaluation resolution
  (including `valid_from` fallback), cursor pagination
- OpenAPI at `/openapi.json`; local content-addressed `StorageBackend`
- Alembic migration, Dockerfile, compose, entrypoint migration, Podman image build verified
- 27 pytest tests (unit + real PostgreSQL integration), ruff and pyright clean

M1 intentionally does **not** include resolve/enrich (Crossref/arXiv/DBLP/...),
BibTeX import/export, PDF files/annotations, collections/tags/views, or the
Postgres job queue. Those start at M2–M4 per docs/README.md §9.

Next (M2): Crossref/arXiv/DBLP/OpenAlex/S2 resolve pipeline, venue normalization
helpers, duplicate detection/merge, BibTeX import, Postgres-backed jobs/worker.

## Development

```bash
UV_CACHE_DIR=$PWD/.uv-cache uv run pytest -q
UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check src tests
UV_CACHE_DIR=$PWD/.uv-cache uv run pyright
```
