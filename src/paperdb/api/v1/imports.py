from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from paperdb.api.deps import scoped
from paperdb.api.schemas import (
    CandidateAuthorOut,
    Evaluation,
    IdentifierInput,
    ImportCreated,
    ImportIdentifiersRequest,
    ImportItemOut,
    ImportJobOut,
    ImportPage,
    JobOut,
    PdfImportCreated,
    ResolveCandidateOut,
    ResolveRequest,
    ResolveResponse,
    VenueSuggestionOut,
)
from paperdb.auth.tokens import TokenPrincipal
from paperdb.config import Settings, get_settings
from paperdb.db.models import ImportItem, ImportJob, Job
from paperdb.db.session import get_session
from paperdb.errors import ApiError
from paperdb.imports.pdf_import import import_pdf
from paperdb.imports.service import (
    cancel_import,
    create_bibtex_import_job,
    create_import_job,
    import_to_out,
    item_to_out,
    retry_item,
)
from paperdb.ingest.http import build_client
from paperdb.ingest.resolve import identify_input, resolve_input

router = APIRouter(tags=["ingest"])


def _candidate_out(candidate) -> ResolveCandidateOut:
    return ResolveCandidateOut(
        confidence=candidate.confidence,
        sources=candidate.sources,
        kind=candidate.kind,
        title=candidate.title,
        abstract=candidate.abstract,
        year=candidate.year,
        publication_date=candidate.publication_date,
        authors=[
            CandidateAuthorOut(
                display_name=author.name,
                family_name=author.family,
                given_name=author.given,
                orcid=author.orcid,
                dblp_pid=author.dblp_pid,
                s2_author_id=author.s2_author_id,
                openalex_id=author.openalex_id,
                affiliation=author.affiliation,
            )
            for author in candidate.authors
        ],
        identifiers=[
            IdentifierInput(
                scheme=str(identifier["scheme"]),
                value=str(identifier["value"]),
                is_primary=bool(identifier.get("isPrimary")),
            )
            for identifier in candidate.identifiers
        ],
        venue=(
            VenueSuggestionOut(
                venue_id=candidate.venue.venue_id,
                full_name=candidate.venue.full_name,
                abbrev=candidate.venue.abbrev,
                kind=candidate.venue.kind,
                year=candidate.venue.year,
                track=candidate.venue.track,
                confidence=candidate.venue.confidence,
                evidence=candidate.venue.evidence,
            )
            if candidate.venue
            else None
        ),
        evaluation=Evaluation.model_validate(candidate.evaluation),
        duplicate_of=candidate.duplicate_of,
        warnings=candidate.warnings,
    )


@router.post("/resolve", response_model=ResolveResponse)
async def resolve_endpoint(
    payload: ResolveRequest,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ResolveResponse:
    ref = identify_input(payload.input)
    async with build_client(settings) as client:
        candidate, warnings = await resolve_input(session, payload.input, settings, client=client)
    return ResolveResponse(
        input={"raw": payload.input, "kind": ref.kind, "normalized": ref.value},
        candidates=[_candidate_out(candidate)] if candidate else [],
        warnings=warnings,
    )


@router.post(
    "/imports/identifiers",
    response_model=ImportCreated,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_import_endpoint(
    payload: ImportIdentifiersRequest,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> ImportCreated:
    import_job, job_id = await create_import_job(session, payload, token_id=principal.token_id)
    await session.commit()
    return ImportCreated(
        import_id=import_job.id,
        job_id=job_id,
        status=import_job.status.value,
        items=len(import_job.items),
    )


@router.post(
    "/imports/pdf",
    response_model=PdfImportCreated,
    status_code=status.HTTP_201_CREATED,
)
async def import_pdf_endpoint(
    file: UploadFile = File(...),
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PdfImportCreated:
    data = await file.read()
    result = await import_pdf(
        session,
        data=data,
        filename=file.filename,
        settings=settings,
    )
    await session.commit()
    return PdfImportCreated(
        paper_id=result.paper.id,
        file_id=result.file_id,
        job_id=result.job_id,
        created=result.created,
        title=result.title,
        warnings=result.warnings,
    )


@router.post(
    "/imports/bibtex",
    response_model=ImportCreated,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_bibtex_import_endpoint(
    file: UploadFile = File(...),
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> ImportCreated:
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise ApiError(413, "payload_too_large", "BibTeX file is too large")
    text = raw.decode("utf-8", errors="replace")
    import_job, job_id = await create_bibtex_import_job(session, text, token_id=principal.token_id)
    await session.commit()
    return ImportCreated(
        import_id=import_job.id,
        job_id=job_id,
        status=import_job.status.value,
        items=len(import_job.items),
    )


@router.get("/imports", response_model=ImportPage)
async def list_imports_endpoint(
    limit: int = Query(default=20, ge=1, le=100),
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> ImportPage:
    rows = list(
        (
            await session.scalars(
                select(ImportJob).order_by(ImportJob.created_at.desc()).limit(limit)
            )
        ).all()
    )
    return ImportPage(items=[import_to_out(row) for row in rows], has_more=False)


@router.get("/imports/{import_id}", response_model=ImportJobOut)
async def get_import_endpoint(
    import_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> ImportJobOut:
    row = await session.get(ImportJob, import_id)
    if row is None:
        raise ApiError(404, "not_found", "Import job not found")
    return import_to_out(row)


@router.get("/imports/{import_id}/items", response_model=list[ImportItemOut])
async def list_items_endpoint(
    import_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> list[ImportItemOut]:
    if await session.get(ImportJob, import_id) is None:
        raise ApiError(404, "not_found", "Import job not found")
    rows = (
        await session.scalars(
            select(ImportItem).where(ImportItem.job_id == import_id).order_by(ImportItem.ordinal)
        )
    ).all()
    return [item_to_out(row) for row in rows]


@router.post("/imports/{import_id}/items/{item_id}/retry", response_model=ImportCreated)
async def retry_item_endpoint(
    import_id: uuid.UUID,
    item_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> ImportCreated:
    row = await session.scalar(
        select(ImportJob).options(selectinload(ImportJob.items)).where(ImportJob.id == import_id)
    )
    if row is None:
        raise ApiError(404, "not_found", "Import job not found")
    item = await session.get(ImportItem, item_id)
    if item is None or item.job_id != import_id:
        raise ApiError(404, "not_found", "Import item not found")
    job_id = await retry_item(session, row, item)
    await session.commit()
    return ImportCreated(
        import_id=row.id,
        job_id=job_id,
        status=row.status.value,
        items=len(row.items),
    )


@router.post("/imports/{import_id}/cancel", response_model=ImportJobOut)
async def cancel_import_endpoint(
    import_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:write")),
    session: AsyncSession = Depends(get_session),
) -> ImportJobOut:
    row = await session.scalar(
        select(ImportJob).options(selectinload(ImportJob.items)).where(ImportJob.id == import_id)
    )
    if row is None:
        raise ApiError(404, "not_found", "Import job not found")
    await cancel_import(session, row)
    await session.commit()
    return import_to_out(row)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job_endpoint(
    job_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    row = await session.get(Job, job_id)
    if row is None:
        raise ApiError(404, "not_found", "Job not found")
    return JobOut(
        id=row.id,
        kind=row.kind,
        status=row.status.value,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        last_error=row.last_error,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )
