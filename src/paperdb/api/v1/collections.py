from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import require_totp, scoped
from paperdb.api.schemas import (
    CollectionCreate,
    CollectionMove,
    CollectionOut,
    CollectionPapers,
    CollectionUpdate,
)
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.session import get_session
from paperdb.organize.collections import (
    add_papers,
    collection_tree,
    create_collection,
    delete_collection,
    get_collection,
    move_collection,
    reorder_papers,
    update_collection,
)

router = APIRouter(prefix="/collections", tags=["collections"])


@router.get("/tree", response_model=list[CollectionOut])
async def tree_endpoint(
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> list[CollectionOut]:
    return [CollectionOut.model_validate(node) for node in await collection_tree(session)]


@router.post("", response_model=CollectionOut, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: CollectionCreate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    collection = await create_collection(
        session,
        parent_id=payload.parent_id,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
    )
    await session.commit()
    return CollectionOut(
        id=collection.id,
        parent_id=collection.parent_id,
        name=collection.name,
        slug=collection.slug,
        description=collection.description,
        position=collection.position,
    )


@router.patch("/{collection_id}", response_model=CollectionOut)
async def update_endpoint(
    collection_id: uuid.UUID,
    payload: CollectionUpdate,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    collection = await get_collection(session, collection_id)
    await update_collection(
        session,
        collection,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
    )
    await session.commit()
    return CollectionOut(
        id=collection.id,
        parent_id=collection.parent_id,
        name=collection.name,
        slug=collection.slug,
        description=collection.description,
        position=collection.position,
    )


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    collection_id: uuid.UUID,
    mode: str = Query(default="promote", pattern="^(promote|discard)$"),
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
    totp: None = Depends(require_totp),
) -> Response:
    collection = await get_collection(session, collection_id)
    await delete_collection(session, collection, mode=mode)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{collection_id}/move", response_model=CollectionOut)
async def move_endpoint(
    collection_id: uuid.UUID,
    payload: CollectionMove,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    collection = await get_collection(session, collection_id)
    await move_collection(
        session, collection, parent_id=payload.parent_id, position=payload.position
    )
    await session.commit()
    return CollectionOut(
        id=collection.id,
        parent_id=collection.parent_id,
        name=collection.name,
        slug=collection.slug,
        description=collection.description,
        position=collection.position,
    )


@router.post("/{collection_id}/papers")
async def add_papers_endpoint(
    collection_id: uuid.UUID,
    payload: CollectionPapers,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    collection = await get_collection(session, collection_id)
    added = await add_papers(session, collection, payload.paper_ids, position=payload.position)
    await session.commit()
    return {"added": added}


@router.delete("/{collection_id}/papers")
async def remove_papers_endpoint(
    collection_id: uuid.UUID,
    payload: CollectionPapers,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    collection = await get_collection(session, collection_id)
    from paperdb.organize.collections import remove_papers

    removed = await remove_papers(session, collection, payload.paper_ids)
    await session.commit()
    return {"removed": removed}


@router.post("/{collection_id}/reorder")
async def reorder_endpoint(
    collection_id: uuid.UUID,
    payload: CollectionPapers,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    collection = await get_collection(session, collection_id)
    await reorder_papers(session, collection, payload.paper_ids)
    await session.commit()
    return {"ok": True}
