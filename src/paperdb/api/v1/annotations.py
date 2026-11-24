from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import scoped
from paperdb.api.schemas import AnnotationCreate, AnnotationOut, AnnotationUpdate
from paperdb.auth.tokens import TokenPrincipal
from paperdb.db.session import get_session
from paperdb.errors import ApiError
from paperdb.papers.service import get_paper
from paperdb.pdf.service import (
    create_annotation,
    get_annotation,
    list_annotations,
)

router = APIRouter(tags=["annotations"])


def _out(annotation) -> AnnotationOut:
    return AnnotationOut(
        id=annotation.id,
        paper_id=annotation.paper_id,
        pdf_file_id=annotation.pdf_file_id,
        kind=annotation.kind.value,
        page=annotation.page,
        rects=annotation.rects,
        quote=annotation.quote,
        prefix=annotation.prefix,
        suffix=annotation.suffix,
        comment=annotation.comment,
        color=annotation.color,
        anchor_status=annotation.anchor_status,
        created_at=annotation.created_at,
        updated_at=annotation.updated_at,
    )


@router.get("/papers/{ref}/annotations", response_model=list[AnnotationOut])
async def list_endpoint(
    ref: str,
    page: int | None = Query(default=None, ge=1),
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> list[AnnotationOut]:
    paper = await get_paper(session, ref)
    return [_out(item) for item in await list_annotations(session, paper.id, page=page)]


@router.post(
    "/papers/{ref}/annotations",
    response_model=AnnotationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_endpoint(
    ref: str,
    payload: AnnotationCreate,
    principal: TokenPrincipal = Depends(scoped("annotations:write")),
    session: AsyncSession = Depends(get_session),
) -> AnnotationOut:
    paper = await get_paper(session, ref)
    annotation = await create_annotation(
        session,
        paper=paper,
        file_id=payload.pdf_file_id,
        kind=payload.kind,
        page=payload.page,
        rects=payload.rects,
        quote=payload.quote,
        prefix=payload.prefix,
        suffix=payload.suffix,
        comment=payload.comment,
        color=payload.color,
    )
    await session.commit()
    return _out(annotation)


@router.patch("/annotations/{annotation_id}", response_model=AnnotationOut)
async def update_endpoint(
    annotation_id: uuid.UUID,
    payload: AnnotationUpdate,
    principal: TokenPrincipal = Depends(scoped("annotations:write")),
    session: AsyncSession = Depends(get_session),
) -> AnnotationOut:
    annotation = await get_annotation(session, annotation_id)
    if payload.comment is not None:
        annotation.comment = payload.comment
    if payload.color is not None:
        if payload.color not in {"yellow", "green", "blue", "pink", "purple"}:
            raise ApiError(422, "invalid_color", "Unsupported annotation color")
        annotation.color = payload.color
    if payload.rects is not None:
        for rect in payload.rects:
            if not all(0 <= float(rect.get(key, -1)) <= 1 for key in ("x", "y", "w", "h")):
                raise ApiError(422, "invalid_rect", "Annotation rects must be 0..1")
        annotation.rects = payload.rects
    annotation.updated_at = datetime.now(UTC)
    await session.commit()
    return _out(annotation)


@router.delete("/annotations/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    annotation_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("annotations:write")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    annotation = await get_annotation(session, annotation_id)
    annotation.deleted_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
