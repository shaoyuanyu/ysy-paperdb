from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from paperdb.api.schemas import (
    AuthorDetail,
    VenueAliasOut,
    VenueDetail,
    VenueEditionOut,
    VenueIdentifierOut,
    VenueRankOut,
    VenueSummary,
)
from paperdb.config import Settings
from paperdb.db.models import (
    Author,
    AuthorAlias,
    DatasetVersion,
    Paper,
    PaperAuthor,
    Venue,
    VenueKind,
    VenueStatus,
)
from paperdb.errors import ApiError
from paperdb.storage import get_storage


def _encode_cursor(key: str, item_id: uuid.UUID) -> str:
    raw = json.dumps({"k": key, "id": str(item_id)}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return str(payload["k"]), uuid.UUID(payload["id"])
    except Exception as exc:
        raise ApiError(400, "invalid_cursor", "Invalid cursor") from exc


async def list_venues(
    session: AsyncSession,
    *,
    q: str | None,
    kind: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[Venue], str | None, bool]:
    stmt = select(Venue).where(Venue.merged_into_id.is_(None))
    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Venue.full_name).like(pattern),
                func.lower(func.coalesce(Venue.abbrev, "")).like(pattern),
            )
        )
    if kind:
        try:
            stmt = stmt.where(Venue.kind == VenueKind(kind))
        except ValueError as exc:
            raise ApiError(422, "invalid_kind", f"Unsupported venue kind: {kind}") from exc
    if cursor:
        key, venue_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Venue.normalized_name > key,
                and_(Venue.normalized_name == key, Venue.id > venue_id),
            )
        )
    stmt = stmt.order_by(Venue.normalized_name.asc(), Venue.id.asc()).limit(limit + 1)
    rows = list((await session.scalars(stmt)).all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        _encode_cursor(items[-1].normalized_name, items[-1].id) if has_more and items else None
    )
    return items, next_cursor, has_more


async def venue_paper_count(session: AsyncSession, venue_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Paper)
            .where(Paper.venue_id == venue_id, Paper.deleted_at.is_(None))
        )
        or 0
    )


async def get_venue(session: AsyncSession, venue_id: uuid.UUID) -> Venue:
    stmt = (
        select(Venue)
        .options(
            selectinload(Venue.aliases),
            selectinload(Venue.identifiers),
            selectinload(Venue.editions),
            selectinload(Venue.ranks),
        )
        .where(Venue.id == venue_id)
    )
    venue = await session.scalar(stmt)
    if venue is None:
        raise ApiError(404, "not_found", "Venue not found")
    return venue


def venue_summary(venue: Venue) -> VenueSummary:
    return VenueSummary(
        id=venue.id,
        kind=venue.kind.value,
        abbrev=venue.abbrev,
        full_name=venue.full_name,
        ccf_field=venue.ccf_field,
        ccf_field_key=venue.ccf_field_key,
        status=venue.status.value,
    )


async def venue_detail(session: AsyncSession, venue: Venue) -> VenueDetail:
    ranks = [
        VenueRankOut(
            id=rank.id,
            scheme=rank.scheme.value,
            dataset_version=rank.dataset_version,
            field_kind=rank.field_kind.value,
            field_name=rank.field_name,
            field_name_en=rank.field_name_en,
            field_key=rank.field_key,
            level=rank.level,
            is_top=rank.is_top,
            is_primary=rank.is_primary,
            rank_in_field=rank.rank_in_field,
            total_in_field=rank.total_in_field,
        )
        for rank in sorted(
            venue.ranks,
            key=lambda item: (item.scheme.value, item.dataset_version, item.field_kind.value),
        )
    ]
    return VenueDetail(
        id=venue.id,
        kind=venue.kind.value,
        abbrev=venue.abbrev,
        full_name=venue.full_name,
        ccf_field=venue.ccf_field,
        ccf_field_key=venue.ccf_field_key,
        status=venue.status.value,
        dblp_key=venue.dblp_key,
        issn_print=venue.issn_print,
        issn_online=venue.issn_online,
        homepage_url=venue.homepage_url,
        aliases=[
            VenueAliasOut(id=alias.id, alias=alias.alias, source=alias.source)
            for alias in sorted(venue.aliases, key=lambda item: item.alias)
        ],
        identifiers=[
            VenueIdentifierOut(
                id=identifier.id,
                source=identifier.source,
                value=identifier.value,
                is_primary=identifier.is_primary,
            )
            for identifier in venue.identifiers
        ],
        editions=[
            VenueEditionOut(
                id=edition.id,
                year=edition.year,
                track=edition.track.value,
                ordinal=edition.ordinal,
            )
            for edition in sorted(venue.editions, key=lambda item: (item.year, item.track.value))
        ],
        ranks=ranks,
        paper_count=await venue_paper_count(session, venue.id),
    )


