from __future__ import annotations

import asyncio
import re
import uuid
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import require_totp, scoped
from paperdb.api.schemas import (
    PdfFileOut,
    PdfFromUrlRequest,
    PdfOutlineOut,
    PdfPageTextOut,
    PdfTextOut,
    PdfUploadOut,
)
from paperdb.auth.tokens import TokenPrincipal
from paperdb.config import Settings, get_settings
from paperdb.db.models import PdfFile, PdfPageText
from paperdb.db.session import get_session
from paperdb.errors import ApiError
from paperdb.imports.pdf_import import fetch_pdf_bytes
from paperdb.jobs.queue import enqueue
from paperdb.papers.service import get_paper
from paperdb.pdf.extract import extract_outline, render_page_png
from paperdb.pdf.service import (
    create_file,
    delete_file,
    get_file,
    list_files,
    set_primary,
)
from paperdb.storage import get_storage

router = APIRouter(tags=["files"])


def _filename_from_url(url: str) -> str | None:
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return name[:200] if name and "." in name else None


def _out(file: PdfFile) -> PdfFileOut:
    return PdfFileOut(
        id=file.id,
        paper_id=file.paper_id,
        is_primary=file.is_primary,
        byte_size=file.byte_size,
        page_count=file.page_count,
        mime=file.mime,
        original_filename=file.original_filename,
        text_status=file.text_status.value,
        text_language=file.text_language,
        created_at=file.created_at,
    )


