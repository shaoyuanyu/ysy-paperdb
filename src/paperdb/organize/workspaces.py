"""Research workspaces: a staging area for batch research.

A workspace is a peer of projects and collections, but its papers are not part
of the library yet: ``Paper.staged`` is true and the default listing hides
them. Research ends with a decision per paper — promote it into the library
(optionally into a collection) or discard it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.db.models import (
    Collection,
    CollectionPaper,
    Paper,
    PdfFile,
    ResearchWorkspace,
    ResearchWorkspacePaper,
)
from paperdb.errors import ApiError
from paperdb.papers.service import get_paper


async def get_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> ResearchWorkspace:
    workspace = await session.get(ResearchWorkspace, workspace_id)
    if workspace is None:
        raise ApiError(404, "not_found", "Research workspace not found")
    return workspace


async def create_workspace(
    session: AsyncSession, *, name: str, description: str | None = None
) -> ResearchWorkspace:
    name = name.strip()
    if not name:
        raise ApiError(422, "invalid_name", "Workspace name must not be empty")
    existing = await session.scalar(
        select(ResearchWorkspace).where(
            ResearchWorkspace.name == name, ResearchWorkspace.closed_at.is_(None)
        )
    )
    if existing is not None:
        raise ApiError(409, "conflict", f"A workspace named {name!r} is already open")
    workspace = ResearchWorkspace(
        id=uuid.uuid4(), name=name, description=description.strip() if description else None
    )
    session.add(workspace)
    await session.flush()
    return workspace


async def update_workspace(
    session: AsyncSession,
    workspace: ResearchWorkspace,
    *,
    name: str | None = None,
    description: str | None = None,
) -> ResearchWorkspace:
    if name is not None:
        name = name.strip()
        if not name:
            raise ApiError(422, "invalid_name", "Workspace name must not be empty")
        clash = await session.scalar(
            select(ResearchWorkspace.id).where(
                ResearchWorkspace.name == name,
                ResearchWorkspace.id != workspace.id,
                ResearchWorkspace.closed_at.is_(None),
            )
        )
        if clash is not None:
            raise ApiError(409, "conflict", f"A workspace named {name!r} is already open")
        workspace.name = name
    if description is not None:
        workspace.description = description.strip() or None
    await session.flush()
    return workspace


async def _membership(
    session: AsyncSession, workspace_id: uuid.UUID, paper_id: uuid.UUID
) -> ResearchWorkspacePaper | None:
    return await session.get(ResearchWorkspacePaper, (workspace_id, paper_id))


async def stage_paper(
    session: AsyncSession,
    workspace: ResearchWorkspace,
    paper: Paper,
    *,
    note: str | None = None,
) -> ResearchWorkspacePaper:
    """Add a paper to the workspace and mark it staged (not in the library)."""
    membership = await _membership(session, workspace.id, paper.id)
    if membership is None:
        next_position = await session.scalar(
            select(func.max(ResearchWorkspacePaper.position)).where(
                ResearchWorkspacePaper.workspace_id == workspace.id
            )
        )
        membership = ResearchWorkspacePaper(
            workspace_id=workspace.id,
            paper_id=paper.id,
            position=(next_position or 0) + 1,
            note=note,
        )
        session.add(membership)
    else:
        membership.promoted_at = None
        if note is not None:
            membership.note = note
    paper.staged = True
    paper.version += 1
    await session.flush()
    return membership


async def add_papers(
    session: AsyncSession, workspace: ResearchWorkspace, refs: list[str], *, note: str | None = None
) -> list[Paper]:
    added: list[Paper] = []
    for ref in refs:
        paper = await get_paper(session, ref)
        await stage_paper(session, workspace, paper, note=note)
        added.append(paper)
    return added


async def _refresh_staged(session: AsyncSession, paper: Paper) -> None:
    # Sessions run with autoflush=False; the membership change must be visible
    # to the count below before the paper's flag is recomputed.
    await session.flush()
    pending = await session.scalar(
        select(func.count())
        .select_from(ResearchWorkspacePaper)
        .where(
            ResearchWorkspacePaper.paper_id == paper.id,
            ResearchWorkspacePaper.promoted_at.is_(None),
        )
    )
    paper.staged = bool(pending)
    paper.version += 1


async def promote_papers(
    session: AsyncSession,
    workspace: ResearchWorkspace,
    refs: list[str],
    *,
    collection_id: uuid.UUID | None = None,
) -> list[Paper]:
    """Move papers from the staging area into the library."""
    if collection_id is not None:
        collection = await session.get(Collection, collection_id)
        if collection is None or collection.deleted_at is not None:
            raise ApiError(404, "not_found", "Collection not found")
    now = datetime.now(UTC)
    promoted: list[Paper] = []
    for ref in refs:
        paper = await get_paper(session, ref)
        membership = await _membership(session, workspace.id, paper.id)
        if membership is not None and membership.promoted_at is None:
            membership.promoted_at = now
        await _refresh_staged(session, paper)
        if (
            collection_id is not None
            and await session.get(CollectionPaper, (collection_id, paper.id)) is None
        ):
            session.add(CollectionPaper(collection_id=collection_id, paper_id=paper.id))
        promoted.append(paper)
    await session.flush()
    return promoted


async def discard_papers(
    session: AsyncSession, workspace: ResearchWorkspace, refs: list[str]
) -> list[Paper]:
    """Drop papers from the workspace; staged papers with no other home die."""
    now = datetime.now(UTC)
    discarded: list[Paper] = []
    for ref in refs:
        paper = await get_paper(session, ref)
        membership = await _membership(session, workspace.id, paper.id)
        if membership is not None:
            await session.delete(membership)
            await session.flush()
        await _refresh_staged(session, paper)
        if paper.staged:
            # Still staged in another workspace — just untie it from this one.
            discarded.append(paper)
            continue
        if paper.deleted_at is None and not await _in_library(session, paper):
            paper.deleted_at = now
            paper.version += 1
        discarded.append(paper)
    await session.flush()
    return discarded


async def _in_library(session: AsyncSession, paper: Paper) -> bool:
    return bool(
        await session.scalar(
            select(func.count())
            .select_from(CollectionPaper)
            .where(CollectionPaper.paper_id == paper.id)
        )
    )


async def close_workspace(
    session: AsyncSession, workspace: ResearchWorkspace, *, discard_unpromoted: bool = False
) -> ResearchWorkspace:
    if workspace.closed_at is None:
        workspace.closed_at = datetime.now(UTC)
    if discard_unpromoted:
        pending = list(
            (
                await session.scalars(
                    select(ResearchWorkspacePaper).where(
                        ResearchWorkspacePaper.workspace_id == workspace.id,
                        ResearchWorkspacePaper.promoted_at.is_(None),
                    )
                )
            ).all()
        )
        for membership in pending:
            paper = await session.get(Paper, membership.paper_id)
            if paper is None:
                continue
            await session.delete(membership)
            await session.flush()
            await _refresh_staged(session, paper)
            # After the refresh the flag means "staged somewhere else"; only
            # papers with no remaining home in the library are dropped.
            if (
                not paper.staged
                and paper.deleted_at is None
                and not await _in_library(session, paper)
            ):
                paper.deleted_at = datetime.now(UTC)
                paper.version += 1
    await session.flush()
    return workspace


async def workspace_stats(
    session: AsyncSession, workspace_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    if not workspace_ids:
        return {}
    rows = (
        await session.execute(
            select(
                ResearchWorkspacePaper.workspace_id,
                func.count(),
                func.count().filter(ResearchWorkspacePaper.promoted_at.is_(None)),
            )
            .join(Paper, Paper.id == ResearchWorkspacePaper.paper_id)
            .where(
                ResearchWorkspacePaper.workspace_id.in_(workspace_ids),
                Paper.deleted_at.is_(None),
            )
            .group_by(ResearchWorkspacePaper.workspace_id)
        )
    ).all()
    result = {
        workspace_id: {"paper_count": int(total), "pending_count": int(pending)}
        for workspace_id, total, pending in rows
    }
    for workspace_id in workspace_ids:
        result.setdefault(workspace_id, {"paper_count": 0, "pending_count": 0})
    return result


async def list_workspaces(session: AsyncSession, *, include_closed: bool = False) -> list[dict]:
    stmt = select(ResearchWorkspace).order_by(
        ResearchWorkspace.closed_at.is_not(None), ResearchWorkspace.created_at.desc()
    )
    if not include_closed:
        stmt = stmt.where(ResearchWorkspace.closed_at.is_(None))
    rows = list((await session.scalars(stmt)).all())
    stats = await workspace_stats(session, [row.id for row in rows])
    return [
        {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "created_at": row.created_at,
            "closed_at": row.closed_at,
            "paper_count": stats[row.id]["paper_count"],
            "pending_count": stats[row.id]["pending_count"],
        }
        for row in rows
    ]


async def workspace_papers(
    session: AsyncSession,
    workspace: ResearchWorkspace,
    *,
    pending_only: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Compact paper rows for an agent to review (newest position first)."""
    stmt = (
        select(ResearchWorkspacePaper, Paper)
        .join(Paper, Paper.id == ResearchWorkspacePaper.paper_id)
        .where(
            ResearchWorkspacePaper.workspace_id == workspace.id,
            Paper.deleted_at.is_(None),
        )
        .order_by(ResearchWorkspacePaper.position, ResearchWorkspacePaper.added_at)
        .limit(limit)
    )
    if pending_only:
        stmt = stmt.where(ResearchWorkspacePaper.promoted_at.is_(None))
    rows = (await session.execute(stmt)).all()
    # Whether each paper is actually readable belongs in the survey list: an
    # agent that has to call get_paper once per row just to learn which ones
    # have an extracted PDF burns a dozen round-trips for nothing.
    files: dict[uuid.UUID, list[PdfFile]] = {}
    paper_ids = [paper.id for _, paper in rows]
    if paper_ids:
        file_rows = (
            await session.execute(
                select(PdfFile)
                .where(PdfFile.paper_id.in_(paper_ids), PdfFile.deleted_at.is_(None))
                .order_by(PdfFile.is_primary.desc(), PdfFile.created_at)
            )
        ).scalars()
        for file in file_rows:
            files.setdefault(file.paper_id, []).append(file)
    return [
        {
            "paper_id": paper.id,
            "citekey": paper.citekey,
            "title": paper.title,
            "year": paper.year,
            "kind": paper.kind.value,
            "promoted": membership.promoted_at is not None,
            "note": membership.note,
            "pdf_count": len(files.get(paper.id, [])),
            "text_status": files[paper.id][0].text_status.value if files.get(paper.id) else None,
        }
        for membership, paper in rows
    ]