async def list_authors(
    session: AsyncSession,
    *,
    q: str | None,
    is_me: bool | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[Author], str | None, bool]:
    stmt = select(Author).where(Author.merged_into_id.is_(None))
    if q:
        stmt = stmt.where(func.lower(Author.display_name).like(f"%{q.lower()}%"))
    if is_me is not None:
        stmt = stmt.where(Author.is_me.is_(is_me))
    if cursor:
        key, author_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                func.lower(Author.display_name) > key,
                and_(func.lower(Author.display_name) == key, Author.id > author_id),
            )
        )
    stmt = stmt.order_by(func.lower(Author.display_name), Author.id).limit(limit + 1)
    rows = list((await session.scalars(stmt)).all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = (
        _encode_cursor(items[-1].display_name.lower(), items[-1].id) if has_more and items else None
    )
    return items, next_cursor, has_more


async def author_paper_count(session: AsyncSession, author_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(PaperAuthor)
            .join(Paper, Paper.id == PaperAuthor.paper_id)
            .where(PaperAuthor.author_id == author_id, Paper.deleted_at.is_(None))
        )
        or 0
    )


async def author_detail(session: AsyncSession, author: Author) -> AuthorDetail:
    aliases = (
        await session.scalars(select(AuthorAlias).where(AuthorAlias.author_id == author.id))
    ).all()
    return AuthorDetail(
        id=author.id,
        display_name=author.display_name,
        family_name=author.family_name,
        given_name=author.given_name,
        orcid=author.orcid,
        dblp_pid=author.dblp_pid,
        s2_author_id=author.s2_author_id,
        openalex_id=author.openalex_id,
        homepage_url=author.homepage_url,
        is_me=author.is_me,
        paper_count=await author_paper_count(session, author.id),
        aliases=[alias.name for alias in aliases],
    )


async def get_author(session: AsyncSession, author_id: uuid.UUID) -> Author:
    author = await session.get(Author, author_id)
    if author is None or author.merged_into_id is not None:
        raise ApiError(404, "not_found", "Author not found")
    return author


async def list_datasets(session: AsyncSession) -> list[DatasetVersion]:
    return list(
        (
            await session.scalars(
                select(DatasetVersion).order_by(
                    DatasetVersion.scheme.asc(), DatasetVersion.version.desc()
                )
            )
        ).all()
    )


async def library_counts(session: AsyncSession) -> dict[str, int]:
    papers = int(
        await session.scalar(
            select(func.count()).select_from(Paper).where(Paper.deleted_at.is_(None))
        )
        or 0
    )
    venues = int(
        await session.scalar(
            select(func.count()).select_from(Venue).where(Venue.merged_into_id.is_(None))
        )
        or 0
    )
    authors = int(
        await session.scalar(
            select(func.count()).select_from(Author).where(Author.merged_into_id.is_(None))
        )
        or 0
    )
    return {"papers": papers, "venues": venues, "authors": authors, "files": 0}


async def venue_gaps(session: AsyncSession) -> dict[str, int]:
    provisional = int(
        await session.scalar(
            select(func.count())
            .select_from(Venue)
            .where(Venue.status == VenueStatus.provisional, Venue.merged_into_id.is_(None))
        )
        or 0
    )
    missing_issn = int(
        await session.scalar(
            select(func.count())
            .select_from(Venue)
            .where(
                Venue.kind == VenueKind.journal,
                Venue.issn_print.is_(None),
                Venue.issn_online.is_(None),
                Venue.merged_into_id.is_(None),
            )
        )
        or 0
    )
    return {"unmatched": provisional, "missingIssn": missing_issn}


def _walk_storage(root: Path) -> tuple[int, int]:
    if not root.exists():
        return 0, 0
    files = 0
    size = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            files += 1
            size += path.stat().st_size
    return files, size


async def storage_meta(settings: Settings) -> dict[str, Any]:
    backend = get_storage()
    # Local-only introspection; remote backends will report counters from DB later.
    if settings.storage_backend == "local":
        import asyncio

        files, size = await asyncio.to_thread(_walk_storage, settings.storage_root)
    else:
        files, size = 0, 0
    return {
        "backend": backend.name,
        "bytes_used": size,
        "files": files,
        "blobs": files,
    }
