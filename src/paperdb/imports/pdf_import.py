from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx
import pymupdf
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.config import Settings
from paperdb.db.models import Paper, PdfFile
from paperdb.errors import ApiError
from paperdb.imports.service import _paper_payload, merge_new_identifiers
from paperdb.ingest.http import build_client
from paperdb.ingest.resolve import fuzzy_library_match, resolve_input
from paperdb.ingest.types import AuthorRecord, Candidate
from paperdb.ingest.venue_resolve import ensure_venue
from paperdb.papers.service import create_paper, normalize_title
from paperdb.pdf.service import create_file

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"arXiv[:\s]+(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
_ACL_RE = re.compile(r"\b((?:19|20)\d{2}\.[a-z0-9-]+\.\d+)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
# Running heads of proceedings volumes ("Findings of the ACL 2026, pages
# 15020-15037") are the most common PDF-metadata trap: they are long, early,
# and look like a title to every naive line picker.
_RUNNING_HEADER_RE = re.compile(
    r"(?i)^(?:in\s+)?(?:proceedings|findings|lecture notes|advances in|"
    r"conference proceedings|workshop proceedings)\b"
)
_PAGES_RE = re.compile(r"(?i)\bpages?\s+\d+\s*[-\u2013]\s*\d+\b")
_SERIAL_RE = re.compile(r"(?i)\b(?:issn|isbn|doi\s*:|https?://)\b")


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


def _looks_like_running_header(text: str) -> bool:
    return bool(_RUNNING_HEADER_RE.match(text) or _PAGES_RE.search(text) or _SERIAL_RE.search(text))


def _plausible_title(text: str) -> bool:
    return (
        8 <= len(text) <= 500
        and not re.fullmatch(r"(?i)(untitled|document|paper|pdf)", text)
        and not text.lower().endswith(".pdf")
        and not _looks_like_running_header(text)
    )


def _title_from_page_layout(page: pymupdf.Page) -> str | None:
    """Pick the visually dominant text block in the top half of page 1.

    Font size is what separates the title from the running head, the author
    list and the affiliations; line order alone cannot.
    """
    height = float(page.rect.height) or 1.0
    layout: list[tuple[float, float, float, str]] = []
    payload = page.get_text("dict")
    blocks = payload.get("blocks", []) if isinstance(payload, dict) else []
    for block in blocks:
        if block.get("type") != 0:
            continue
        bbox = block.get("bbox", (0, 0, 0, 0))
        y0, y1 = float(bbox[1]), float(bbox[3])
        if y0 > height * 0.55:
            continue
        lines = [line for line in block.get("lines", []) if line.get("spans")]
        if not lines:
            continue
        text = _clean(
            " ".join("".join(span.get("text", "") for span in line["spans"]) for line in lines)
        )
        if not _plausible_title(text) or "@" in text:
            continue
        size = max(float(span.get("size", 0.0)) for line in lines for span in line.get("spans", []))
        layout.append((size, y0, y1, text))
    if not layout:
        return None
    # The title is the topmost block set in (near) the page's largest font;
    # length is irrelevant — long affiliation lines are set in body size. A
    # wrapped title is stitched back together from consecutive blocks.
    largest = max(item[0] for item in layout)
    prominent = [item for item in layout if item[0] >= largest * 0.85]
    prominent.sort(key=lambda item: item[1])
    size0 = prominent[0][0]
    parts = [prominent[0][3]]
    previous_bottom = prominent[0][2]
    for size, y0, y1, text in prominent[1:]:
        if abs(size - size0) > max(1.0, size0 * 0.12) or y0 - previous_bottom > height * 0.045:
            break
        if sum(len(part) for part in parts) + len(text) > 350:
            break
        parts.append(text)
        previous_bottom = y1
    return " ".join(parts)


def _title_from_metadata(
    raw: str | None, first_page: str, filename: str | None, layout_title: str | None = None
) -> str:
    title = _clean(raw)
    if title.lower().startswith("microsoft word - "):
        title = _clean(title[len("Microsoft Word - ") :])
    title = title.strip(" -_")
    if _plausible_title(title):
        return title[:500]
    if layout_title:
        return layout_title[:500]
    # Last resort: the longest plausible early line, skipping headers/author
    # lines. Deliberately conservative.
    candidates = []
    for line in first_page.splitlines()[:12]:
        line = _clean(line)
        if 12 <= len(line) <= 300 and not re.search(r"@|https?://|doi\.org", line, re.I):
            if _looks_like_running_header(line):
                continue
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
        layout_title = None
        if len(document):
            first_page = str(document[0].get_text("text") or "")
            layout_title = _title_from_page_layout(document[0])
        title = _title_from_metadata(metadata.get("title"), first_page, filename, layout_title)
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
    file_id: uuid.UUID | None
    job_id: uuid.UUID | None
    created: bool
    title: str
    warnings: list[str]


def _candidate_doi(candidate: Candidate) -> str | None:
    for identifier in candidate.identifiers:
        if identifier["scheme"] == "doi":
            return str(identifier["value"])
    return None


async def _paper_for_candidate(
    session: AsyncSession,
    candidate: Candidate,
    *,
    title_match_doi: str | None,
) -> tuple[Paper, bool, list[str]]:
    """Create (or link) the Paper for a resolved candidate.

    Mirrors the import flow: identifier duplicate first (with identifier
    merge), then a strong fuzzy match (exact title + year + first author),
    then a normalized-title match only when a DOI corroborates it, else
    create. Returns (paper, created, notes).
    """
    notes: list[str] = []
    duplicate_id = None
    if candidate.duplicate_of:
        duplicate_id = candidate.duplicate_of.get("paperId")
    if not duplicate_id:
        # PDF-fallback candidates never went through resolve_input; compute
        # the fuzzy match here so identifier-less PDFs of existing entries
        # (e.g. re-uploads of zero-identifier papers) still link.
        suggestion = candidate.suggested_duplicate_of
        if suggestion is None:
            suggestion = await fuzzy_library_match(session, candidate)
        if suggestion and suggestion.get("strong"):
            duplicate_id = str(suggestion["paperId"])
            notes.append(f"linked by {suggestion.get('evidence', 'fuzzy')} match")
    if duplicate_id:
        row = await session.get(Paper, uuid.UUID(duplicate_id))
        if row is not None and row.deleted_at is None:
            merged = await merge_new_identifiers(session, row, candidate.identifiers)
            if merged:
                notes.append(f"+{len(merged)} identifier(s) merged")
            return row, False, notes

    existing_by_title = await session.scalar(
        select(Paper).where(
            Paper.title_normalized == normalize_title(candidate.title),
            Paper.deleted_at.is_(None),
        )
    )
    if existing_by_title is not None and title_match_doi:
        return existing_by_title, False, notes

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
            _paper_payload(candidate, venue.id if venue else None, edition.id if edition else None),
        )
    except ApiError as exc:
        if exc.code != "citekey_conflict" or not candidate.citekey:
            raise
        candidate.citekey = None
        paper = await create_paper(
            session,
            _paper_payload(candidate, venue.id if venue else None, edition.id if edition else None),
        )
    return paper, True, notes


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

    # The same file may already be attached to an existing entry (re-upload,
    # "attach PDF" of a paper imported earlier). Content is the strongest
    # duplicate signal there is: link instead of importing it twice.
    digest = hashlib.sha256(data).hexdigest()
    existing = await session.scalar(
        select(PdfFile).where(PdfFile.sha256 == digest, PdfFile.deleted_at.is_(None)).limit(1)
    )
    if existing is not None:
        existing_paper = await session.get(Paper, existing.paper_id)
        if existing_paper is not None and existing_paper.deleted_at is None:
            return PdfImportResult(
                paper=existing_paper,
                file_id=existing.id,
                job_id=None,
                created=False,
                title=existing_paper.title,
                warnings=["linked by identical PDF (same sha256); metadata lookup skipped"],
            )

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

    paper, created, link_notes = await _paper_for_candidate(
        session, candidate, title_match_doi=meta.doi
    )
    warnings.extend(link_notes)
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


