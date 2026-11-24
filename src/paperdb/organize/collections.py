from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.db.models import Collection, CollectionPaper, Paper
from paperdb.errors import ApiError


async def _counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    rows = (
        await session.execute(
            select(CollectionPaper.collection_id, func.count())
            .join(Paper, Paper.id == CollectionPaper.paper_id)
            .where(Paper.deleted_at.is_(None))
            .group_by(CollectionPaper.collection_id)
        )
    ).all()
    return {collection_id: int(count) for collection_id, count in rows}


async def collection_tree(session: AsyncSession) -> list[dict[str, Any]]:
    collections = list(
        (
            await session.scalars(
                select(Collection)
                .where(Collection.deleted_at.is_(None))
                .order_by(Collection.position, func.lower(Collection.name))
            )
        ).all()
    )
    counts = await _counts(session)
    nodes = {
        item.id: {
            "id": item.id,
            "parent_id": item.parent_id,
            "name": item.name,
            "slug": item.slug,
            "description": item.description,
            "position": item.position,
            "paper_count": counts.get(item.id, 0),
            "total_count": counts.get(item.id, 0),
            "children": [],
        }
        for item in collections
    }
    roots: list[dict[str, Any]] = []
    for item in collections:
        node = nodes[item.id]
        if item.parent_id and item.parent_id in nodes:
            nodes[item.parent_id]["children"].append(node)
        else:
            roots.append(node)

    def totals(node: dict[str, Any]) -> int:
        node["total_count"] = node["paper_count"] + sum(totals(child) for child in node["children"])
        return node["total_count"]

    for root in roots:
        totals(root)
    return roots


async def get_collection(session: AsyncSession, collection_id: uuid.UUID) -> Collection:
    collection = await session.get(Collection, collection_id)
    if collection is None or collection.deleted_at is not None:
        raise ApiError(404, "not_found", "Collection not found")
    return collection


async def descendant_ids(session: AsyncSession, root_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (await session.scalars(select(Collection).where(Collection.deleted_at.is_(None)))).all()
    by_parent: dict[uuid.UUID | None, list[Collection]] = defaultdict(list)
    for row in rows:
        by_parent[row.parent_id].append(row)
    found: list[uuid.UUID] = []
    stack = list(by_parent.get(root_id, []))
    while stack:
        item = stack.pop()
        found.append(item.id)
        stack.extend(by_parent.get(item.id, []))
    return found


async def _sibling_name_taken(
    session: AsyncSession,
    parent_id: uuid.UUID | None,
    name: str,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    stmt = select(Collection.id).where(
        func.lower(Collection.name) == name.strip().lower(),
        Collection.deleted_at.is_(None),
    )
    stmt = (
        stmt.where(Collection.parent_id == parent_id)
        if parent_id
        else stmt.where(Collection.parent_id.is_(None))
    )
    if exclude_id:
        stmt = stmt.where(Collection.id != exclude_id)
    return await session.scalar(stmt) is not None


async def create_collection(
    session: AsyncSession,
    *,
    parent_id: uuid.UUID | None,
    name: str,
    slug: str | None = None,
    description: str | None = None,
) -> Collection:
    name = name.strip()
    if not name:
        raise ApiError(422, "invalid_collection", "name is required")
    if parent_id is not None:
        await get_collection(session, parent_id)
    if await _sibling_name_taken(session, parent_id, name):
        raise ApiError(409, "conflict", "A sibling collection with this name exists")
    position = int(
        await session.scalar(
            select(func.coalesce(func.max(Collection.position), -1) + 1).where(
                Collection.parent_id == parent_id if parent_id else Collection.parent_id.is_(None)
            )
        )
        or 0
    )
    collection = Collection(
        id=uuid.uuid4(),
        parent_id=parent_id,
        name=name,
        slug=slug,
        description=description,
        position=position,
    )
    session.add(collection)
    await session.flush()
    return collection


async def update_collection(
    session: AsyncSession,
    collection: Collection,
    *,
    name: str | None,
    slug: str | None,
    description: str | None,
) -> Collection:
    if name is not None:
        name = name.strip()
        if not name:
            raise ApiError(422, "invalid_collection", "name cannot be empty")
        if await _sibling_name_taken(session, collection.parent_id, name, collection.id):
            raise ApiError(409, "conflict", "A sibling collection with this name exists")
        collection.name = name
    if slug is not None:
        collection.slug = slug
    if description is not None:
        collection.description = description
    await session.flush()
    return collection


async def move_collection(
    session: AsyncSession,
    collection: Collection,
    *,
    parent_id: uuid.UUID | None,
    position: int,
) -> Collection:
    if parent_id == collection.id:
        raise ApiError(422, "invalid_move", "Collection cannot be its own parent")
    if parent_id is not None:
        await get_collection(session, parent_id)
        if parent_id in await descendant_ids(session, collection.id):
            raise ApiError(422, "invalid_move", "Cannot move a collection under its descendant")
    if await _sibling_name_taken(session, parent_id, collection.name, collection.id):
        raise ApiError(409, "conflict", "A sibling collection with this name exists")
    collection.parent_id = parent_id
    collection.position = position
    await session.flush()
    return collection


async def delete_collection(
    session: AsyncSession, collection: Collection, *, mode: str = "promote"
) -> None:
    children = list(
        (
            await session.scalars(select(Collection).where(Collection.parent_id == collection.id))
        ).all()
    )
    if mode == "promote":
        for child in children:
            child.parent_id = collection.parent_id
    elif mode == "discard":

        async def discard(item: Collection) -> None:
            for child in (
                await session.scalars(select(Collection).where(Collection.parent_id == item.id))
            ).all():
                await discard(child)
            await session.execute(
                delete(CollectionPaper).where(CollectionPaper.collection_id == item.id)
            )
            await session.delete(item)

        await discard(collection)
        return
    else:
        raise ApiError(422, "invalid_mode", "mode must be promote or discard")
    await session.execute(
        delete(CollectionPaper).where(CollectionPaper.collection_id == collection.id)
    )
    await session.delete(collection)


async def add_papers(
    session: AsyncSession,
    collection: Collection,
    paper_ids: list[uuid.UUID],
    *,
    position: int | None = None,
) -> int:
    existing = set(
        (
            await session.scalars(
                select(CollectionPaper.paper_id).where(
                    CollectionPaper.collection_id == collection.id,
                    CollectionPaper.paper_id.in_(paper_ids),
                )
            )
        ).all()
    )
    valid = set(
        (
            await session.scalars(
                select(Paper.id).where(Paper.id.in_(paper_ids), Paper.deleted_at.is_(None))
            )
        ).all()
    )
    added = 0
    for paper_id in paper_ids:
        if paper_id in existing or paper_id not in valid:
            continue
        session.add(
            CollectionPaper(
                collection_id=collection.id,
                paper_id=paper_id,
                position=position,
            )
        )
        added += 1
    await session.flush()
    return added


async def remove_papers(
    session: AsyncSession, collection: Collection, paper_ids: list[uuid.UUID]
) -> int:
    result = await session.execute(
        delete(CollectionPaper).where(
            CollectionPaper.collection_id == collection.id,
            CollectionPaper.paper_id.in_(paper_ids),
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def reorder_papers(
    session: AsyncSession, collection: Collection, paper_ids: list[uuid.UUID]
) -> None:
    for position, paper_id in enumerate(paper_ids):
        row = await session.get(CollectionPaper, (collection.id, paper_id))
        if row is not None:
            row.position = position
    await session.flush()
