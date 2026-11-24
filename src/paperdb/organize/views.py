from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.db.models import Paper, SavedView
from paperdb.errors import ApiError
from paperdb.organize.filters import compile_filter, validate_filter


async def list_views(session: AsyncSession) -> list[SavedView]:
    return list(
        (
            await session.scalars(
                select(SavedView).order_by(
                    SavedView.is_pinned.desc(), SavedView.position, func.lower(SavedView.name)
                )
            )
        ).all()
    )


async def get_view(session: AsyncSession, view_id: uuid.UUID) -> SavedView:
    view = await session.get(SavedView, view_id)
    if view is None:
        raise ApiError(404, "not_found", "View not found")
    return view


async def view_count(session: AsyncSession, view: SavedView) -> int:
    condition = compile_filter(view.filter)
    return int(
        await session.scalar(
            select(func.count()).select_from(Paper).where(Paper.deleted_at.is_(None), condition)
        )
        or 0
    )


async def create_view(
    session: AsyncSession,
    *,
    name: str,
    description: str | None,
    filter_payload: dict[str, Any],
    sort: list[dict],
    columns: list[str],
    view_mode: str,
    is_pinned: bool,
    position: int,
) -> SavedView:
    name = name.strip()
    if not name:
        raise ApiError(422, "invalid_view", "name is required")
    validate_filter(filter_payload)
    if await session.scalar(select(SavedView.id).where(func.lower(SavedView.name) == name.lower())):
        raise ApiError(409, "conflict", "A view with this name exists")
    view = SavedView(
        id=uuid.uuid4(),
        name=name,
        description=description,
        filter=filter_payload,
        sort=sort,
        columns=columns,
        view_mode=view_mode,
        is_pinned=is_pinned,
        position=position,
    )
    session.add(view)
    await session.flush()
    return view


async def update_view(session: AsyncSession, view: SavedView, payload: dict[str, Any]) -> SavedView:
    if payload.get("name") is not None:
        name = payload["name"].strip()
        if not name:
            raise ApiError(422, "invalid_view", "name cannot be empty")
        conflict = await session.scalar(
            select(SavedView.id).where(
                func.lower(SavedView.name) == name.lower(), SavedView.id != view.id
            )
        )
        if conflict:
            raise ApiError(409, "conflict", "A view with this name exists")
        view.name = name
    if payload.get("filter") is not None:
        validate_filter(payload["filter"])
        view.filter = payload["filter"]
    for field in ("description", "sort", "columns", "view_mode", "is_pinned", "position"):
        if field in payload and payload[field] is not None:
            setattr(view, field, payload[field])
    await session.flush()
    return view


async def delete_view(session: AsyncSession, view: SavedView) -> None:
    await session.delete(view)
