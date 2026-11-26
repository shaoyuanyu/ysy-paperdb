from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, File, Header, Request, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.deps import require_totp, scoped
from paperdb.api.schemas import PdfFileOut, PdfUploadOut
from paperdb.auth.tokens import TokenPrincipal
from paperdb.config import Settings, get_settings
from paperdb.db.models import PdfFile, PdfPageText
from paperdb.db.session import get_session
from paperdb.errors import ApiError
from paperdb.jobs.queue import enqueue
from paperdb.papers.service import get_paper
from paperdb.pdf.service import (
    create_file,
    delete_file,
    get_file,
    list_files,
    set_primary,
)
from paperdb.storage import get_storage

router = APIRouter(tags=["files"])


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
