from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import (
    FilterValidateOut,
    FilterValidateRequest,
    SavedViewCreate,
    SavedViewOut,
    SavedViewUpdate,
    ViewCountOut,
)
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.session import get_session
from paperdb.organize.filters import validate_filter
from paperdb.organize.views import (
    create_view,
    delete_view,
    get_view,
    list_views,
    update_view,
    view_count,
)

router = APIRouter(prefix="/views", tags=["views"])


async def _out(session: AsyncSession, view) -> SavedViewOut:
    return SavedViewOut(
        id=view.id,
        name=view.name,
        description=view.description,
        filter=view.filter,
        sort=view.sort,
        columns=view.columns,
        view_mode=view.view_mode,
        is_pinned=view.is_pinned,
        position=view.position,
        count=await view_count(session, view),
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@router.get("", response_model=list[SavedViewOut])
async def list_endpoint(
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> list[SavedViewOut]:
    return [await _out(session, view) for view in await list_views(session)]


@router.post("", response_model=SavedViewOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: SavedViewCreate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> SavedViewOut:
    view = await create_view(
        session,
        name=payload.name,
        description=payload.description,
        filter_payload=payload.filter,
        sort=payload.sort,
        columns=payload.columns,
        view_mode=payload.view_mode,
        is_pinned=payload.is_pinned,
        position=payload.position,
    )
    await session.commit()
    return await _out(session, view)


@router.post("/validate", response_model=FilterValidateOut)
async def validate_endpoint(
    payload: FilterValidateRequest,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
) -> FilterValidateOut:
    validate_filter(payload.filter)
    return FilterValidateOut(valid=True, rules=len(payload.filter.get("rules") or []))


@router.get("/{view_id}", response_model=SavedViewOut)
async def get_endpoint(
    view_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> SavedViewOut:
    return await _out(session, await get_view(session, view_id))


@router.patch("/{view_id}", response_model=SavedViewOut)
async def update_endpoint(
    view_id: uuid.UUID,
    payload: SavedViewUpdate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> SavedViewOut:
    view = await get_view(session, view_id)
    await update_view(session, view, payload.model_dump(exclude_unset=True))
    await session.commit()
    return await _out(session, view)


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    view_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await delete_view(session, await get_view(session, view_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{view_id}/count", response_model=ViewCountOut)
async def count_endpoint(
    view_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> ViewCountOut:
    view = await get_view(session, view_id)
    return ViewCountOut(count=await view_count(session, view))
