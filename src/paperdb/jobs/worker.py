from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.config import Settings, get_settings
from paperdb.db.models import Job
from paperdb.db.session import get_sessionmaker
from paperdb.jobs.handlers import (
    handle_bibtex,
    handle_enrich,
    handle_extract_pdf,
    handle_gc_blob,
    handle_import,
    handle_sync_citations,
)
from paperdb.jobs.queue import claim_job, complete, fail, reclaim_stale

logger = logging.getLogger("paperdb.worker")

Handler = Callable[[AsyncSession, Job, Settings], Awaitable[None]]
HANDLERS: dict[str, Handler] = {
    "import_identifiers": handle_import,
    "import_bibtex": handle_bibtex,
    "enrich_paper": handle_enrich,
    "extract_pdf": handle_extract_pdf,
    "gc_blob": handle_gc_blob,
    "sync_citations": handle_sync_citations,
}


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def run_once(*, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    async with get_sessionmaker()() as session:
        job = await claim_job(session, worker_id())
        await session.commit()
        if job is None:
            return False
        handler = HANDLERS.get(job.kind)
        if handler is None:
            logger.error("no handler for job kind=%s id=%s", job.kind, job.id)
            await fail(session, job, f"no handler for {job.kind}")
            await session.commit()
            return True
        try:
            await handler(session, job, settings)
            await complete(session, job)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            job = await session.get(Job, job.id)
            if job is not None:
                await fail(session, job, str(exc))
                await session.commit()
            logger.exception(
                "job failed id=%s kind=%s", job.id if job else "?", job.kind if job else "?"
            )
        return True


async def run_forever(*, settings: Settings | None = None, interval: float = 1.0) -> None:
    settings = settings or get_settings()
    logger.info("paperdb worker started id=%s", worker_id())
    try:
        while True:
            async with get_sessionmaker()() as session:
                await reclaim_stale(session)
                await session.commit()
            worked = await run_once(settings=settings)
            if not worked:
                await asyncio.sleep(interval)
    finally:
        logger.info("paperdb worker stopped id=%s", worker_id())
