from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from paperdb.config import Settings
from paperdb.db.models import (
    CitationEdge,
    CitationStat,
    ExternalCitation,
    ExternalWork,
    IdentifierScheme,
    Paper,
    PaperIdentifier,
)
from paperdb.ingest.http import build_client, get_json

OPENALEX = "https://api.openalex.org/works"
_SELECT = "id,doi,title,publication_year,cited_by_count,referenced_works"


def _strip_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


async def _find_work(
    client: httpx.AsyncClient, paper: Paper, identifiers: dict[str, str], settings: Settings
) -> dict[str, Any] | None:
    doi = identifiers.get("doi")
    openalex_id = identifiers.get("openalex")
    if doi:
        data = await get_json(
            client,
            OPENALEX,
            params={
                "filter": f"doi:{doi}",
                "select": _SELECT,
                "per-page": 1,
                "mailto": settings.openalex_mailto,
            },
            headers={"User-Agent": settings.arxiv_user_agent},
        )
        results = (data or {}).get("results") or []
        if results:
            return results[0]
    if openalex_id:
        data = await get_json(
            client,
            f"{OPENALEX}/{openalex_id}",
            params={"mailto": settings.openalex_mailto},
            headers={"User-Agent": settings.arxiv_user_agent},
        )
        if data:
            return data
    return None


async def _library_map(
    session: AsyncSession, scheme: IdentifierScheme, values: list[str]
) -> dict[str, uuid.UUID]:
    if not values:
        return {}
    rows = await session.execute(
        select(PaperIdentifier.normalized, PaperIdentifier.paper_id)
        .join(Paper, Paper.id == PaperIdentifier.paper_id)
        .where(
            PaperIdentifier.scheme == scheme,
            PaperIdentifier.normalized.in_(values),
            Paper.deleted_at.is_(None),
        )
    )
    return {value: paper_id for value, paper_id in rows.all()}


async def _upsert_external(
    session: AsyncSession,
    *,
    openalex_id: str | None,
    doi: str | None,
    title: str,
    year: int | None,
    cited_by_count: int | None,
    resolved_paper_id: uuid.UUID | None = None,
) -> ExternalWork:
    work = None
    if openalex_id:
        work = await session.scalar(
            select(ExternalWork).where(ExternalWork.openalex_id == openalex_id)
        )
    if work is None and doi:
        work = await session.scalar(select(ExternalWork).where(ExternalWork.doi == doi))
    if work is None:
        work = ExternalWork(
            id=uuid.uuid4(),
            title=title or "Untitled external work",
            year=year,
            doi=doi,
            openalex_id=openalex_id,
            cited_by_count=cited_by_count,
            resolved_paper_id=resolved_paper_id,
        )
        session.add(work)
        await session.flush()
        return work
    work.title = title or work.title
    work.year = year or work.year
    work.doi = doi or work.doi
    work.openalex_id = openalex_id or work.openalex_id
    work.cited_by_count = cited_by_count if cited_by_count is not None else work.cited_by_count
    work.resolved_paper_id = resolved_paper_id or work.resolved_paper_id
    await session.flush()
    return work


async def _edge(session: AsyncSession, citing: uuid.UUID, cited: uuid.UUID, source: str) -> None:
    if citing == cited:
        return
    edge = await session.scalar(
        select(CitationEdge).where(
            CitationEdge.citing_paper_id == citing,
            CitationEdge.cited_paper_id == cited,
        )
    )
    if edge is None:
        session.add(
            CitationEdge(
                id=uuid.uuid4(),
                citing_paper_id=citing,
                cited_paper_id=cited,
                source=source,
            )
        )
    else:
        edge.last_seen_at = datetime.now(UTC)
        edge.source = source


