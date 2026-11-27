from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import ExportTokenCreate, ExportTokenCreated, ExportTokenOut
from paperdb.auth.tokens import TokenPrincipal, create_export_token
from paperdb.config import Settings, get_settings
from paperdb.db.models import Collection, ExportToken, Paper
from paperdb.db.session import get_session
from paperdb.errors import ApiError
from paperdb.exports import authenticate_export_token
from paperdb.organize.collections import descendant_ids
from paperdb.organize.projects import get_project, project_paper_ids
from paperdb.papers.bibtex import render_bibtex
from paperdb.papers.service import PAPER_OPTIONS

router = APIRouter(tags=["exports"])


@router.post(
    "/export-tokens",
    response_model=ExportTokenCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_export_link(
    payload: ExportTokenCreate,
    principal: TokenPrincipal = Depends(scoped("exports:read")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ExportTokenCreated:
    if payload.scope_type not in {"library", "paper", "collection", "project"}:
        raise ApiError(
            422,
            "unsupported_scope",
            "scopeType must be library/paper/collection/project",
        )
    if payload.scope_type != "library" and payload.scope_id is None:
        raise ApiError(422, "scope_id_required", "scopeId is required for this scope")
    if payload.scope_type == "paper":
        paper = await session.get(Paper, payload.scope_id)
        if paper is None or paper.deleted_at is not None:
            raise ApiError(404, "not_found", "Paper not found")
    elif payload.scope_type == "collection":
        collection = await session.get(Collection, payload.scope_id)
        if collection is None or collection.deleted_at is not None:
            raise ApiError(404, "not_found", "Collection not found")
    elif payload.scope_type == "project":
        assert payload.scope_id is not None
        await get_project(session, payload.scope_id)
    plaintext, prefix, token_hash = create_export_token(payload.name, settings.token_pepper)
    record = ExportToken(
        name=payload.name,
        prefix=prefix,
        token_hash=token_hash,
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
        citekey_template=payload.citekey_template,
        include_abstract=payload.include_abstract,
    )
    session.add(record)
    await session.commit()
    return ExportTokenCreated(
        id=record.id,
        name=record.name,
        prefix=record.prefix,
        scope_type=record.scope_type,
        scope_id=record.scope_id,
        url=f"{settings.public_url.rstrip('/')}/v1/exports/bib/{plaintext}.bib",
        created_at=record.created_at,
    )


@router.get("/export-tokens", response_model=list[ExportTokenOut])
async def list_export_links(
    principal: TokenPrincipal = Depends(scoped("exports:read")),
    session: AsyncSession = Depends(get_session),
) -> list[ExportTokenOut]:
    rows = (
        await session.scalars(select(ExportToken).order_by(ExportToken.created_at.desc()))
    ).all()
    return [
        ExportTokenOut(
            id=row.id,
            name=row.name,
            prefix=row.prefix,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            include_abstract=row.include_abstract,
            created_at=row.created_at,
            last_accessed_at=row.last_accessed_at,
            access_count=row.access_count,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]


@router.delete("/export-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_export_link(
    token_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("exports:read")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    record = await session.get(ExportToken, token_id)
    if record is None:
        raise ApiError(404, "not_found", "Export link not found")
    if record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/exports/bib/{token}")
async def export_bib(
    token: str,
    request: Request,
    if_none_match: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    raw = token[:-4] if token.endswith(".bib") else token
    record = await authenticate_export_token(session, raw, settings)

    stmt = select(Paper).options(*PAPER_OPTIONS).where(Paper.deleted_at.is_(None))
    if record.scope_type == "paper":
        stmt = stmt.where(Paper.id == record.scope_id)
    elif record.scope_type == "collection":
        collection = await session.get(Collection, record.scope_id)
        if collection is None or collection.deleted_at is not None:
            raise ApiError(404, "not_found", "Export link not found")
        ids = [collection.id, *await descendant_ids(session, collection.id)]
        stmt = stmt.where(Paper.collections.any(Collection.id.in_(ids)), Paper.staged.is_(False))
    elif record.scope_type == "project":
        assert record.scope_id is not None
        try:
            project = await get_project(session, record.scope_id)
        except ApiError:
            raise ApiError(404, "not_found", "Export link not found") from None
        ids = await project_paper_ids(session, project)
        stmt = stmt.where(Paper.id.in_(ids), Paper.staged.is_(False))
    elif record.scope_type != "library":
        raise ApiError(404, "not_found", "Export link not found")
    else:
        # Staging-area papers are not part of the exported library.
        stmt = stmt.where(Paper.staged.is_(False))
    papers = list((await session.scalars(stmt)).all())
    body = render_bibtex(papers, include_abstract=record.include_abstract)
    etag = hashlib.sha256(body.encode()).hexdigest()
    if if_none_match and if_none_match.strip('"') == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": f'"{etag}"'})
    return Response(
        content=body,
        media_type="application/x-bibtex",
        headers={
            "Content-Type": "application/x-bibtex; charset=utf-8",
            "Cache-Control": "private, no-cache",
            "ETag": f'"{etag}"',
            "Content-Disposition": f'inline; filename="paperdb-{record.scope_type}.bib"',
        },
    )
