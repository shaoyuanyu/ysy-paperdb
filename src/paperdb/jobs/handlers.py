from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.citations.service import sync_papers
from paperdb.config import Settings
from paperdb.db.models import IdentifierScheme, Job, PaperIdentifier, PdfFile
from paperdb.imports.service import process_bibtex, process_import
from paperdb.ingest.resolve import resolve_input
from paperdb.ingest.venue_resolve import ensure_venue
from paperdb.papers.service import get_paper
from paperdb.pdf.service import extract_file
from paperdb.storage import get_storage


async def handle_import(session: AsyncSession, job: Job, settings: Settings) -> None:
    import_id = job.payload.get("import_id")
    if not import_id:
        raise ValueError("import job missing import_id")
    await process_import(session, uuid.UUID(str(import_id)), settings)


async def handle_bibtex(session: AsyncSession, job: Job, settings: Settings) -> None:
    import_id = job.payload.get("import_id")
    if not import_id:
        raise ValueError("bibtex job missing import_id")
    await process_bibtex(session, uuid.UUID(str(import_id)), settings)


async def handle_enrich(session: AsyncSession, job: Job, settings: Settings) -> None:
    paper_id = job.payload.get("paper_id")
    if not paper_id:
        raise ValueError("enrich job missing paper_id")
    paper = await get_paper(session, str(paper_id))
    identifier = next(
        (
            item
            for item in paper.identifiers
            if item.scheme in {IdentifierScheme.doi, IdentifierScheme.arxiv}
        ),
        None,
    )
    raw = (
        f"{identifier.scheme.value}:{identifier.normalized}"
        if identifier is not None
        else paper.title
    )
    candidate, warnings = await resolve_input(session, raw, settings)
    if candidate is None:
        raise ValueError("; ".join(warnings) or "no metadata found")
    locked = set(paper.user_locked_fields)
    venue, edition = (None, None)
    if candidate.venue is not None:
        venue, edition = await ensure_venue(
            session,
            candidate.venue,
            raw_name=candidate._venue_name,
            kind=candidate.kind,
            year=candidate.year,
            dblp_venue_key=candidate._dblp_venue_key,
            edition_dblp_key=candidate._edition_key,
        )
    if "title" not in locked and candidate.title:
        from paperdb.papers.service import normalize_title

        paper.title = candidate.title
        paper.title_normalized = normalize_title(candidate.title)
    if "abstract" not in locked and candidate.abstract:
        paper.abstract = candidate.abstract
    if "year" not in locked:
        paper.year = candidate.year
    if "publication_date" not in locked and candidate.publication_date:
        paper.publication_date = candidate.publication_date
    if "volume" not in locked and candidate.volume:
        paper.volume = candidate.volume
    if "issue" not in locked and candidate.issue:
        paper.issue = candidate.issue
    if "pages" not in locked and candidate.pages:
        paper.pages = candidate.pages
    if "venue_id" not in locked and venue is not None:
        paper.venue_id = venue.id
        paper.venue_edition_id = edition.id if edition else None
    existing = {(item.scheme, item.normalized) for item in paper.identifiers}
    for item in candidate.identifiers:
        scheme_value = str(item["scheme"])
        value = str(item["value"])
        try:
            scheme = IdentifierScheme(scheme_value)
        except ValueError:
            continue
        if (scheme, value) in existing:
            continue
        conflict = await session.scalar(
            select(PaperIdentifier).where(
                PaperIdentifier.scheme == scheme,
                PaperIdentifier.normalized == value,
            )
        )
        if conflict is not None:
            continue
        session.add(
            PaperIdentifier(
                paper_id=paper.id,
                scheme=scheme,
                raw_value=value,
                normalized=value,
                is_primary=bool(item.get("isPrimary")),
                source="enrich",
            )
        )
        existing.add((scheme, value))
    paper.version += 1
    await session.flush()


async def handle_extract_pdf(session: AsyncSession, job: Job, settings: Settings) -> None:
    file_id = job.payload.get("file_id")
    if not file_id:
        raise ValueError("extract job missing file_id")
    await extract_file(session, uuid.UUID(str(file_id)))
    await session.flush()


async def handle_gc_blob(session: AsyncSession, job: Job, settings: Settings) -> None:
    backend = str(job.payload.get("backend") or "")
    key = str(job.payload.get("key") or "")
    if not key:
        raise ValueError("gc job missing key")
    if backend != get_storage().name:
        return
    count = int(
        await session.scalar(
            select(func.count())
            .select_from(PdfFile)
            .where(
                PdfFile.storage_backend == backend,
                PdfFile.storage_key == key,
                PdfFile.deleted_at.is_(None),
            )
        )
        or 0
    )
    if count == 0:
        await get_storage().delete(key)


async def handle_sync_citations(session: AsyncSession, job: Job, settings: Settings) -> None:
    raw = job.payload.get("paper_ids") or []
    paper_ids = [uuid.UUID(str(item)) for item in raw]
    if not paper_ids:
        return
    await sync_papers(session, paper_ids, settings)
    await session.flush()
