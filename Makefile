UV ?= uv
UV_ENV = UV_CACHE_DIR=$(CURDIR)/.uv-cache UV_PYTHON_INSTALL_DIR=$(CURDIR)/.uv-python

.PHONY: sync test lint format typecheck migrate import-ccf import-cas run

sync:
	$(UV_ENV) $(UV) sync --extra dev --extra mcp

test:
	$(UV_ENV) $(UV) run pytest -q

lint:
	$(UV_ENV) $(UV) run ruff check src tests
	$(UV_ENV) $(UV) run ruff format --check src tests

format:
	$(UV_ENV) $(UV) run ruff format src tests
	$(UV_ENV) $(UV) run ruff check --fix src tests

typecheck:
	$(UV_ENV) $(UV) run pyright

migrate:
	$(UV_ENV) $(UV) run alembic upgrade head

import-ccf:
	$(UV_ENV) $(UV) run paperdb datasets import --scheme ccf --version 2026 \
		--file datasets/ccf-2026.json --valid-from 2026

import-cas:
	$(UV_ENV) $(UV) run paperdb datasets import --scheme cas --version 2025 \
		--file datasets/cas-2025.json --valid-from 2025

run:
	$(UV_ENV) $(UV) run uvicorn paperdb.main:app --reload --port 9720
