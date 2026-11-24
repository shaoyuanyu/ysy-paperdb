from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.db.models import Job, JobStatus


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, object] | None = None,
    *,
    priority: int = 0,
    run_at: datetime | None = None,
    max_attempts: int = 5,
) -> Job:
    job = Job(
        id=uuid.uuid4(),
        kind=kind,
        payload=payload or {},
        status=JobStatus.pending,
        priority=priority,
        run_at=run_at or datetime.now(UTC),
        max_attempts=max_attempts,
    )
    session.add(job)
    await session.flush()
    return job


async def claim_job(session: AsyncSession, worker_id: str) -> Job | None:
    now = datetime.now(UTC)
    job = await session.scalar(
        select(Job)
        .where(
            Job.status.in_([JobStatus.pending, JobStatus.failed]),
            Job.run_at <= now,
        )
        .order_by(Job.priority.asc(), Job.run_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = JobStatus.running
    job.locked_by = worker_id
    job.locked_at = now
    job.heartbeat_at = now
    job.attempts += 1
    await session.flush()
    return job


async def heartbeat(session: AsyncSession, job: Job) -> None:
    job.heartbeat_at = datetime.now(UTC)
    await session.flush()


async def complete(session: AsyncSession, job: Job) -> None:
    job.status = JobStatus.succeeded
    job.finished_at = datetime.now(UTC)
    job.locked_by = None
    job.locked_at = None
    job.heartbeat_at = None
    await session.flush()


async def fail(session: AsyncSession, job: Job, error: str) -> None:
    job.last_error = error[:8000]
    job.locked_by = None
    job.locked_at = None
    job.heartbeat_at = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.dead
        job.finished_at = datetime.now(UTC)
    else:
        job.status = JobStatus.failed
        delay = min(30 * (2 ** max(job.attempts - 1, 0)), 3600)
        job.run_at = datetime.now(UTC) + timedelta(seconds=delay)
    await session.flush()


async def reclaim_stale(session: AsyncSession, older_than_seconds: int = 900) -> int:
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    result = await session.execute(
        update(Job)
        .where(Job.status == JobStatus.running, Job.heartbeat_at < cutoff)
        .values(
            status=JobStatus.failed,
            locked_by=None,
            locked_at=None,
            heartbeat_at=None,
            run_at=datetime.now(UTC),
            last_error="worker lease expired",
        )
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)