async def _fetch_bytes(client: httpx.AsyncClient, url: str, settings: Settings) -> bytes | None:
    """GET a URL and return its bytes when it looks like a usable download."""
    try:
        response = await client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400 or len(response.content) > settings.max_upload_mb * 1024 * 1024:
        return None
    return response.content


def _filename_from_url(url: str, fallback: str) -> str:
    path = urlsplit(url).path
    base = path.rsplit("/", 1)[-1]
    if base and "." in base:
        return base[:200]
    return fallback


async def fetch_pdf_bytes(url: str, settings: Settings) -> bytes:
    """Download a PDF from a direct link (used to attach a file to a paper)."""
    if not url.startswith(("http://", "https://")):
        raise ApiError(422, "invalid_url", "Only http(s) URLs are supported")
    async with build_client(settings) as client:
        data = await _fetch_bytes(client, url, settings)
    if not data:
        raise ApiError(422, "pdf_fetch_failed", f"could not download a file from {url}")
    if not data.startswith(b"%PDF-"):
        raise ApiError(422, "not_a_pdf", f"{url} did not return a PDF")
    return data


async def import_pdf_from_url(
    session: AsyncSession,
    *,
    url: str,
    filename: str | None,
    settings: Settings,
) -> PdfImportResult:
    """Import a paper (and its PDF) from a landing-page or direct-PDF URL.

    Order of operations:
    1. resolve the paper via the landing page (citation meta / og tags / title
       search through the aggregator sources);
    2. download the first working PDF link found on the page
       (``citation_pdf_url`` and friends, exposed as ``candidate.pdf_urls``);
    3. create/link the paper and store the file. When no PDF can be fetched,
       the paper is still imported (metadata-only) with a warning.
    """
    if not url.startswith(("http://", "https://")):
        raise ApiError(422, "invalid_url", "Only http(s) URLs are supported")

    warnings: list[str] = []
    owns_client = True
    client = build_client(settings)
    try:
        candidate, resolve_warnings = await resolve_input(session, url, settings, client=client)
        warnings.extend(resolve_warnings)

        data: bytes | None = None
        source_url: str | None = None
        if candidate is not None:
            for pdf_url in candidate.pdf_urls[:3]:
                fetched = await _fetch_bytes(client, pdf_url, settings)
                if fetched and fetched.startswith(b"%PDF-"):
                    data = fetched
                    source_url = pdf_url
                    break

        if data is None:
            # Maybe the URL itself is a direct PDF link (or resolves to one).
            fetched = await _fetch_bytes(client, url, settings)
            if fetched and fetched.startswith(b"%PDF-"):
                if candidate is None:
                    # Direct PDF with no metadata: reuse the upload pipeline,
                    # which extracts identifiers from the first page.
                    return await import_pdf(
                        session,
                        data=fetched,
                        filename=filename or _filename_from_url(url, "downloaded.pdf"),
                        settings=settings,
                    )
                data = fetched
                source_url = url

        if candidate is None:
            raise ApiError(
                422,
                "resolve_failed",
                "; ".join(warnings) or "landing page gave no usable paper metadata",
            )

        paper, created, link_notes = await _paper_for_candidate(
            session, candidate, title_match_doi=_candidate_doi(candidate)
        )
        warnings.extend(link_notes)

        if data is None:
            if candidate.pdf_urls:
                warnings.append(
                    "PDF link(s) found on page but none downloadable; imported metadata only"
                )
            else:
                warnings.append("no PDF link found on page; imported metadata only")
            await session.flush()
            return PdfImportResult(
                paper=paper,
                file_id=None,
                job_id=None,
                created=created,
                title=paper.title,
                warnings=list(dict.fromkeys(warnings)),
            )

        file, job_id = await create_file(
            session,
            paper=paper,
            data=data,
            filename=filename
            or _filename_from_url(source_url or url, f"{paper.citekey or 'paper'}.pdf"),
            settings=settings,
            source_url=source_url or url,
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
    finally:
        if owns_client:
            await client.aclose()
