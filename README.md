# paperdb

CS-native literature library backend (M1–M7): FastAPI + async SQLAlchemy +
PostgreSQL, bearer-token auth, CCF/CAS evaluation, venue normalization, metadata
resolve/import, collections/tags/views/projects, PDF fulltext/annotations, and
OpenAlex citation graph.

See [docs/README.md](docs/README.md) for the full design; implementation notes: [M1](docs/M1.md), [M2](docs/M2.md), [M3](docs/M3.md), [M4](docs/M4.md), [M6](docs/M6.md), [deploy](docs/deploy.md).

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
UV_CACHE_DIR=$PWD/.uv-cache UV_PYTHON_INSTALL_DIR=$PWD/.uv-python uv sync --extra dev --extra mcp

# 3. Schema + datasets
UV_CACHE_DIR=$PWD/.uv-cache uv run alembic upgrade head
UV_CACHE_DIR=$PWD/.uv-cache uv run paperdb datasets import \
  --scheme ccf --version 2026 --file datasets/ccf-2026.json --valid-from 2026
UV_CACHE_DIR=$PWD/.uv-cache uv run paperdb datasets import \
  --scheme cas --version 2025 --file datasets/cas-2025.json --valid-from 2025

# 4. Run API (terminal A) and worker (terminal B)
UV_CACHE_DIR=$PWD/.uv-cache uv run uvicorn paperdb.main:app --reload --port 9720
UV_CACHE_DIR=$PWD/.uv-cache uv run paperdb worker
```

OpenAPI: <http://127.0.0.1:9720/docs>.

## Compose (API + worker + Postgres)

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
  ingest/            Crossref/arXiv/DBLP/OpenAlex/S2/ACL/OpenReview resolve
  imports/           identifier/BibTeX import jobs
  organize/          collections/tags/views/projects/auto-rules/filter DSL
  pdf/               PyMuPDF extraction, PDF/annotation/search services
  citations/         OpenAlex citation sync
  jobs/              Postgres queue + worker
  api/v1/            routers + Pydantic schemas
  cli.py             paperdb datasets/db/serve/worker commands
alembic/             migrations
datasets/            CCF 2026 / CAS 2025 snapshots (do not publish)
docs/                design docs (moved from the homepage repo)
tests/               unit + Postgres integration tests
```

## Status (M1–M7)

Done and verified locally against PostgreSQL 17:

- service/personal Bearer tokens + scope enforcement, token revoke; export token + `.bib` URL (library/paper scopes, ETag/304)
- CCF 2026 import: 677 venues / 681 rank rows (DBLP-key collisions handled;
  multi-field ranks kept as non-primary)
- CAS 2025 import: 547 new venues + 211 exact-name matches / 2060 rank rows
- paper CRUD; venue/author create/read/update; citekey generation; CCF/CAS evaluation resolution
  (including `valid_from` fallback), cursor pagination
- resolve/enrich: Crossref, arXiv, DBLP search/record, OpenAlex, S2, ACL `.bib`, OpenReview
- Postgres job queue + worker; identifier/BibTeX imports; dedupe/merge; BibTeX export
- OpenAPI at `/openapi.json`; local content-addressed `StorageBackend`
- Alembic migration, Dockerfile, compose, entrypoint migration, Podman image build verified
- 48 pytest tests (unit + real PostgreSQL integration), ruff and pyright clean

M5 adds the owner-only Next.js library (list/detail/import/settings), the
double-page/inverted PDF reader, and citation sections; M6 adds OpenAlex
citation edges/stubs. M7 adds the deploy/backup/runbook.

Next: production smoke + periodic citation sync; optional graph canvas.

## Agent integration (MCP server + skill)

`src/paperdb/mcp_server.py` wraps the HTTP API as an MCP stdio server
(`uv sync --extra mcp`, then `.venv/bin/paperdb-mcp`), exposing the whole API
surface as agent tools — import/resolve/BibTeX, reading (text, page images,
scoped search), organization (tags/collections/projects/views/rules,
**research workspaces** whose papers stay out of the library until promoted),
and TOTP-gated deletes/merges. `docs/mcp.md` is the tool-by-tool contract and
the coverage guard (`tests/test_mcp_coverage.py`) keeps it honest.
Import tools poll their job and return the final per-item status plus the
paper. Configuration via env: `PAPERDB_MCP_BASE_URL` (default
`http://127.0.0.1:9720`) and a token from `PAPERDB_MCP_TOKEN`, else the file in
`PAPERDB_MCP_TOKEN_FILE`, else `~/.config/paperdb/mcp-token`.

The DeepSeek Harness integration lives in `for-agents/` — the
`@local/dsh-paperdb-mcp` bundle plus the `my-paperdb` skill, hand-edited as
source and packaged by `scripts/build_dsh_release.py` into the GitHub Release
asset `paperdb-dsh-<version>.tar.gz` (`.github/workflows/release.yml`, every
`v*` tag). `paperdb-mcp` installs a release into
`PAPERDB_DSH_INTEGRATION_DIR` through `dsh_integration_status` /
`update_dsh_integration` / `rollback_dsh_integration`; applying an update is
TOTP-gated, verifies the asset digest plus a per-file manifest, and keeps the
previous files for rollback. The bundle carries no secret: the token lives in
`~/.config/paperdb/mcp-token` (or wherever `PAPERDB_MCP_TOKEN_FILE` points).
DSH reads only the installed copy (`~/.dsh/paperdb-integration/`): both
`~/.agents/skills/my-paperdb` and `.dsh/skills/my-paperdb` are symlinks to its
skill, and the profile's `@local/dsh-paperdb-mcp` link points at its bundle.
Edit `for-agents/`, then render+install locally with
`python scripts/install_dsh_integration.py`. Layout, the source/installed split,
publishing and trust model: `for-agents/README.md`. The skill teaches agents
the import ladder (URL → identifier → BibTeX → PDF → WeChat-article-to-title),
the verify-before-reporting rules, and the OA PDF acquisition paths.

Landing-page ingestion (2026-11): `resolve` and all import entry points accept
any paper page URL. The page is fetched, mined for `citation_*`/og/H1/DOI/
arXiv/PDF links (`ingest/page.py`), then resolved through the aggregators; page
metadata is identification only, never the authority. Verified end-to-end with
the NDSS page (no DOI, no citation meta tags): title search → venue NDSS →
CCF-A → PDF download. DBLP is currently behind an anti-bot challenge (Anubis);
the backend degrades gracefully to OpenAlex.

## Development

```bash
UV_CACHE_DIR=$PWD/.uv-cache uv run pytest -q
UV_CACHE_DIR=$PWD/.uv-cache uv run ruff check src tests
UV_CACHE_DIR=$PWD/.uv-cache uv run pyright
```
