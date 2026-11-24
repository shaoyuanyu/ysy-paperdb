from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import VenueDetail, VenuePage, VenueRankOut
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.session import get_session
from paperdb.venues.service import get_venue, list_venues, venue_detail, venue_summary

router = APIRouter(prefix="/venues", tags=["venues"])


@router.get("", response_model=VenuePage)
async def list_endpoint(
    q: str | None = Query(default=None, max_length=200),
    kind: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> VenuePage:
    venues, next_cursor, has_more = await list_venues(
        session, q=q, kind=kind, limit=limit, cursor=cursor
    )
    return VenuePage(
        items=[venue_summary(venue) for venue in venues],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/{venue_id}", response_model=VenueDetail)
async def get_endpoint(
    venue_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> VenueDetail:
    venue = await get_venue(session, venue_id)
    return await venue_detail(session, venue)


@router.get("/{venue_id}/ranks", response_model=list[VenueRankOut])
async def ranks_endpoint(
    venue_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> list[VenueRankOut]:
    venue = await get_venue(session, venue_id)
    detail = await venue_detail(session, venue)
    return detail.ranks
