from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.config import Settings
from paperdb.db.models import (
    Annotation,
    AnnotationKind,
    Paper,
    PdfFile,
    PdfPageText,
    PdfTextStatus,
    ResearchWorkspacePaper,
)
from paperdb.errors import ApiError
from paperdb.jobs.queue import enqueue
from paperdb.pdf.extract import extract_pdf
from paperdb.storage import get_storage


async def create_file(
    session: AsyncSession,
    *,
    paper: Paper,
    data: bytes,
    filename: str | None,
    settings: Settings,
    source_url: str | None = None,
) -> tuple[PdfFile, uuid.UUID | None]:
    if not data.startswith(b"%PDF-"):
        raise ApiError(415, "unsupported_media_type", "Only PDF uploads are accepted")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ApiError(413, "payload_too_large", "PDF is larger than the upload limit")
    storage = get_storage()
    stored = await storage.put_bytes(data, extension="pdf")
    existing = await session.scalar(
        select(PdfFile).where(
            PdfFile.paper_id == paper.id,
            PdfFile.storage_key == stored.key,
            PdfFile.deleted_at.is_(None),
        )
    )
    if existing is not None:
        return existing, None
    has_primary = await session.scalar(
        select(PdfFile.id).where(
            PdfFile.paper_id == paper.id,
            PdfFile.is_primary.is_(True),
            PdfFile.deleted_at.is_(None),
        )
    )
    file = PdfFile(
        id=uuid.uuid4(),
        paper_id=paper.id,
        storage_backend=stored.backend,
        storage_key=stored.key,
        sha256=stored.sha256,
        byte_size=stored.byte_size,
        mime="application/pdf",
        original_filename=filename,
        source_url=source_url,
        is_primary=has_primary is None,
        text_status=PdfTextStatus.pending,
    )
    session.add(file)
    await session.flush()
    job = await enqueue(session, "extract_pdf", {"file_id": str(file.id)})
    return file, job.id


async def get_file(session: AsyncSession, file_id: uuid.UUID) -> PdfFile:
    file = await session.get(PdfFile, file_id)
    if file is None or file.deleted_at is not None:
        raise ApiError(404, "not_found", "PDF file not found")
    return file


async def list_files(session: AsyncSession, paper_id: uuid.UUID) -> list[PdfFile]:
    return list(
        (
            await session.scalars(
                select(PdfFile)
                .where(PdfFile.paper_id == paper_id, PdfFile.deleted_at.is_(None))
                .order_by(PdfFile.is_primary.desc(), PdfFile.created_at.desc())
            )
        ).all()
    )


async def set_primary(session: AsyncSession, file: PdfFile) -> None:
    for other in await list_files(session, file.paper_id):
        other.is_primary = other.id == file.id
    await session.flush()


async def delete_file(session: AsyncSession, file: PdfFile) -> None:
    was_primary = file.is_primary
    file.deleted_at = datetime.now(UTC)
    file.is_primary = False
    if was_primary:
        replacement = await session.scalar(
            select(PdfFile)
            .where(
                PdfFile.paper_id == file.paper_id,
                PdfFile.id != file.id,
                PdfFile.deleted_at.is_(None),
            )
            .order_by(PdfFile.created_at.desc())
            .limit(1)
        )
        if replacement is not None:
            replacement.is_primary = True
    await enqueue(
        session,
        "gc_blob",
        {"backend": file.storage_backend, "key": file.storage_key},
        run_at=datetime.now(UTC),
    )
    await session.flush()


async def extract_file(session: AsyncSession, file_id: uuid.UUID) -> PdfFile:
    file = await get_file(session, file_id)
    storage = get_storage()
    data = await storage.read_bytes(file.storage_key)
    try:
        page_count, pages, language = await asyncio.to_thread(extract_pdf, data)
    except Exception:
        file.text_status = PdfTextStatus.failed
        await session.flush()
        raise
    await session.execute(delete(PdfPageText).where(PdfPageText.pdf_file_id == file.id))
    session.add_all(
        [
            PdfPageText(
                pdf_file_id=file.id,
                page=index,
                text=text,
                char_count=len(text),
            )
            for index, text in enumerate(pages, start=1)
        ]
    )
    file.page_count = page_count
    file.text_language = language
    # A PDF whose every page extracted to whitespace is a scan or an image-only
    # export: there is no OCR here, so claiming "extracted" would let callers
    # read an empty paper and conclude the search index is broken.
    file.text_status = (
        PdfTextStatus.extracted if any(page.strip() for page in pages) else PdfTextStatus.ocr_needed
    )
    file.extracted_at = datetime.now(UTC)
    await session.flush()
    return file


