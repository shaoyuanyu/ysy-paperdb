from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import (
    BulkPatch,
    DuplicateOut,
    EnrichCreated,
    MergeRequest,
    PaperBulkRequest,
    PaperBulkResult,
    PaperCollectionsInput,
    PaperCreate,
    PaperDetail,
    PaperPage,
    PaperSummary,
    PaperTagsInput,
    PaperUpdate,
    StateInput,
    StateOut,
)
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.models import (
    Collection,
    CollectionPaper,
    PaperState,
    PaperTag,
    ReadingStatus,
    SavedView,
)
from paperdb.db.session import get_session
from paperdb.errors import ApiError
from paperdb.jobs.queue import enqueue
from paperdb.organize.tags import ensure_tag_path
from paperdb.papers.bibtex import generate_bibtex
from paperdb.papers.service import (
    create_paper,
    delete_paper,
    find_duplicates,
    get_paper,
    list_papers,
    merge_papers,
    paper_to_detail,
    paper_to_summary,
    ranks_for_venues,
    restore_paper,
    update_paper,
)

router = APIRouter(prefix="/papers", tags=["papers"])


async def _detail(session: AsyncSession, ref: str) -> PaperDetail:
    paper = await get_paper(session, ref)
    ranks = await ranks_for_venues(session, [paper.venue_id] if paper.venue_id else [])
    return PaperDetail.model_validate(paper_to_detail(paper, ranks))


