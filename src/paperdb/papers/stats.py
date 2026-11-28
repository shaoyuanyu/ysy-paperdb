"""One-call overview of the library.

An agent cannot page through ten thousand papers to answer "what do I have?".
The stats endpoint is that glance: totals, reading status spread, and how much
of the corpus is actually readable (PDF present, text extracted).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.db.models import (
    Collection,
    Paper,
    PaperState,
    PdfFile,
    PdfTextStatus,
    Project,
    ResearchWorkspace,
    Tag,
)


async def library_stats(session: AsyncSession) -> dict[str, Any]:
    live = Paper.deleted_at.is_(None)
    in_library = and_(live, Paper.staged.is_(False))

    papers = await session.scalar(select(func.count()).select_from(Paper).where(in_library)) or 0
    staged = (
        await session.scalar(
            select(func.count()).select_from(Paper).where(and_(live, Paper.staged.is_(True)))
        )
        or 0
    )

    with_pdf = (
        await session.scalar(
            select(func.count(func.distinct(PdfFile.paper_id)))
            .select_from(PdfFile)
            .join(Paper, Paper.id == PdfFile.paper_id)
            .where(in_library, PdfFile.deleted_at.is_(None))
        )
        or 0
    )
    text_indexed = (
        await session.scalar(
            select(func.count(func.distinct(PdfFile.paper_id)))
            .select_from(PdfFile)
            .join(Paper, Paper.id == PdfFile.paper_id)
            .where(
                in_library,
                PdfFile.deleted_at.is_(None),
                PdfFile.text_status == PdfTextStatus.extracted,
            )
        )
        or 0
    )

    # Papers with no state row are unread; the outer join keeps them counted.
    status_rows = (
        await session.execute(
            select(PaperState.status, func.count())
            .select_from(Paper)
            .outerjoin(PaperState, PaperState.paper_id == Paper.id)
            .where(in_library)
            .group_by(PaperState.status)
        )
    ).all()
    by_status = {
        (status.value if status is not None else "unread"): int(count)
        for status, count in status_rows
    }

    collections = (
        await session.scalar(
            select(func.count()).select_from(Collection).where(Collection.deleted_at.is_(None))
        )
        or 0
    )
    tags = await session.scalar(select(func.count()).select_from(Tag)) or 0
    projects = (
        await session.scalar(
            select(func.count()).select_from(Project).where(Project.archived_at.is_(None))
        )
        or 0
    )
    workspaces = (
        await session.scalar(
            select(func.count())
            .select_from(ResearchWorkspace)
            .where(ResearchWorkspace.closed_at.is_(None))
        )
        or 0
    )

    return {
        "papers": int(papers),
        "staged": int(staged),
        "with_pdf": int(with_pdf),
        "text_indexed": int(text_indexed),
        "by_status": by_status,
        "collections": int(collections),
        "tags": int(tags),
        "projects": int(projects),
        "workspaces": int(workspaces),
    }