async def _external_citation(
    session: AsyncSession,
    paper_id: uuid.UUID,
    external_id: uuid.UUID,
    direction: str,
    source: str,
) -> None:
    row = await session.get(ExternalCitation, (paper_id, external_id, direction))
    if row is None:
        session.add(
            ExternalCitation(
                paper_id=paper_id,
                external_work_id=external_id,
                direction=direction,
                source=source,
            )
        )
    else:
        row.last_seen_at = datetime.now(UTC)


async def sync_paper(
    session: AsyncSession,
    paper: Paper,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    identifiers = {
        item.scheme.value: item.normalized
        for item in paper.identifiers
        if item.scheme in {IdentifierScheme.doi, IdentifierScheme.openalex}
    }
    owns_client = client is None
    client = client or build_client(settings)
    try:
        work = await _find_work(client, paper, identifiers, settings)
        if work is None:
            return False
        work_id = _strip_id(work.get("id"))
        if work_id:
            identifiers["openalex"] = work_id
        refs = [_strip_id(value) for value in work.get("referenced_works") or []]
        refs = [value for value in refs if value]
        library = await _library_map(session, IdentifierScheme.openalex, refs)
        for ref in refs:
            target = library.get(ref)
            if target is not None:
                await _edge(session, paper.id, target, "openalex")
            else:
                external = await _upsert_external(
                    session,
                    openalex_id=ref,
                    doi=None,
                    title=f"OpenAlex {ref}",
                    year=None,
                    cited_by_count=None,
                )
                await _external_citation(session, paper.id, external.id, "cites", "openalex")

        cited_count = work.get("cited_by_count")
        work_id = _strip_id(work.get("id"))
        if work_id:
            data = await get_json(
                client,
                OPENALEX,
                params={
                    "filter": f"cites:{work_id}",
                    "select": "id,doi,title,publication_year,cited_by_count",
                    "per-page": 200,
                    "mailto": settings.openalex_mailto,
                },
                headers={"User-Agent": settings.arxiv_user_agent},
            )
            for citing_work in (data or {}).get("results") or []:
                external_id = _strip_id(citing_work.get("id"))
                doi = citing_work.get("doi")
                if isinstance(doi, str):
                    doi = doi.replace("https://doi.org/", "").lower()
                title = str(citing_work.get("title") or "Untitled external work")
                year = citing_work.get("publication_year")
                by_openalex = await _library_map(
                    session, IdentifierScheme.openalex, [external_id] if external_id else []
                )
                by_doi = await _library_map(session, IdentifierScheme.doi, [doi] if doi else [])
                target = by_openalex.get(external_id or "") or by_doi.get(doi or "")
                if target is not None:
                    await _edge(session, target, paper.id, "openalex")
                    continue
                external = await _upsert_external(
                    session,
                    openalex_id=external_id,
                    doi=doi,
                    title=title,
                    year=year,
                    cited_by_count=citing_work.get("cited_by_count"),
                )
                await _external_citation(session, paper.id, external.id, "cited_by", "openalex")

        today = date.today()
        stat = await session.get(CitationStat, (paper.id, "openalex", today))
        if stat is None:
            session.add(
                CitationStat(
                    paper_id=paper.id,
                    source="openalex",
                    as_of=today,
                    cited_by_count=int(cited_count or 0),
                )
            )
        else:
            stat.cited_by_count = int(cited_count or 0)
        paper.citations_synced_at = datetime.now(UTC)
        await session.flush()
        return True
    finally:
        if owns_client:
            await client.aclose()


async def sync_papers(session: AsyncSession, paper_ids: list[uuid.UUID], settings: Settings) -> int:
    papers = list(
        (
            await session.scalars(
                select(Paper)
                .options(selectinload(Paper.identifiers))
                .where(Paper.id.in_(paper_ids), Paper.deleted_at.is_(None))
            )
        ).all()
    )
    if not papers:
        return 0
    client = build_client(settings)
    synced = 0
    try:
        for paper in papers:
            if await sync_paper(session, paper, settings, client=client):
                synced += 1
    finally:
        await client.aclose()
    return synced
