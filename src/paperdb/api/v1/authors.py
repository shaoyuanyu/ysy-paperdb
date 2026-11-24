from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import AuthorCreate, AuthorDetail, AuthorPage, AuthorUpdate
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.session import get_session
from paperdb.venues.service import (
    author_detail,
    create_author,
    get_author,
    list_authors,
    update_author,
)

router = APIRouter(prefix="/authors", tags=["authors"])


@router.get("", response_model=AuthorPage)
async def list_endpoint(
    q: str | None = Query(default=None, max_length=200),
    is_me: bool | None = Query(default=None, alias="isMe"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> AuthorPage:
    authors, next_cursor, has_more = await list_authors(
        session, q=q, is_me=is_me, limit=limit, cursor=cursor
    )
    return AuthorPage(
        items=[await author_detail(session, author) for author in authors],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/{author_id}", response_model=AuthorDetail)
async def get_endpoint(
    author_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> AuthorDetail:
    author = await get_author(session, author_id)
    return await author_detail(session, author)


@router.post("", response_model=AuthorDetail, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: AuthorCreate,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> AuthorDetail:
    author = await create_author(session, payload)
    await session.commit()
    return await author_detail(session, author)


@router.patch("/{author_id}", response_model=AuthorDetail)
async def update_endpoint(
    author_id: uuid.UUID,
    payload: AuthorUpdate,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> AuthorDetail:
    author = await get_author(session, author_id)
    await update_author(session, author, payload)
    await session.commit()
    return await author_detail(session, author)
