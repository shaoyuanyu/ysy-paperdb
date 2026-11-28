from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

os.environ["PAPERDB_ENV"] = "test"
os.environ["PAPERDB_DATABASE_URL"] = (
    "postgresql+asyncpg://paperdb:paperdb_dev_password@127.0.0.1:5432/paperdb_test"
)
os.environ["PAPERDB_SERVICE_TOKEN"] = "pdb_svc_test0123456789abcdef0123456789abcdef"
os.environ["PAPERDB_TOKEN_PEPPER"] = "test-pepper-0123456789abcdef0123456789"
os.environ["PAPERDB_STORAGE_BACKEND"] = "local"
os.environ["PAPERDB_STORAGE_ROOT"] = str(Path(".test-storage").resolve())
os.environ["PAPERDB_TOTP_SECRET"] = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import paperdb.db.models  # noqa: F401
from paperdb.db.base import Base
from paperdb.main import app
from paperdb.venues.datasets import import_dataset, load_dataset

SERVICE_TOKEN = os.environ["PAPERDB_SERVICE_TOKEN"]
DATASETS = [
    ("ccf", "2026", "datasets/ccf-2026.json", 2026),
    ("cas", "2025", "datasets/cas-2025.json", 2025),
]


@pytest_asyncio.fixture(scope="session", autouse=True)
async def seeded_database() -> AsyncIterator[None]:
    engine = create_async_engine(os.environ["PAPERDB_DATABASE_URL"])
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    for scheme, version, filename, valid_from in DATASETS:
        payload, checksum = load_dataset(Path(filename))
        async with sessionmaker() as session:
            await import_dataset(
                session,
                scheme=scheme,
                version=version,
                payload=payload,
                checksum=checksum,
                valid_from=valid_from,
            )
            await session.commit()
    yield
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Direct DB session for service-level tests (no HTTP layer)."""
    from paperdb.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        yield session


@pytest_asyncio.fixture
async def drain_queue() -> AsyncIterator[None]:
    """Run pending worker jobs after a test that enqueues them.

    The queue is global: a leftover extract_pdf job would otherwise be the one
    a later test's run_once() executes.
    """
    yield
    from paperdb.jobs.worker import run_once

    while await run_once():
        pass


@pytest.fixture
def service_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVICE_TOKEN}"}


@pytest.fixture
def mcp_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run MCP tools against the in-process ASGI app instead of a live server.

    The MCP server is a thin HTTP client; pointing its transport at the app
    exercises the real request shapes (aliases, bodies, query params) without
    standing up uvicorn. The service token is used so TOTP-gated tools can be
    driven directly — TOTP itself is covered by test_totp.py.
    """
    from paperdb import mcp_server

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            timeout=30.0,
        )

    monkeypatch.setattr(mcp_server, "_client", factory)
