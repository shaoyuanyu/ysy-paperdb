from __future__ import annotations

import base64
import json
import re
import unicodedata
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from paperdb.api.schemas import (
    PaperAuthorInput,
    PaperCreate,
    PaperUpdate,
    StateInput,
)
from paperdb.db.models import (
    Author,
    AuthorStatus,
    IdentifierScheme,
    Paper,
    PaperAuthor,
    PaperIdentifier,
    PaperKind,
    PaperState,
    ReadingStatus,
    Venue,
    VenueEdition,
    VenueRank,
)
from paperdb.errors import ApiError
from paperdb.venues.rank import resolve_evaluation

PAPER_OPTIONS = (
    selectinload(Paper.venue),
    selectinload(Paper.venue_edition),
    selectinload(Paper.authors).selectinload(PaperAuthor.author),
    selectinload(Paper.identifiers),
    selectinload(Paper.state),
)

_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "via",
    "with",
}
_CITEKEY_RE = re.compile(r"^[a-z][a-z0-9]{0,63}$")


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())


def _ascii(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", ascii_text.lower())


def title_word(title: str, index: int = 1) -> str:
    words = [word for word in re.findall(r"[A-Za-z0-9]+", title) if word.lower() not in _STOPWORDS]
    if not words:
        return "paper"
    return words[min(index, len(words)) - 1]


def _author_cite_part(author: PaperAuthorInput, created: Author | None) -> str:
    if author.family_name:
        return _ascii(author.family_name)
    if created and created.family_name:
        return _ascii(created.family_name)
    name = author.display_name or (created.display_name if created else "")
    return _ascii(name.split(" ")[-1])


def normalize_identifier(scheme: IdentifierScheme, value: str) -> str:
    raw = value.strip()
    if scheme == IdentifierScheme.doi:
        raw = raw.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
        raw = raw.removeprefix("doi:").strip().lower()
    elif scheme == IdentifierScheme.arxiv:
        raw = raw.removeprefix("arXiv:").removeprefix("arxiv:").strip()
        raw = re.sub(r"v\d+$", "", raw)
    elif scheme == IdentifierScheme.openalex:
        raw = raw.rstrip("/").rsplit("/", 1)[-1].upper()
    elif scheme == IdentifierScheme.dblp:
        raw = raw.removeprefix("https://dblp.org/rec/").removeprefix("https://dblp.org/")
        raw = raw.removesuffix(".html")
    elif scheme == IdentifierScheme.s2:
        raw = raw.removeprefix("CorpusId:").strip()
    elif scheme == IdentifierScheme.acl:
        raw = raw.rstrip("/").rsplit("/", 1)[-1].lower()
    if not raw:
        raise ApiError(422, "invalid_identifier", "Identifier is empty")
    return raw


def _parse_kind(kind: str) -> PaperKind:
    try:
        return PaperKind(kind)
    except ValueError as exc:
        raise ApiError(422, "invalid_kind", f"Unsupported paper kind: {kind}") from exc


def _parse_reading_status(value: str) -> ReadingStatus:
    try:
        return ReadingStatus(value)
    except ValueError as exc:
        raise ApiError(422, "invalid_status", f"Unsupported reading status: {value}") from exc


async def _get_or_create_author(session: AsyncSession, data: PaperAuthorInput) -> Author:
    if data.author_id is not None:
        author = await session.get(Author, data.author_id)
        if author is None or author.merged_into_id is not None:
            raise ApiError(404, "author_not_found", f"Author not found: {data.author_id}")
        return author
    if not data.display_name:
        raise ApiError(422, "author_required", "Each author needs authorId or displayName")
    display_name = data.display_name.strip()
    existing = await session.scalar(
        select(Author)
        .where(
            func.lower(Author.display_name) == display_name.lower(),
            Author.merged_into_id.is_(None),
        )
        .limit(1)
    )
    if existing is not None:
        return existing
    author = Author(
        id=uuid.uuid4(),
        display_name=display_name,
        family_name=data.family_name,
        given_name=data.given_name,
        status=AuthorStatus.provisional,
    )
    session.add(author)
    return author


async def _unique_citekey(
    session: AsyncSession,
    *,
    provided: str | None,
    first_author: str,
    year: int,
    title: str,
) -> str:
    if provided:
        candidate = provided.strip().lower()
        if not _CITEKEY_RE.match(candidate):
            raise ApiError(422, "invalid_citekey", "Citekey must be lowercase alphanumeric")
        exists = await session.scalar(
            select(Paper.id).where(Paper.citekey == candidate, Paper.deleted_at.is_(None))
        )
        if exists:
            raise ApiError(409, "citekey_conflict", f"Citekey already used: {candidate}")
        return candidate

    base = f"{first_author}{year}{title_word(title)}".lower()
    base = re.sub(r"[^a-z0-9]", "", base)
    if not base:
        base = f"paper{year}"
    if not base[0].isalpha():
        base = f"paper{base}"
    existing = set(
        (
            await session.scalars(
                select(Paper.citekey).where(
                    Paper.citekey.like(f"{base}%"), Paper.deleted_at.is_(None)
                )
            )
        ).all()
    )
    if base not in existing:
        return base
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        candidate = f"{base}{suffix}"
        if candidate not in existing:
            return candidate
    for index in range(2, 1000):
        candidate = f"{base}{index}"
        if candidate not in existing:
            return candidate
    raise ApiError(409, "citekey_conflict", "Could not allocate a unique citekey")


async def _validate_venue(
    session: AsyncSession,
    kind: PaperKind,
    venue_id: uuid.UUID | None,
    venue_edition_id: uuid.UUID | None,
) -> tuple[Venue | None, VenueEdition | None]:
    venue = None
    edition = None
    if venue_id is not None:
        venue = await session.get(Venue, venue_id)
        if venue is None or venue.merged_into_id is not None:
            raise ApiError(422, "venue_not_found", f"Venue not found: {venue_id}")
    if venue_edition_id is not None:
        edition = await session.get(VenueEdition, venue_edition_id)
        if edition is None:
            raise ApiError(422, "edition_not_found", f"Edition not found: {venue_edition_id}")
        if venue is None:
            venue = await session.get(Venue, edition.venue_id)
        elif edition.venue_id != venue.id:
            raise ApiError(422, "edition_venue_mismatch", "Edition does not belong to venue")
    if kind in (PaperKind.journal, PaperKind.conference) and venue is None:
        raise ApiError(422, "venue_required", "venueId is required for journal/conference papers")
    if kind == PaperKind.journal and edition is not None:
        raise ApiError(422, "edition_not_allowed", "Journal papers cannot have a venue edition")
    return venue, edition


async def create_paper(session: AsyncSession, data: PaperCreate) -> Paper:
    kind = _parse_kind(data.kind)
    venue, edition = await _validate_venue(session, kind, data.venue_id, data.venue_edition_id)

    paper_id = uuid.uuid4()
    author_inputs = data.authors
    created_authors: list[tuple[PaperAuthorInput, Author]] = []
    seen_author_ids: set[uuid.UUID] = set()
    for item in author_inputs:
        author = await _get_or_create_author(session, item)
        if author.id in seen_author_ids:
            raise ApiError(422, "duplicate_author", f"Duplicate author: {author.display_name}")
        seen_author_ids.add(author.id)
        created_authors.append((item, author))

    first_author = (
        _author_cite_part(created_authors[0][0], created_authors[0][1]) if created_authors else ""
    )
    citekey = await _unique_citekey(
        session,
        provided=data.citekey,
        first_author=first_author,
        year=data.year,
        title=data.title,
    )

    paper = Paper(
        id=paper_id,
        kind=kind,
        title=data.title,
        title_normalized=normalize_title(data.title),
        abstract=data.abstract,
        year=data.year,
        publication_date=data.publication_date,
        venue_id=venue.id if venue else None,
        venue_edition_id=edition.id if edition else None,
        volume=data.volume,
        issue=data.issue,
        pages=data.pages,
        institution=data.institution,
        citekey=citekey,
        language=data.language,
        author_keywords=data.author_keywords,
        urls=data.urls,
        added_via=data.added_via,
        user_locked_fields=[],
        paper_author_names=", ".join(author.display_name for _, author in created_authors),
    )
    session.add(paper)

    for position, (item, author) in enumerate(created_authors, start=1):
        session.add(
            PaperAuthor(
                paper=paper,
                author=author,
                position=item.position or position,
                is_corresponding=item.is_corresponding,
                equal_contribution=item.equal_contribution,
                affiliation_text=item.affiliation,
                raw_name=item.display_name or author.display_name,
            )
        )

    for identifier in data.identifiers:
        try:
            scheme = IdentifierScheme(identifier.scheme)
        except ValueError as exc:
            raise ApiError(422, "invalid_identifier_scheme", identifier.scheme) from exc
        normalized = normalize_identifier(scheme, identifier.value)
        existing = await session.scalar(
            select(PaperIdentifier).where(
                PaperIdentifier.scheme == scheme,
                PaperIdentifier.normalized == normalized,
            )
        )
        if existing is not None:
            raise ApiError(
                409,
                "duplicate",
                f"Identifier already attached to paper {existing.paper_id}",
            )
        session.add(
            PaperIdentifier(
                paper=paper,
                scheme=scheme,
                raw_value=identifier.value,
                normalized=normalized,
                is_primary=identifier.is_primary,
                source=data.added_via,
            )
        )

    state_data = data.state or StateInput()
    session.add(_new_state(paper, state_data))
    await session.flush()
    return paper


def _new_state(paper: Paper, data: StateInput) -> PaperState:
    now = datetime.now(UTC)
    status = _parse_reading_status(data.status) if data.status else ReadingStatus.unread
    return PaperState(
        paper=paper,
        status=status,
        priority=data.priority or 0,
        rating=data.rating,
        progress_page=data.progress_page,
        starred_at=now if data.starred else None,
        archived_at=now if data.archived else None,
        last_opened_at=None,
    )


async def get_paper(session: AsyncSession, ref: str, *, include_deleted: bool = False) -> Paper:
    condition = None
    try:
        condition = Paper.id == uuid.UUID(ref)
    except ValueError:
        condition = Paper.citekey == ref
    stmt = select(Paper).options(*PAPER_OPTIONS).where(condition)
    if not include_deleted:
        stmt = stmt.where(Paper.deleted_at.is_(None))
    paper = await session.scalar(stmt)
    if paper is None:
        raise ApiError(404, "not_found", "Paper not found")
    return paper


async def update_paper(session: AsyncSession, paper: Paper, data: PaperUpdate) -> Paper:
    fields = data.model_fields_set
    if not fields:
        return paper

    if "kind" in fields and data.kind is not None:
        paper.kind = _parse_kind(data.kind)
    if "title" in fields and data.title is not None:
        paper.title = data.title.strip()
        paper.title_normalized = normalize_title(paper.title)
    if "abstract" in fields:
        paper.abstract = data.abstract
    if "year" in fields and data.year is not None:
        paper.year = data.year
    if "publication_date" in fields:
        paper.publication_date = data.publication_date
    if "volume" in fields:
        paper.volume = data.volume
    if "issue" in fields:
        paper.issue = data.issue
    if "pages" in fields:
        paper.pages = data.pages
    if "institution" in fields:
        paper.institution = data.institution
    if "language" in fields and data.language is not None:
        paper.language = data.language
    if "author_keywords" in fields and data.author_keywords is not None:
        paper.author_keywords = data.author_keywords
    if "urls" in fields and data.urls is not None:
        paper.urls = data.urls
    if "citekey" in fields and data.citekey is not None:
        candidate = data.citekey.strip().lower()
        if not _CITEKEY_RE.match(candidate):
            raise ApiError(422, "invalid_citekey", "Citekey must be lowercase alphanumeric")
        conflict = await session.scalar(
            select(Paper.id).where(
                Paper.citekey == candidate, Paper.id != paper.id, Paper.deleted_at.is_(None)
            )
        )
        if conflict:
            raise ApiError(409, "citekey_conflict", f"Citekey already used: {candidate}")
        paper.citekey = candidate

    if "venue_id" in fields or "venue_edition_id" in fields:
        venue_id = data.venue_id if "venue_id" in fields else paper.venue_id
        edition_id = (
            data.venue_edition_id if "venue_edition_id" in fields else paper.venue_edition_id
        )
        venue, edition = await _validate_venue(session, paper.kind, venue_id, edition_id)
        paper.venue_id = venue.id if venue else None
        paper.venue_edition_id = edition.id if edition else None

    paper.user_locked_fields = list(dict.fromkeys([*paper.user_locked_fields, *sorted(fields)]))
    paper.version += 1
    await session.flush()
    return paper


async def delete_paper(session: AsyncSession, paper: Paper) -> None:
    paper.deleted_at = datetime.now(UTC)
    paper.version += 1


async def restore_paper(session: AsyncSession, paper: Paper) -> None:
    paper.deleted_at = None
    paper.version += 1


async def ranks_for_venues(
    session: AsyncSession, venue_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[VenueRank]]:
    if not venue_ids:
        return {}
    rows = (await session.scalars(select(VenueRank).where(VenueRank.venue_id.in_(venue_ids)))).all()
    result: dict[uuid.UUID, list[VenueRank]] = {}
    for row in rows:
        result.setdefault(row.venue_id, []).append(row)
    return result


def _venue_ref(paper: Paper) -> dict[str, Any] | None:
    venue = paper.venue
    if venue is None:
        return None
    edition = paper.venue_edition
    return {
        "id": venue.id,
        "kind": venue.kind.value,
        "abbrev": venue.abbrev,
        "full_name": venue.full_name,
        "ccf_field": venue.ccf_field,
        "ccf_field_key": venue.ccf_field_key,
        "edition": (
            {
                "id": edition.id,
                "year": edition.year,
                "track": edition.track.value,
                "ordinal": edition.ordinal,
            }
            if edition
            else None
        ),
    }


def _state_out(paper: Paper) -> dict[str, Any]:
    state = paper.state
    if state is None:
        return {
            "status": "unread",
            "priority": 0,
            "rating": None,
            "progress_page": None,
            "starred": False,
            "archived": False,
        }
    return {
        "status": state.status.value,
        "priority": state.priority,
        "rating": state.rating,
        "progress_page": state.progress_page,
        "starred": state.starred_at is not None,
        "archived": state.archived_at is not None,
    }


def paper_to_summary(paper: Paper, ranks: dict[uuid.UUID, list[VenueRank]]) -> dict[str, Any]:
    venue_ranks = ranks.get(paper.venue_id, []) if paper.venue_id else []
    authors = [
        {
            "id": link.author.id,
            "display_name": link.author.display_name,
            "family_name": link.author.family_name,
            "given_name": link.author.given_name,
            "position": link.position,
            "is_corresponding": link.is_corresponding,
            "equal_contribution": link.equal_contribution,
            "affiliation": link.affiliation_text,
            "orcid": link.author.orcid,
            "is_me": link.author.is_me,
        }
        for link in sorted(paper.authors, key=lambda item: item.position)
    ]
    return {
        "id": paper.id,
        "kind": paper.kind.value,
        "title": paper.title,
        "citekey": paper.citekey,
        "year": paper.year,
        "venue": _venue_ref(paper),
        "evaluation": resolve_evaluation(paper.venue, venue_ranks, paper.year),
        "authors": authors,
        "author_count": len(authors),
        "state": _state_out(paper),
        "counts": {"pdf_files": 0, "annotations": 0, "cited_by": 0},
        "created_at": paper.created_at,
        "updated_at": paper.updated_at,
        "version": paper.version,
    }


def paper_to_detail(paper: Paper, ranks: dict[uuid.UUID, list[VenueRank]]) -> dict[str, Any]:
    data = paper_to_summary(paper, ranks)
    data.update(
        {
            "abstract": paper.abstract,
            "publication_date": paper.publication_date,
            "volume": paper.volume,
            "issue": paper.issue,
            "pages": paper.pages,
            "institution": paper.institution,
            "language": paper.language,
            "author_keywords": paper.author_keywords,
            "user_locked_fields": paper.user_locked_fields,
            "urls": paper.urls,
            "identifiers": [
                {
                    "id": identifier.id,
                    "scheme": identifier.scheme.value,
                    "value": identifier.normalized,
                    "is_primary": identifier.is_primary,
                }
                for identifier in paper.identifiers
            ],
        }
    )
    return data


def encode_cursor(created_at: datetime, paper_id: uuid.UUID) -> str:
    payload = {"createdAt": created_at.isoformat(), "id": str(paper_id)}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        created_at = datetime.fromisoformat(payload["createdAt"])
        paper_id = uuid.UUID(payload["id"])
    except Exception as exc:
        raise ApiError(400, "invalid_cursor", "Invalid cursor") from exc
    return created_at, paper_id


async def list_papers(
    session: AsyncSession,
    *,
    q: str | None,
    year: int | None,
    kind: str | None,
    venue_id: uuid.UUID | None,
    status: str | None,
    starred: bool | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[Paper], str | None, bool]:
    stmt: Select = select(Paper).options(*PAPER_OPTIONS).where(Paper.deleted_at.is_(None))
    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Paper.title).like(pattern),
                func.lower(Paper.paper_author_names).like(pattern),
                func.lower(func.coalesce(Paper.abstract, "")).like(pattern),
            )
        )
    if year is not None:
        stmt = stmt.where(Paper.year == year)
    if kind is not None:
        stmt = stmt.where(Paper.kind == _parse_kind(kind))
    if venue_id is not None:
        stmt = stmt.where(Paper.venue_id == venue_id)
    if status is not None:
        stmt = stmt.where(Paper.state.has(PaperState.status == _parse_reading_status(status)))
    if starred is True:
        stmt = stmt.where(Paper.state.has(PaperState.starred_at.is_not(None)))
    elif starred is False:
        stmt = stmt.where(Paper.state.has(PaperState.starred_at.is_(None)))

    if cursor:
        created_at, paper_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Paper.created_at < created_at,
                and_(Paper.created_at == created_at, Paper.id < paper_id),
            )
        )
    stmt = stmt.order_by(Paper.created_at.desc(), Paper.id.desc()).limit(limit + 1)
    rows = list((await session.scalars(stmt)).all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
    return items, next_cursor, has_more


def today(value: date | None) -> date | None:
    return value
