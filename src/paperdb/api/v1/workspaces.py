from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import (
    ResearchWorkspaceCreate,
    ResearchWorkspaceDetail,
    ResearchWorkspaceOut,
    ResearchWorkspacePage,
    ResearchWorkspaceUpdate,
    WorkspaceCloseInput,
    WorkspacePaperOut,
    WorkspacePapersInput,
    WorkspacePromoteInput,
)
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.session import get_session
from paperdb.organize.workspaces import (
    add_papers,
    close_workspace,
    create_workspace,
    discard_papers,
    get_workspace,
    list_workspaces,
    promote_papers,
    update_workspace,
    workspace_papers,
    workspace_stats,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _out(row: dict) -> ResearchWorkspaceOut:
    return ResearchWorkspaceOut(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
        paper_count=row["paper_count"],
        pending_count=row["pending_count"],
    )


async def _detail(
    session: AsyncSession, workspace_id: uuid.UUID, *, limit: int = 200, pending_only: bool = False
) -> ResearchWorkspaceDetail:
    workspace = await get_workspace(session, workspace_id)
    stats = await workspace_stats(session, [workspace.id])
    papers = await workspace_papers(
        session, workspace, limit=limit, pending_only=pending_only
    )
    return ResearchWorkspaceDetail(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        created_at=workspace.created_at,
        closed_at=workspace.closed_at,
        paper_count=stats[workspace.id]["paper_count"],
        pending_count=stats[workspace.id]["pending_count"],
        papers=[WorkspacePaperOut(**item) for item in papers],
    )


@router.get("", response_model=ResearchWorkspacePage)
async def list_endpoint(
    include_closed: bool = False,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> ResearchWorkspacePage:
    rows = await list_workspaces(session, include_closed=include_closed)
    return ResearchWorkspacePage(items=[_out(row) for row in rows])


@router.post("", response_model=ResearchWorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: ResearchWorkspaceCreate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> ResearchWorkspaceOut:
    workspace = await create_workspace(
        session, name=payload.name, description=payload.description
    )
    await session.commit()
    stats = await workspace_stats(session, [workspace.id])
    return ResearchWorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        created_at=workspace.created_at,
        closed_at=workspace.closed_at,
        paper_count=stats[workspace.id]["paper_count"],
        pending_count=stats[workspace.id]["pending_count"],
    )


@router.get("/{workspace_id}", response_model=ResearchWorkspaceDetail)
async def get_endpoint(
    workspace_id: uuid.UUID,
    pending_only: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> ResearchWorkspaceDetail:
    return await _detail(session, workspace_id, limit=limit, pending_only=pending_only)


@router.patch("/{workspace_id}", response_model=ResearchWorkspaceDetail)
async def update_endpoint(
    workspace_id: uuid.UUID,
    payload: ResearchWorkspaceUpdate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> ResearchWorkspaceDetail:
    workspace = await get_workspace(session, workspace_id)
    await update_workspace(
        session, workspace, name=payload.name, description=payload.description
    )
    await session.commit()
    return await _detail(session, workspace_id)


@router.get("/{workspace_id}/papers", response_model=list[WorkspacePaperOut])
async def papers_endpoint(
    workspace_id: uuid.UUID,
    pending_only: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> list[WorkspacePaperOut]:
    workspace = await get_workspace(session, workspace_id)
    rows = await workspace_papers(
        session, workspace, limit=limit, pending_only=pending_only
    )
    return [WorkspacePaperOut(**item) for item in rows]


@router.post("/{workspace_id}/papers")
async def add_papers_endpoint(
    workspace_id: uuid.UUID,
    payload: WorkspacePapersInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    workspace = await get_workspace(session, workspace_id)
    papers = await add_papers(session, workspace, payload.paper_refs)
    await session.commit()
    return {"added": len(papers), "paper_ids": [paper.id for paper in papers]}


@router.post("/{workspace_id}/promote")
async def promote_endpoint(
    workspace_id: uuid.UUID,
    payload: WorkspacePromoteInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    workspace = await get_workspace(session, workspace_id)
    papers = await promote_papers(
        session, workspace, payload.paper_refs, collection_id=payload.collection_id
    )
    await session.commit()
    return {"promoted": len(papers), "paper_ids": [paper.id for paper in papers]}


@router.post("/{workspace_id}/discard")
async def discard_endpoint(
    workspace_id: uuid.UUID,
    payload: WorkspacePapersInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    workspace = await get_workspace(session, workspace_id)
    papers = await discard_papers(session, workspace, payload.paper_refs)
    await session.commit()
    return {"discarded": len(papers), "paper_ids": [paper.id for paper in papers]}


@router.post("/{workspace_id}/close", response_model=ResearchWorkspaceDetail)
async def close_endpoint(
    workspace_id: uuid.UUID,
    payload: WorkspaceCloseInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> ResearchWorkspaceDetail:
    workspace = await get_workspace(session, workspace_id)
    await close_workspace(
        session, workspace, discard_unpromoted=payload.discard_unpromoted
    )
    await session.commit()
    return await _detail(session, workspace_id)
