from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.db.models import (
    AutoRule,
    Paper,
    PaperTag,
    SavedView,
    Tag,
)
from paperdb.errors import ApiError


def _parts(path: str) -> tuple[str, str, list[str]]:
    path = path.strip().strip("/")
    if not path:
        raise ApiError(422, "invalid_tag", "tag path is required")
    if ":" in path and "/" not in path:
        namespace, name = path.split(":", 1)
        return namespace, name, []
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) > 4:
        raise ApiError(422, "invalid_tag", "tag hierarchy depth must be <= 4")
    return segments[0], segments[-1], segments


async def _counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    rows = (
        await session.execute(
            select(PaperTag.tag_id, func.count())
            .join(Paper, Paper.id == PaperTag.paper_id)
            .where(Paper.deleted_at.is_(None))
            .group_by(PaperTag.tag_id)
        )
    ).all()
    return {tag_id: int(count) for tag_id, count in rows}


async def tag_tree(session: AsyncSession) -> list[dict[str, Any]]:
    tags = list((await session.scalars(select(Tag).order_by(Tag.path))).all())
    counts = await _counts(session)
    nodes = {
        item.id: {
            "id": item.id,
            "path": item.path,
            "namespace": item.namespace,
            "name": item.name,
            "parent_id": item.parent_id,
            "color": item.color,
            "description": item.description,
            "paper_count": counts.get(item.id, 0),
            "children": [],
        }
        for item in tags
    }
    roots: list[dict[str, Any]] = []
    for item in tags:
        node = nodes[item.id]
        if item.parent_id and item.parent_id in nodes:
            nodes[item.parent_id]["children"].append(node)
        else:
            roots.append(node)

    def sort_tree(items: list[dict[str, Any]]) -> None:
        items.sort(key=lambda node: node["path"])
        for item in items:
            sort_tree(item["children"])

    sort_tree(roots)
    return roots


async def get_tag(session: AsyncSession, tag_id: uuid.UUID) -> Tag:
    tag = await session.get(Tag, tag_id)
    if tag is None:
        raise ApiError(404, "not_found", "Tag not found")
    return tag


async def ensure_tag_path(
    session: AsyncSession,
    path: str,
    *,
    color: str | None = None,
    description: str | None = None,
) -> Tag:
    namespace, _name, segments = _parts(path)
    cleaned = path.strip().strip("/")
    if not segments:
        tag = await session.scalar(select(Tag).where(Tag.path == cleaned))
        if tag is not None:
            return tag
        tag = Tag(
            id=uuid.uuid4(),
            path=cleaned,
            namespace=namespace,
            name=cleaned.split(":", 1)[-1],
            color=color,
            description=description,
        )
        session.add(tag)
        await session.flush()
        return tag

    parent: Tag | None = None
    current = ""
    for segment in segments:
        current = f"{current}/{segment}" if current else segment
        tag = await session.scalar(select(Tag).where(Tag.path == current))
        if tag is None:
            tag = Tag(
                id=uuid.uuid4(),
                path=current,
                namespace=segments[0],
                name=segment,
                parent_id=parent.id if parent else None,
            )
            session.add(tag)
            await session.flush()
        parent = tag
    if color and parent is not None:
        parent.color = color
    if description and parent is not None:
        parent.description = description
    assert parent is not None
    return parent


async def update_tag(
    session: AsyncSession,
    tag: Tag,
    *,
    name: str | None,
    color: str | None,
    description: str | None,
) -> Tag:
    if name is not None:
        name = name.strip()
        if not name:
            raise ApiError(422, "invalid_tag", "tag name cannot be empty")
        # The path is the stable key; only the display name changes.
        tag.name = name
    if color is not None:
        tag.color = color
    if description is not None:
        tag.description = description
    await session.flush()
    return tag


def _rewrite(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        if value == old:
            return new
        if value.startswith(f"{old}/"):
            return f"{new}{value[len(old) :]}"
        return value
    if isinstance(value, list):
        return [_rewrite(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, old, new) for key, item in value.items()}
    return value


async def merge_tags(session: AsyncSession, source: Tag, target: Tag) -> None:
    if source.id == target.id:
        raise ApiError(422, "invalid_merge", "Cannot merge a tag into itself")
    links = (await session.scalars(select(PaperTag).where(PaperTag.tag_id == source.id))).all()
    existing = set(
        (await session.scalars(select(PaperTag.paper_id).where(PaperTag.tag_id == target.id))).all()
    )
    for link in links:
        await session.delete(link)
        if link.paper_id not in existing:
            session.add(PaperTag(paper_id=link.paper_id, tag_id=target.id))
            existing.add(link.paper_id)
    views = (await session.scalars(select(SavedView))).all()
    for view in views:
        view.filter = _rewrite(view.filter, source.path, target.path)
    rules = (await session.scalars(select(AutoRule))).all()
    for rule in rules:
        rule.filter = _rewrite(rule.filter, source.path, target.path)
        rule.actions = _rewrite(rule.actions, source.path, target.path)
    await session.delete(source)
    await session.flush()


async def delete_tag(session: AsyncSession, tag: Tag) -> None:
    child = await session.scalar(select(Tag.id).where(Tag.parent_id == tag.id))
    if child is not None:
        raise ApiError(422, "tag_has_children", "Delete or promote child tags first")
    await session.execute(delete(PaperTag).where(PaperTag.tag_id == tag.id))
    await session.delete(tag)