async def create_annotation(
    session: AsyncSession,
    *,
    paper: Paper,
    file_id: uuid.UUID,
    kind: str,
    page: int,
    rects: list[dict[str, float]],
    quote: str,
    prefix: str | None,
    suffix: str | None,
    comment: str | None,
    color: str,
) -> Annotation:
    file = await get_file(session, file_id)
    if file.paper_id != paper.id:
        raise ApiError(422, "file_paper_mismatch", "PDF does not belong to paper")
    if page < 1 or (file.page_count is not None and page > file.page_count):
        raise ApiError(422, "invalid_page", "Annotation page is out of range")
    try:
        annotation_kind = AnnotationKind(kind)
    except ValueError as exc:
        raise ApiError(422, "invalid_annotation", "Unsupported annotation kind") from exc
    if color not in {"yellow", "green", "blue", "pink", "purple"}:
        raise ApiError(422, "invalid_color", "Unsupported annotation color")
    if len(quote) > 4000 or (comment and len(comment) > 10000):
        raise ApiError(422, "annotation_too_long", "Annotation text is too long")
    for rect in rects:
        if not all(0 <= float(rect.get(key, -1)) <= 1 for key in ("x", "y", "w", "h")):
            raise ApiError(422, "invalid_rect", "Annotation rects must be 0..1")
    annotation = Annotation(
        id=uuid.uuid4(),
        paper_id=paper.id,
        pdf_file_id=file.id,
        kind=annotation_kind,
        page=page,
        rects=rects,
        quote=quote,
        prefix=prefix,
        suffix=suffix,
        comment=comment,
        color=color,
    )
    session.add(annotation)
    await session.flush()
    return annotation


async def list_annotations(
    session: AsyncSession, paper_id: uuid.UUID, *, page: int | None = None
) -> list[Annotation]:
    stmt = select(Annotation).where(
        Annotation.paper_id == paper_id, Annotation.deleted_at.is_(None)
    )
    if page is not None:
        stmt = stmt.where(Annotation.page == page)
    return list(
        (await session.scalars(stmt.order_by(Annotation.page, Annotation.created_at))).all()
    )


async def get_annotation(session: AsyncSession, annotation_id: uuid.UUID) -> Annotation:
    annotation = await session.get(Annotation, annotation_id)
    if annotation is None or annotation.deleted_at is not None:
        raise ApiError(404, "not_found", "Annotation not found")
    return annotation


async def search_pages(
    session: AsyncSession,
    query: str,
    *,
    file_id: uuid.UUID | None = None,
    paper_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    limit: int = 20,
    include_staged: bool = False,
    max_per_paper: int | None = None,
) -> list[dict[str, Any]]:
    """Search extracted page text.

    Scoping is also a visibility decision. An explicit file/paper/workspace
    scope is the owner reading their own material, so it reaches staged
    (research-workspace) papers; only the unscoped library search keeps them
    hidden unless ``include_staged`` is set.
    """
    query = query.strip()
    if not query:
        return []
    tsquery = func.websearch_to_tsquery("english", query)
    stmt = (
        select(PdfPageText, PdfFile, Paper)
        .join(PdfFile, PdfFile.id == PdfPageText.pdf_file_id)
        .join(Paper, Paper.id == PdfFile.paper_id)
        .where(
            PdfFile.deleted_at.is_(None),
            Paper.deleted_at.is_(None),
            or_(
                PdfPageText.tsv.op("@@")(tsquery),
                PdfPageText.text.ilike(f"%{query}%"),
            ),
        )
        .order_by(Paper.created_at.desc(), PdfPageText.page)
    )
    if file_id is not None:
        stmt = stmt.where(PdfPageText.pdf_file_id == file_id)
    elif paper_id is not None:
        stmt = stmt.where(PdfFile.paper_id == paper_id)
    elif workspace_id is not None:
        members = select(ResearchWorkspacePaper.paper_id).where(
            ResearchWorkspacePaper.workspace_id == workspace_id
        )
        stmt = stmt.where(PdfFile.paper_id.in_(members))
    elif not include_staged:
        # Library-wide search must not surface staging-area papers.
        stmt = stmt.where(Paper.staged.is_(False))

    # Over-fetch when capping per paper: the cap is applied after the SQL
    # limit, so a single chatty paper must not crowd out the other hits.
    fetch_limit = limit if max_per_paper is None else min(limit * 8, 500)
    rows = (await session.execute(stmt.limit(fetch_limit))).all()
    hits = [
        {
            "paper_id": paper.id,
            "paper_title": paper.title,
            "file_id": file.id,
            "page": page_text.page,
            "text": page_text.text,
        }
        for page_text, file, paper in rows
    ]
    if max_per_paper is None:
        return hits[:limit]
    kept: list[dict[str, Any]] = []
    seen: Counter[uuid.UUID] = Counter()
    for hit in hits:
        if seen[hit["paper_id"]] >= max_per_paper:
            continue
        seen[hit["paper_id"]] += 1
        kept.append(hit)
        if len(kept) >= limit:
            break
    return kept


async def media_counts(
    session: AsyncSession, paper_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    if not paper_ids:
        return {}
    files = await session.execute(
        select(PdfFile.paper_id, func.count())
        .where(PdfFile.paper_id.in_(paper_ids), PdfFile.deleted_at.is_(None))
        .group_by(PdfFile.paper_id)
    )
    annotations = await session.execute(
        select(Annotation.paper_id, func.count())
        .where(Annotation.paper_id.in_(paper_ids), Annotation.deleted_at.is_(None))
        .group_by(Annotation.paper_id)
    )
    result: dict[uuid.UUID, dict[str, int]] = {
        paper_id: {"pdf_files": 0, "annotations": 0} for paper_id in paper_ids
    }
    for paper_id, count in files.all():
        result[paper_id]["pdf_files"] = int(count)
    for paper_id, count in annotations.all():
        result[paper_id]["annotations"] = int(count)
    return result