@router.post(
    "/papers/{ref}/files", response_model=PdfUploadOut, status_code=status.HTTP_201_CREATED
)
async def upload_endpoint(
    ref: str,
    file: UploadFile = File(...),
    principal: TokenPrincipal = Depends(scoped("pdf:write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PdfUploadOut:
    paper = await get_paper(session, ref)
    data = await file.read()
    record, job_id = await create_file(
        session,
        paper=paper,
        data=data,
        filename=file.filename,
        settings=settings,
    )
    await session.commit()
    return PdfUploadOut(file=_out(record), job_id=job_id)


@router.post(
    "/papers/{ref}/files/from-url", response_model=PdfUploadOut, status_code=status.HTTP_201_CREATED
)
async def upload_from_url_endpoint(
    ref: str,
    payload: PdfFromUrlRequest,
    principal: TokenPrincipal = Depends(scoped("pdf:write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PdfUploadOut:
    """Attach a PDF (fetched from a direct URL) to an existing paper.

    The repair path for metadata-only imports: no new paper is created and no
    duplicate can appear, unlike the import endpoints.
    """
    paper = await get_paper(session, ref)
    data = await fetch_pdf_bytes(payload.url, settings)
    record, job_id = await create_file(
        session,
        paper=paper,
        data=data,
        filename=payload.filename or _filename_from_url(payload.url),
        settings=settings,
        source_url=payload.url,
    )
    await session.commit()
    return PdfUploadOut(file=_out(record), job_id=job_id)


@router.get("/papers/{ref}/files", response_model=list[PdfFileOut])
async def list_endpoint(
    ref: str,
    principal: TokenPrincipal = Depends(scoped("pdf:read")),
    session: AsyncSession = Depends(get_session),
) -> list[PdfFileOut]:
    paper = await get_paper(session, ref)
    return [_out(file) for file in await list_files(session, paper.id)]


@router.get("/files/{file_id}", response_model=PdfFileOut)
async def get_endpoint(
    file_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("pdf:read")),
    session: AsyncSession = Depends(get_session),
) -> PdfFileOut:
    return _out(await get_file(session, file_id))


@router.get("/files/{file_id}/content")
async def content_endpoint(
    file_id: uuid.UUID,
    request: Request,
    range_header: str | None = Header(default=None, alias="Range"),
    principal: TokenPrincipal = Depends(scoped("pdf:read")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    file = await get_file(session, file_id)
    storage = get_storage()
    size = file.byte_size
    start = 0
    end = max(size - 1, 0)
    status_code = 200
    if range_header:
        match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match:
            raise ApiError(416, "invalid_range", "Invalid Range header")
        if match.group(1):
            start = int(match.group(1))
        if match.group(2):
            end = min(int(match.group(2)), size - 1)
        if start >= size or start > end:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}"},
            )
        status_code = 206
    data = await storage.read_range(file.storage_key, start, end)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", file.original_filename or "paper.pdf")
    headers = {
        "Content-Type": "application/pdf",
        "Content-Disposition": f'inline; filename="{safe_name}"',
        "Accept-Ranges": "bytes",
        "Content-Length": str(len(data)),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
        "Cache-Control": "private, no-store",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(content=data, status_code=status_code, headers=headers)


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    file_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("pdf:write")),
    session: AsyncSession = Depends(get_session),
    totp: None = Depends(require_totp),
) -> Response:
    file = await get_file(session, file_id)
    await delete_file(session, file)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/files/{file_id}/primary", response_model=PdfFileOut)
async def primary_endpoint(
    file_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("pdf:write")),
    session: AsyncSession = Depends(get_session),
) -> PdfFileOut:
    file = await get_file(session, file_id)
    await set_primary(session, file)
    await session.commit()
    return _out(file)


@router.post("/files/{file_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex_endpoint(
    file_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("pdf:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, uuid.UUID]:
    file = await get_file(session, file_id)
    job = await enqueue(session, "extract_pdf", {"file_id": str(file.id)})
    await session.commit()
    return {"job_id": job.id}


@router.get("/files/{file_id}/text", response_model=PdfTextOut)
async def text_endpoint(
    file_id: uuid.UUID,
    page_from: int = Query(default=1, ge=1, alias="from"),
    page_to: int | None = Query(default=None, ge=1, alias="to"),
    max_pages: int = Query(default=100, ge=1, le=500, alias="maxPages"),
    max_chars: int | None = Query(default=None, ge=1000, le=2000000, alias="maxChars"),
    principal: TokenPrincipal = Depends(scoped("pdf:read")),
    session: AsyncSession = Depends(get_session),
) -> PdfTextOut:
    """A page range of extracted text.

    The window is measured in pages, not characters: one call reads every page
    from ``page_from`` up to ``max_pages`` pages (default 100) — i.e. a whole
    paper unless it is unusually long. ``text_status`` tells the caller whether
    an empty answer means "no text here" or "not extracted yet".
    """
    file = await get_file(session, file_id)
    window_end = page_from + max_pages - 1
    stop = window_end if page_to is None else min(window_end, page_to)
    stmt = (
        select(PdfPageText)
        .where(
            PdfPageText.pdf_file_id == file_id,
            PdfPageText.page >= page_from,
            PdfPageText.page <= stop,
        )
        .order_by(PdfPageText.page)
    )
    rows = list((await session.execute(stmt)).scalars())

    pages: list[PdfPageTextOut] = []
    used = 0
    next_page: int | None = None
    for row in rows:
        text = row.text or ""
        # Always return at least one page, even when it alone blows the budget:
        # a truncated-but-empty answer is useless to the caller.
        if max_chars is not None and pages and used + len(text) > max_chars:
            next_page = row.page
            break
        pages.append(PdfPageTextOut(page=row.page, text=text, char_count=len(text)))
        used += len(text)
    if next_page is None and len(pages) < len(rows):
        next_page = rows[len(pages)].page
    if pages and next_page is None:
        # The window may have ended before the document (max_pages or page_to).
        last = pages[-1].page
        boundary = page_to if page_to is not None else (file.page_count or last)
        if last < boundary:
            next_page = last + 1
    return PdfTextOut(
        file_id=file.id,
        text_status=file.text_status.value,
        page_count=file.page_count,
        pages=pages,
        truncated=next_page is not None,
        next_page=next_page,
    )


@router.get("/files/{file_id}/outline", response_model=PdfOutlineOut)
async def outline_endpoint(
    file_id: uuid.UUID,
    principal: TokenPrincipal = Depends(scoped("pdf:read")),
    session: AsyncSession = Depends(get_session),
) -> PdfOutlineOut:
    """The PDF's bookmark tree (section title + page), when it has one.

    An empty ``sections`` list is normal — most papers ship without bookmarks.
    """
    file = await get_file(session, file_id)
    data = await get_storage().read_bytes(file.storage_key)
    try:
        sections = await asyncio.to_thread(extract_outline, data)
    except ValueError as exc:
        raise ApiError(422, "invalid_pdf", str(exc)) from exc
    return PdfOutlineOut(
        file_id=file.id,
        page_count=file.page_count,
        sections=sections,  # type: ignore[arg-type]
    )


@router.get("/files/{file_id}/pages/{page}/image")
async def page_image_endpoint(
    file_id: uuid.UUID,
    page: int,
    dpi: int = Query(default=144, ge=72, le=300),
    principal: TokenPrincipal = Depends(scoped("pdf:read")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render one PDF page to PNG (figures, tables, equations, layout).

    Text extraction cannot carry what a reader sees on the page; this is the
    other half of reading a paper.
    """
    file = await get_file(session, file_id)
    data = await get_storage().read_bytes(file.storage_key)
    try:
        png = await asyncio.to_thread(render_page_png, data, page, dpi)
    except ValueError as exc:
        raise ApiError(422, "invalid_page", str(exc)) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.get("/files/{file_id}/pages/{page}/text")
async def page_text_endpoint(
    file_id: uuid.UUID,
    page: int,
    principal: TokenPrincipal = Depends(scoped("pdf:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await get_file(session, file_id)
    row = await session.get(PdfPageText, (file_id, page))
    if row is None:
        raise ApiError(404, "not_found", "Page text not found")
    return {"page": row.page, "text": row.text, "charCount": row.char_count}
