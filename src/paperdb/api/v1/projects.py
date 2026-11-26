from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import require_totp, scoped
from paperdb.api.schemas import (
    PaperPage,
    PaperSummary,
    ProjectCollectionsInput,
    ProjectCreate,
    ProjectOut,
    ProjectPapersInput,
    ProjectUpdate,
)
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.models import Paper
from paperdb.db.session import get_session
from paperdb.organize.projects import (
    add_collections,
    add_papers,
    create_project,
    delete_project,
    get_project,
    list_projects,
    project_count,
    project_paper_ids,
    remove_collections,
    remove_papers,
    update_project,
)
from paperdb.papers.service import PAPER_OPTIONS, paper_to_summary, ranks_for_venues

router = APIRouter(prefix="/projects", tags=["projects"])


async def _out(session: AsyncSession, project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        citekey_template=project.citekey_template,
        export_include_abstract=project.export_include_abstract,
        collection_ids=[link.collection_id for link in project.collections],
        paper_ids=[link.paper_id for link in project.papers],
        paper_count=await project_count(session, project),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.get("", response_model=list[ProjectOut])
async def list_endpoint(
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> list[ProjectOut]:
    return [await _out(session, project) for project in await list_projects(session)]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: ProjectCreate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = await create_project(
        session,
        name=payload.name,
        description=payload.description,
        citekey_template=payload.citekey_template,
        export_include_abstract=payload.export_include_abstract,
    )
    await session.commit()
    project = await get_project(session, project.id)
    return await _out(session, project)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_endpoint(
    project_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    return await _out(session, await get_project(session, project_id))


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_endpoint(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = await get_project(session, project_id)
    await update_project(session, project, payload.model_dump(exclude_unset=True))
    await session.commit()
    return await _out(session, project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    project_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
    totp: None = Depends(require_totp),
) -> Response:
    await delete_project(session, await get_project(session, project_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{project_id}/collections")
async def add_collections_endpoint(
    project_id: uuid.UUID,
    payload: ProjectCollectionsInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    project = await get_project(session, project_id)
    added = await add_collections(session, project, payload.collection_ids)
    await session.commit()
    return {"added": added}


@router.delete("/{project_id}/collections")
async def remove_collections_endpoint(
    project_id: uuid.UUID,
    payload: ProjectCollectionsInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    project = await get_project(session, project_id)
    removed = await remove_collections(session, project, payload.collection_ids)
    await session.commit()
    return {"removed": removed}


@router.post("/{project_id}/papers")
async def add_papers_endpoint(
    project_id: uuid.UUID,
    payload: ProjectPapersInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    project = await get_project(session, project_id)
    added = await add_papers(session, project, payload.paper_ids)
    await session.commit()
    return {"added": added}


@router.delete("/{project_id}/papers")
async def remove_papers_endpoint(
    project_id: uuid.UUID,
    payload: ProjectPapersInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    project = await get_project(session, project_id)
    removed = await remove_papers(session, project, payload.paper_ids)
    await session.commit()
    return {"removed": removed}


@router.get("/{project_id}/papers", response_model=PaperPage)
async def papers_endpoint(
    project_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> PaperPage:
    project = await get_project(session, project_id)
    ids = await project_paper_ids(session, project)
    rows = list(
        (
            await session.scalars(
                select(Paper)
                .options(*PAPER_OPTIONS)
                .where(Paper.id.in_(ids), Paper.deleted_at.is_(None))
                .order_by(Paper.created_at.desc())
                .limit(limit)
            )
        ).all()
    )
    ranks = await ranks_for_venues(session, [paper.venue_id for paper in rows if paper.venue_id])
    return PaperPage(
        items=[PaperSummary.model_validate(paper_to_summary(paper, ranks)) for paper in rows],
        has_more=len(ids) > len(rows),
    )
