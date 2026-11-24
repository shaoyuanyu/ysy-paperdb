from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import FulltextSearchOut, SearchHitOut
from paperdb.auth.tokens import TokenPrincipal
from paperdb.config import Settings, get_settings
from paperdb.db.session import get_session
from paperdb.pdf.extract import snippet
from paperdb.pdf.service import get_file, search_pages

router = APIRouter(tags=["search"])


@router.get("/files/{file_id}/search", response_model=FulltextSearchOut)
async def file_search_endpoint(
    file_id: uuid.UUID,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    principal: TokenPrincipal = Depends(scoped("pdf:read")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FulltextSearchOut:
    await get_file(session, file_id)
    rows = await search_pages(session, q, file_id=file_id, limit=limit)
    return FulltextSearchOut(
        query=q,
        hits=[
            SearchHitOut(
                paper_id=row["paper_id"],
                paper_title=row["paper_title"],
                file_id=row["file_id"],
                page=row["page"],
                snippet=snippet(row["text"], q),
            )
            for row in rows
        ],
        has_more=len(rows) == limit,
    )


@router.get("/search/fulltext", response_model=FulltextSearchOut)
async def fulltext_endpoint(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FulltextSearchOut:
    rows = await search_pages(session, q, limit=limit)
    return FulltextSearchOut(
        query=q,
        hits=[
            SearchHitOut(
                paper_id=row["paper_id"],
                paper_title=row["paper_title"],
                file_id=row["file_id"],
                page=row["page"],
                snippet=snippet(row["text"], q),
            )
            for row in rows
        ],
        has_more=len(rows) == limit,
    )
