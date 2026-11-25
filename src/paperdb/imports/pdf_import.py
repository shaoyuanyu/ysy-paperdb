from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pymupdf
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.config import Settings
from paperdb.db.models import Paper
from paperdb.errors import ApiError
from paperdb.imports.service import _paper_payload
from paperdb.ingest.resolve import resolve_input
from paperdb.ingest.types import AuthorRecord, Candidate
from paperdb.ingest.venue_resolve import ensure_venue
from paperdb.papers.service import create_paper, normalize_title
from paperdb.pdf.service import create_file

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"arXiv[:\s]+(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
_ACL_RE = re.compile(r"\b((?:19|20)\d{2}\.[a-z0-9-]+\.\d+)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


@dataclass(slots=True)
class PdfMetadata:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv: str | None = None
    acl_id: str | None = None
    first_page: str = ""


def _clean(value: str | None) -> str:
    return _WS_RE.sub(" ", value or "").strip()


def _title_from_metadata(raw: str | None, first_page: str, filename: str | None) -> str:
    title = _clean(raw)
    if title.lower().startswith("microsoft word - "):
        title = _clean(title[len("Microsoft Word - ") :])
    title = title.strip(" -_")
    if len(title) >= 8 and not re.fullmatch(r"(?i)(untitled|document|paper|pdf)", title):
        return title[:500]
    # Fall back to the longest plausible early line on the first page. This is
    # deliberately conservative: journal headers and author lines are skipped.
    candidates = []
    for line in first_page.splitlines()[:12]:
        line = _clean(line)
        if 12 <= len(line) <= 300 and not re.search(r"@|https?://|doi\.org", line, re.I):
            candidates.append(line)
    if candidates:
        return max(candidates, key=len)[:500]
    stem = (filename or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return _clean(stem) or "Untitled PDF"


def _year_from(text: str, created: str | None) -> int:
    if created:
        match = re.search(r"D:((?:19|20)\d{2})", created)
        if match:
            return int(match.group(1))
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return int(match.group(0)) if match else date.today().year


def _authors(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"\s*(?:;|\band\b)\s*", raw.strip())
    if len(parts) == 1 and "," in raw:
        # Metadata author fields are usually "Family, Given" lists separated by ;
        # but a single comma-separated field is ambiguous; keep it as one name.
        parts = [raw.strip()]
    return [name for name in (_clean(part) for part in parts) if name and name.lower() != "unknown"]


def inspect_pdf(data: bytes, filename: str | None = None) -> PdfMetadata:
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        if document.needs_pass:
            raise ValueError("encrypted PDF")
        metadata = document.metadata or {}
        first_page = ""
        if len(document):
            first_page = str(document[0].get_text("text") or "")
        title = _title_from_metadata(metadata.get("title"), first_page, filename)
        text = first_page[:12000]
        doi_match = _DOI_RE.search(text)
        doi = doi_match.group(0).rstrip(".,;)]}>").lower() if doi_match else None
        arxiv_match = _ARXIV_RE.search(text)
        arxiv = arxiv_match.group(1) if arxiv_match else None
        acl_match = _ACL_RE.search(text)
        acl_id = acl_match.group(1).lower() if acl_match else None
        if not acl_id and doi and doi.startswith("10.18653/v1/"):
            acl_id = doi.split("/v1/", 1)[1]
        return PdfMetadata(
            title=title,
            authors=_authors(metadata.get("author")),
            year=_year_from(text, metadata.get("creationDate")),
            doi=doi,
            arxiv=arxiv,
            acl_id=acl_id,
            first_page=text,
        )
    finally:
        document.close()


@dataclass(slots=True)
class PdfImportResult:
    paper: Paper
    file_id: uuid.UUID
    job_id: uuid.UUID | None
    created: bool
    title: str
    warnings: list[str]


def _fallback_candidate(meta: PdfMetadata, warnings: list[str]) -> Candidate:
    authors = []
    for name in meta.authors:
        parts = name.split()
        authors.append(
            AuthorRecord(
                name=name,
                family=parts[-1] if len(parts) > 1 else None,
                given=" ".join(parts[:-1]) if len(parts) > 1 else None,
            )
        )
    identifiers: list[dict[str, Any]] = []
    if meta.doi:
        identifiers.append({"scheme": "doi", "value": meta.doi, "isPrimary": True})
    if meta.arxiv:
        identifiers.append(
            {"scheme": "arxiv", "value": meta.arxiv, "isPrimary": not bool(meta.doi)}
        )
    if meta.acl_id:
        identifiers.append({"scheme": "acl", "value": meta.acl_id, "isPrimary": False})
    return Candidate(
        confidence=0.3,
        sources=["pdf"],
        kind="preprint",
        title=meta.title,
        abstract=None,
        year=meta.year or date.today().year,
        publication_date=None,
        authors=authors,
        identifiers=identifiers,
        venue=None,
        evaluation={"ccf": None, "cas": None},
        warnings=warnings,
    )


async def import_pdf(
    session: AsyncSession,
    *,
    data: bytes,
    filename: str | None,
    settings: Settings,
) -> PdfImportResult:
    if not data.startswith(b"%PDF-"):
        raise ApiError(415, "unsupported_media_type", "Only PDF uploads are accepted")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ApiError(413, "payload_too_large", "PDF is larger than the upload limit")
    try:
        meta = inspect_pdf(data, filename)
    except Exception as exc:
        raise ApiError(422, "invalid_pdf", f"Could not inspect PDF: {exc}") from exc

    warnings: list[str] = []
    candidate: Candidate | None = None
    lookup: str | None = None
    if meta.doi:
        lookup = f"doi:{meta.doi}"
    elif meta.arxiv:
        lookup = f"arxiv:{meta.arxiv}"
    elif meta.acl_id:
        lookup = f"https://aclanthology.org/{meta.acl_id}/"
    elif len(meta.title) >= 12:
        lookup = meta.title

    if lookup:
        try:
            candidate, lookup_warnings = await resolve_input(session, lookup, settings)
            warnings.extend(lookup_warnings)
        except Exception as exc:  # external metadata must not block the PDF import
            warnings.append(f"metadata lookup failed: {exc}")
    if candidate is None:
        candidate = _fallback_candidate(meta, warnings)
        warnings.append("created from PDF metadata; external metadata not matched")

    paper: Paper
    duplicate_id = candidate.duplicate_of.get("paperId") if candidate.duplicate_of else None
    if duplicate_id:
        existing = await session.get(Paper, uuid.UUID(duplicate_id))
        if existing is None or existing.deleted_at is not None:
            duplicate_id = None
    if duplicate_id:
        paper = existing  # type: ignore[assignment]
        created = False
    else:
        existing_by_title = await session.scalar(
            select(Paper).where(
                Paper.title_normalized == normalize_title(candidate.title),
                Paper.deleted_at.is_(None),
            )
        )
        if existing_by_title is not None and meta.doi:
            paper = existing_by_title
            created = False
        else:
            venue = None
            edition = None
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
            try:
                paper = await create_paper(
                    session,
                    _paper_payload(
                        candidate,
                        venue.id if venue else None,
                        edition.id if edition else None,
                    ),
                )
            except ApiError as exc:
                if exc.code != "citekey_conflict" or not candidate.citekey:
                    raise
                candidate.citekey = None
                paper = await create_paper(
                    session,
                    _paper_payload(
                        candidate,
                        venue.id if venue else None,
                        edition.id if edition else None,
                    ),
                )
            created = True
    file, job_id = await create_file(
        session,
        paper=paper,
        data=data,
        filename=filename,
        settings=settings,
    )
    await session.flush()
    return PdfImportResult(
        paper=paper,
        file_id=file.id,
        job_id=job_id,
        created=created,
        title=paper.title,
        warnings=list(dict.fromkeys(warnings)),
    )
