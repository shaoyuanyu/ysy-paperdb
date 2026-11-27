from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from paperdb.db.models import (
    Collection,
    CollectionPaper,
    Paper,
    Project,
    ProjectCollection,
    ProjectPaper,
)
from paperdb.errors import ApiError
from paperdb.organize.collections import descendant_ids


def _options():
    return (
        selectinload(Project.collections),
        selectinload(Project.papers),
    )


async def list_projects(session: AsyncSession) -> list[Project]:
    return list(
        (
            await session.scalars(
                select(Project)
                .options(*_options())
                .where(Project.archived_at.is_(None))
                .order_by(func.lower(Project.name))
            )
        ).all()
    )


async def get_project(session: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await session.scalar(
        select(Project).options(*_options()).where(Project.id == project_id)
    )
    if project is None:
        raise ApiError(404, "not_found", "Project not found")
    return project


async def project_paper_ids(session: AsyncSession, project: Project) -> list[uuid.UUID]:
    collection_ids: set[uuid.UUID] = set()
    for link in project.collections:
        collection_ids.add(link.collection_id)
        collection_ids.update(await descendant_ids(session, link.collection_id))
    ids: set[uuid.UUID] = set()
    if collection_ids:
        rows = await session.scalars(
            select(CollectionPaper.paper_id)
            .join(Paper, Paper.id == CollectionPaper.paper_id)
            .where(
                CollectionPaper.collection_id.in_(collection_ids),
                Paper.deleted_at.is_(None),
                Paper.staged.is_(False),
            )
        )
        ids.update(rows.all())
    pinned = await session.scalars(
        select(ProjectPaper.paper_id)
        .join(Paper, Paper.id == ProjectPaper.paper_id)
        .where(
            ProjectPaper.project_id == project.id,
            Paper.deleted_at.is_(None),
            Paper.staged.is_(False),
        )
    )
    ids.update(pinned.all())
    return list(ids)


async def project_count(session: AsyncSession, project: Project) -> int:
    return len(await project_paper_ids(session, project))


async def create_project(
    session: AsyncSession,
    *,
    name: str,
    description: str | None,
    citekey_template: str | None,
    export_include_abstract: bool,
) -> Project:
    name = name.strip()
    if not name:
        raise ApiError(422, "invalid_project", "name is required")
    if await session.scalar(select(Project.id).where(func.lower(Project.name) == name.lower())):
        raise ApiError(409, "conflict", "A project with this name exists")
    project = Project(
        id=uuid.uuid4(),
        name=name,
        description=description,
        citekey_template=citekey_template,
        export_include_abstract=export_include_abstract,
    )
    session.add(project)
    await session.flush()
    return project


async def update_project(session: AsyncSession, project: Project, payload: dict) -> Project:
    if payload.get("name") is not None:
        name = payload["name"].strip()
        if not name:
            raise ApiError(422, "invalid_project", "name cannot be empty")
        conflict = await session.scalar(
            select(Project.id).where(
                func.lower(Project.name) == name.lower(), Project.id != project.id
            )
        )
        if conflict:
            raise ApiError(409, "conflict", "A project with this name exists")
        project.name = name
    for field in ("description", "citekey_template", "export_include_abstract"):
        if field in payload and payload[field] is not None:
            setattr(project, field, payload[field])
    await session.flush()
    return project


async def delete_project(session: AsyncSession, project: Project) -> None:
    await session.delete(project)


async def add_collections(
    session: AsyncSession, project: Project, collection_ids: list[uuid.UUID]
) -> int:
    existing = {link.collection_id for link in project.collections}
    valid = set(
        (
            await session.scalars(
                select(Collection.id).where(
                    Collection.id.in_(collection_ids), Collection.deleted_at.is_(None)
                )
            )
        ).all()
    )
    added = 0
    for collection_id in collection_ids:
        if collection_id in existing or collection_id not in valid:
            continue
        project.collections.append(
            ProjectCollection(project_id=project.id, collection_id=collection_id)
        )
        added += 1
    await session.flush()
    return added


async def remove_collections(
    session: AsyncSession, project: Project, collection_ids: list[uuid.UUID]
) -> int:
    remove = set(collection_ids)
    removed = 0
    for link in list(project.collections):
        if link.collection_id in remove:
            await session.delete(link)
            removed += 1
    await session.flush()
    return removed


async def add_papers(session: AsyncSession, project: Project, paper_ids: list[uuid.UUID]) -> int:
    existing = {link.paper_id for link in project.papers}
    valid = set(
        (
            await session.scalars(
                select(Paper.id).where(Paper.id.in_(paper_ids), Paper.deleted_at.is_(None))
            )
        ).all()
    )
    next_position = max((link.position for link in project.papers), default=0) + 1
    added = 0
    for paper_id in paper_ids:
        if paper_id in existing or paper_id not in valid:
            continue
        project.papers.append(
            ProjectPaper(project_id=project.id, paper_id=paper_id, position=next_position)
        )
        next_position += 1
        added += 1
    await session.flush()
    return added


async def remove_papers(session: AsyncSession, project: Project, paper_ids: list[uuid.UUID]) -> int:
    remove = set(paper_ids)
    removed = 0
    for link in list(project.papers):
        if link.paper_id in remove:
            await session.delete(link)
            removed += 1
    await session.flush()
    return removed
