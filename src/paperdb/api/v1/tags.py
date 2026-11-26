from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import require_totp, scoped
from paperdb.api.schemas import TagCreate, TagMerge, TagOut, TagUpdate
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.session import get_session
from paperdb.organize.tags import (
    delete_tag,
    ensure_tag_path,
    get_tag,
    merge_tags,
    tag_tree,
    update_tag,
)

router = APIRouter(prefix="/tags", tags=["tags"])


async def _out(session: AsyncSession, tag_id: uuid.UUID) -> TagOut:
    tag = await get_tag(session, tag_id)
    from paperdb.db.models import Paper, PaperTag

    count = int(
        await session.scalar(
            select(func.count())
            .select_from(PaperTag)
            .join(Paper, Paper.id == PaperTag.paper_id)
            .where(PaperTag.tag_id == tag.id, Paper.deleted_at.is_(None))
        )
        or 0
    )
    return TagOut(
        id=tag.id,
        path=tag.path,
        namespace=tag.namespace,
        name=tag.name,
        parent_id=tag.parent_id,
        color=tag.color,
        description=tag.description,
        paper_count=count,
    )


@router.get("/tree", response_model=list[TagOut])
async def tree_endpoint(
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> list[TagOut]:
    return [TagOut.model_validate(node) for node in await tag_tree(session)]


@router.post("", response_model=TagOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: TagCreate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> TagOut:
    tag = await ensure_tag_path(
        session, payload.path, color=payload.color, description=payload.description
    )
    await session.commit()
    return await _out(session, tag.id)


@router.patch("/{tag_id}", response_model=TagOut)
async def update_endpoint(
    tag_id: uuid.UUID,
    payload: TagUpdate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> TagOut:
    tag = await get_tag(session, tag_id)
    await update_tag(
        session,
        tag,
        name=payload.name,
        color=payload.color,
        description=payload.description,
    )
    await session.commit()
    return await _out(session, tag.id)


@router.post("/{tag_id}/merge", response_model=TagOut)
async def merge_endpoint(
    tag_id: uuid.UUID,
    payload: TagMerge,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> TagOut:
    source = await get_tag(session, tag_id)
    target = await get_tag(session, payload.into_tag_id)
    await merge_tags(session, source, target)
    await session.commit()
    return await _out(session, target.id)


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    tag_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
    totp: None = Depends(require_totp),
) -> Response:
    tag = await get_tag(session, tag_id)
    await delete_tag(session, tag)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