@router.get("", response_model=PaperPage)
async def list_endpoint(
    q: str | None = Query(default=None, max_length=200),
    year: int | None = Query(default=None, ge=1900, le=2100),
    kind: str | None = None,
    venue_id: uuid.UUID | None = Query(default=None, alias="venueId"),
    reading_status: str | None = Query(default=None, alias="status"),
    starred: bool | None = None,
    filter_param: str | None = Query(default=None, alias="filter"),
    view_id: uuid.UUID | None = Query(default=None, alias="view"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> PaperPage:
    filter_payload = None
    if view_id is not None:
        view = await session.get(SavedView, view_id)
        if view is None:
            raise ApiError(404, "not_found", "View not found")
        filter_payload = view.filter
    if filter_param:
        try:
            padded = filter_param + "=" * (-len(filter_param) % 4)
            filter_payload = json.loads(base64.urlsafe_b64decode(padded))
        except Exception as exc:
            raise ApiError(400, "invalid_filter", "filter must be base64url JSON") from exc
    papers, next_cursor, has_more = await list_papers(
        session,
        q=q,
        year=year,
        kind=kind,
        venue_id=venue_id,
        status=reading_status,
        starred=starred,
        filter_payload=filter_payload,
        limit=limit,
        cursor=cursor,
    )
    ranks = await ranks_for_venues(session, [paper.venue_id for paper in papers if paper.venue_id])
    return PaperPage(
        items=[PaperSummary.model_validate(paper_to_summary(paper, ranks)) for paper in papers],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post("", response_model=PaperDetail, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    payload: PaperCreate,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> PaperDetail:
    try:
        paper = await create_paper(session, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(409, "conflict", "Paper conflicts with existing data") from exc
    return await _detail(session, str(paper.id))


@router.patch("/bulk", response_model=PaperBulkResult)
async def bulk_endpoint(
    payload: PaperBulkRequest,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> PaperBulkResult:
    from paperdb.db.models import Tag
    from paperdb.organize.tags import ensure_tag_path

    failed: list[dict[str, str]] = []
    updated = 0
    tag_cache: dict[str, uuid.UUID] = {}
    for paper_id in payload.ids:
        paper = await get_paper(session, str(paper_id))
        patch: BulkPatch = payload.patch
        for path in patch.add_tags:
            if path not in tag_cache:
                tag = await ensure_tag_path(session, path)
                tag_cache[path] = tag.id
            tag_id = tag_cache[path]
            if await session.get(PaperTag, (paper.id, tag_id)) is None:
                session.add(PaperTag(paper_id=paper.id, tag_id=tag_id))
        if patch.remove_tags:
            tag_ids = (
                await session.scalars(select(Tag.id).where(Tag.path.in_(patch.remove_tags)))
            ).all()
            if tag_ids:
                await session.execute(
                    delete(PaperTag).where(
                        PaperTag.paper_id == paper.id, PaperTag.tag_id.in_(list(tag_ids))
                    )
                )
        for collection_id in patch.add_collections:
            collection = await session.get(Collection, collection_id)
            if collection is None:
                continue
            if await session.get(CollectionPaper, (collection_id, paper.id)) is None:
                session.add(CollectionPaper(collection_id=collection_id, paper_id=paper.id))
        if patch.remove_collections:
            await session.execute(
                delete(CollectionPaper).where(
                    CollectionPaper.paper_id == paper.id,
                    CollectionPaper.collection_id.in_(patch.remove_collections),
                )
            )
        if patch.state is not None:
            state = paper.state
            if state is None:
                state = PaperState(paper=paper, status=ReadingStatus.unread, priority=0)
                session.add(state)
            now = datetime.now(UTC)
            if patch.state.status is not None:
                state.status = ReadingStatus(patch.state.status)
            if patch.state.priority is not None:
                state.priority = patch.state.priority
            if patch.state.rating is not None:
                state.rating = patch.state.rating
            if patch.state.starred is not None:
                state.starred_at = now if patch.state.starred else None
            if patch.state.archived is not None:
                state.archived_at = now if patch.state.archived else None
        paper.version += 1
        updated += 1
    await session.commit()
    return PaperBulkResult(updated=updated, failed=failed)


@router.get("/{ref}", response_model=PaperDetail)
async def get_endpoint(
    ref: str,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> PaperDetail:
    return await _detail(session, ref)


@router.patch("/{ref}", response_model=PaperDetail)
async def update_endpoint(
    ref: str,
    payload: PaperUpdate,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> PaperDetail:
    paper = await get_paper(session, ref)
    try:
        await update_paper(session, paper, payload)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(409, "conflict", "Update conflicts with existing data") from exc
    return await _detail(session, str(paper.id))


@router.delete("/{ref}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    ref: str,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    paper = await get_paper(session, ref)
    await delete_paper(session, paper)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{ref}/restore", response_model=PaperDetail)
async def restore_endpoint(
    ref: str,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> PaperDetail:
    paper = await get_paper(session, ref, include_deleted=True)
    if paper.deleted_at is None:
        return await _detail(session, str(paper.id))
    await restore_paper(session, paper)
    await session.commit()
    return await _detail(session, str(paper.id))


def _state_out(state: PaperState | None) -> StateOut:
    if state is None:
        return StateOut(status="unread", priority=0)
    return StateOut(
        status=state.status.value,
        priority=state.priority,
        rating=state.rating,
        progress_page=state.progress_page,
        starred=state.starred_at is not None,
        archived=state.archived_at is not None,
    )


@router.get("/{ref}/state", response_model=StateOut)
async def get_state_endpoint(
    ref: str,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> StateOut:
    paper = await get_paper(session, ref)
    return _state_out(paper.state)


@router.patch("/{ref}/state", response_model=StateOut)
async def update_state_endpoint(
    ref: str,
    payload: StateInput,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> StateOut:
    paper = await get_paper(session, ref)
    state = paper.state
    if state is None:
        state = PaperState(paper=paper, status=ReadingStatus.unread, priority=0)
        session.add(state)
    now = datetime.now(UTC)
    if payload.status is not None:
        state.status = ReadingStatus(payload.status)
    if payload.priority is not None:
        state.priority = payload.priority
    if payload.rating is not None:
        state.rating = payload.rating
    if payload.progress_page is not None:
        state.progress_page = payload.progress_page
    if payload.starred is not None:
        state.starred_at = now if payload.starred else None
    if payload.archived is not None:
        state.archived_at = now if payload.archived else None
    await session.commit()
    return _state_out(state)


@router.get("/{ref}/bibtex")
async def bibtex_endpoint(
    ref: str,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    paper = await get_paper(session, ref)
    return Response(
        content=generate_bibtex(paper),
        media_type="application/x-bibtex",
        headers={"Content-Type": "application/x-bibtex; charset=utf-8"},
    )


@router.get("/{ref}/duplicates", response_model=list[DuplicateOut])
async def duplicates_endpoint(
    ref: str,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> list[DuplicateOut]:
    paper = await get_paper(session, ref)
    duplicates = await find_duplicates(session, paper)
    ranks = await ranks_for_venues(
        session, [item.paper.venue_id for item in duplicates if item.paper.venue_id]
    )
    return [
        DuplicateOut(
            paper=PaperSummary.model_validate(paper_to_summary(item.paper, ranks)),
            score=item.score,
            reasons=item.reasons,
        )
        for item in duplicates
    ]


@router.post("/{ref}/merge", response_model=PaperDetail)
async def merge_endpoint(
    ref: str,
    payload: MergeRequest,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> PaperDetail:
    source = await get_paper(session, ref)
    target = await get_paper(session, str(payload.into))
    fields = payload.fields or {}
    await merge_papers(
        session,
        source,
        target,
        title_from_source=fields.get("title") == "source",
        abstract_from_source=fields.get("abstract") == "source",
    )
    await session.commit()
    return await _detail(session, str(target.id))


@router.post("/{ref}/enrich", response_model=EnrichCreated, status_code=status.HTTP_202_ACCEPTED)
async def enrich_endpoint(
    ref: str,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> EnrichCreated:
    paper = await get_paper(session, ref)
    job = await enqueue(session, "enrich_paper", {"paper_id": str(paper.id)})
    await session.commit()
    return EnrichCreated(job_id=job.id, paper_id=paper.id)


@router.post("/{ref}/tags")
async def add_tags_endpoint(
    ref: str,
    payload: PaperTagsInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    paper = await get_paper(session, ref)
    existing = set(
        (await session.scalars(select(PaperTag.tag_id).where(PaperTag.paper_id == paper.id))).all()
    )
    added = 0
    for path in payload.paths:
        tag = await ensure_tag_path(session, path)
        if tag.id in existing:
            continue
        session.add(PaperTag(paper_id=paper.id, tag_id=tag.id))
        existing.add(tag.id)
        added += 1
    await session.commit()
    return {"added": added}


@router.delete("/{ref}/tags")
async def remove_tags_endpoint(
    ref: str,
    payload: PaperTagsInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    from sqlalchemy import delete

    from paperdb.db.models import Tag

    paper = await get_paper(session, ref)
    tag_ids = (await session.scalars(select(Tag.id).where(Tag.path.in_(payload.paths)))).all()
    result = await session.execute(
        delete(PaperTag).where(PaperTag.paper_id == paper.id, PaperTag.tag_id.in_(list(tag_ids)))
    )
    await session.commit()
    return {"removed": int(getattr(result, "rowcount", 0) or 0)}


@router.post("/{ref}/collections")
async def add_collections_endpoint(
    ref: str,
    payload: PaperCollectionsInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    paper = await get_paper(session, ref)
    existing = set(
        (
            await session.scalars(
                select(CollectionPaper.collection_id).where(CollectionPaper.paper_id == paper.id)
            )
        ).all()
    )
    valid = set(
        (
            await session.scalars(
                select(Collection.id).where(
                    Collection.id.in_(payload.collection_ids),
                    Collection.deleted_at.is_(None),
                )
            )
        ).all()
    )
    added = 0
    for collection_id in payload.collection_ids:
        if collection_id in existing or collection_id not in valid:
            continue
        session.add(CollectionPaper(collection_id=collection_id, paper_id=paper.id))
        added += 1
    await session.commit()
    return {"added": added}


@router.delete("/{ref}/collections")
async def remove_collections_endpoint(
    ref: str,
    payload: PaperCollectionsInput,
    principal: TokenPrincipal = Depends(scoped("organize:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    from sqlalchemy import delete

    paper = await get_paper(session, ref)
    result = await session.execute(
        delete(CollectionPaper).where(
            CollectionPaper.paper_id == paper.id,
            CollectionPaper.collection_id.in_(payload.collection_ids),
        )
    )
    await session.commit()
    return {"removed": int(getattr(result, "rowcount", 0) or 0)}
