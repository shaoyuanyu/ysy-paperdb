from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import FulltextSearchOut, SearchHitOut
from paperdb.auth.tokens import TokenPrincipal
from paperdb.config import Settings, get_settings
from paperdb.db.session import get_session
from paperdb.errors import ApiError
from paperdb.pdf.extract import snippet
from paperdb.pdf.service import get_file, search_pages

router = APIRouter(tags=["search"])


def _out(rows: list[dict], query: str, context_chars: int, limit: int) -> FulltextSearchOut:
    return FulltextSearchOut(
        query=query,
        hits=[
            SearchHitOut(
                paper_id=row["paper_id"],
                paper_title=row["paper_title"],
                file_id=row["file_id"],
                page=row["page"],
                snippet=snippet(row["text"], query, radius=context_chars),
            )
            for row in rows
        ],
        has_more=len(rows) >= limit,
    )


@router.get("/files/{file_id}/search", response_model=FulltextSearchOut)
async def file_search_endpoint(
    file_id: uuid.UUID,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    context_chars: int = Query(default=240, ge=60, le=2000, alias="contextChars"),
    principal: TokenPrincipal = Depends(scoped("pdf:read")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FulltextSearchOut:
    await get_file(session, file_id)
    rows = await search_pages(session, q, file_id=file_id, limit=limit)
    return _out(rows, q, context_chars, limit)


@router.get("/search/fulltext", response_model=FulltextSearchOut)
async def fulltext_endpoint(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    include_staged: bool = Query(default=False, alias="includeStaged"),
    file_id: uuid.UUID | None = Query(default=None, alias="fileId"),
    paper_id: uuid.UUID | None = Query(default=None, alias="paperId"),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    max_per_paper: int | None = Query(default=None, ge=1, le=20, alias="maxPerPaper"),
    context_chars: int = Query(default=240, ge=60, le=2000, alias="contextChars"),
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> FulltextSearchOut:
    """Full-text search inside extracted PDF text.

    ``fileId`` / ``paperId`` / ``workspaceId`` narrow the search to one file,
    one paper, or one research workspace. Any explicit scope also reaches
    staged papers — reading a workspace is the point of having one — while the
    unscoped library search keeps staging hidden unless ``includeStaged``.
    """
    if sum(scope is not None for scope in (file_id, paper_id, workspace_id)) > 1:
        raise ApiError(
            422, "ambiguous_scope", "fileId, paperId and workspaceId are mutually exclusive"
        )
    rows = await search_pages(
        session,
        q,
        file_id=file_id,
        paper_id=paper_id,
        workspace_id=workspace_id,
        limit=limit,
        include_staged=include_staged,
        max_per_paper=max_per_paper,
    )
    return _out(rows, q, context_chars, limit)
