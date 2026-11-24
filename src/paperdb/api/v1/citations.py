from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import (
    CitationItemOut,
    CitationPage,
    CitationPaperOut,
    CitationSyncCreated,
    CitationSyncRequest,
    ExternalWorkOut,
)
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.models import (
    CitationEdge,
    ExternalCitation,
    ExternalWork,
    Paper,
)
from paperdb.db.session import get_session
from paperdb.jobs.queue import enqueue
from paperdb.papers.service import get_paper

router = APIRouter(tags=["citations"])


def _paper_out(paper: Paper) -> CitationPaperOut:
    return CitationPaperOut(id=paper.id, title=paper.title, year=paper.year, citekey=paper.citekey)


def _external_out(work: ExternalWork) -> ExternalWorkOut:
    return ExternalWorkOut(
        id=work.id,
        title=work.title,
        year=work.year,
        doi=work.doi,
        openalex_id=work.openalex_id,
        cited_by_count=work.cited_by_count,
        resolved_paper_id=work.resolved_paper_id,
    )


@router.get("/papers/{ref}/references", response_model=CitationPage)
async def references_endpoint(
    ref: str,
    limit: int = Query(default=50, ge=1, le=200),
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> CitationPage:
    paper = await get_paper(session, ref)
    edges = (
        await session.execute(
            select(CitationEdge, Paper)
            .join(Paper, Paper.id == CitationEdge.cited_paper_id)
            .where(
                CitationEdge.citing_paper_id == paper.id,
                Paper.deleted_at.is_(None),
            )
            .order_by(Paper.year.desc())
            .limit(limit + 1)
        )
    ).all()
    externals = (
        await session.execute(
            select(ExternalCitation, ExternalWork)
            .join(ExternalWork, ExternalWork.id == ExternalCitation.external_work_id)
            .where(
                ExternalCitation.paper_id == paper.id,
                ExternalCitation.direction == "cites",
            )
            .order_by(ExternalWork.year.desc().nullslast())
            .limit(limit + 1)
        )
    ).all()
    items = [
        CitationItemOut(in_library=True, paper=_paper_out(cited), source=edge.source)
        for edge, cited in edges[:limit]
    ]
    items += [
        CitationItemOut(in_library=False, external=_external_out(work), source=link.source)
        for link, work in externals[: max(limit - len(items), 0)]
    ]
    return CitationPage(items=items, has_more=len(edges) + len(externals) > limit)


@router.get("/papers/{ref}/citations", response_model=CitationPage)
async def citations_endpoint(
    ref: str,
    limit: int = Query(default=50, ge=1, le=200),
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> CitationPage:
    paper = await get_paper(session, ref)
    edges = (
        await session.execute(
            select(CitationEdge, Paper)
            .join(Paper, Paper.id == CitationEdge.citing_paper_id)
            .where(
                CitationEdge.cited_paper_id == paper.id,
                Paper.deleted_at.is_(None),
            )
            .order_by(Paper.year.desc())
            .limit(limit + 1)
        )
    ).all()
    externals = (
        await session.execute(
            select(ExternalCitation, ExternalWork)
            .join(ExternalWork, ExternalWork.id == ExternalCitation.external_work_id)
            .where(
                ExternalCitation.paper_id == paper.id,
                ExternalCitation.direction == "cited_by",
            )
            .order_by(ExternalWork.year.desc().nullslast())
            .limit(limit + 1)
        )
    ).all()
    items = [
        CitationItemOut(in_library=True, paper=_paper_out(citing), source=edge.source)
        for edge, citing in edges[:limit]
    ]
    items += [
        CitationItemOut(in_library=False, external=_external_out(work), source=link.source)
        for link, work in externals[: max(limit - len(items), 0)]
    ]
    return CitationPage(items=items, has_more=len(edges) + len(externals) > limit)


@router.post(
    "/citations/sync",
    response_model=CitationSyncCreated,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_endpoint(
    payload: CitationSyncRequest,
    principal: TokenPrincipal = Depends(scoped("admin")),
    session: AsyncSession = Depends(get_session),
) -> CitationSyncCreated:
    if payload.paper_ids:
        papers = list(
            (
                await session.scalars(
                    select(Paper.id).where(
                        Paper.id.in_(payload.paper_ids), Paper.deleted_at.is_(None)
                    )
                )
            ).all()
        )
    else:
        papers = list(
            (
                await session.scalars(
                    select(Paper.id)
                    .where(
                        Paper.deleted_at.is_(None),
                        Paper.citations_synced_at.is_(None),
                    )
                    .limit(500)
                )
            ).all()
        )
    job = await enqueue(
        session,
        "sync_citations",
        {"paper_ids": [str(paper_id) for paper_id in papers]},
    )
    await session.commit()
    return CitationSyncCreated(job_id=job.id, papers=len(papers))
