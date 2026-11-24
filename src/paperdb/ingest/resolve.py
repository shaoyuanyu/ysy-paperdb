from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import replace
from datetime import date
from difflib import SequenceMatcher
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.config import Settings
from paperdb.db.models import Paper, PaperIdentifier, Venue, VenueRank
from paperdb.ingest.acl import fetch_acl
from paperdb.ingest.arxiv import fetch_arxiv
from paperdb.ingest.crossref import fetch_crossref
from paperdb.ingest.dblp import search_dblp
from paperdb.ingest.dblp_record import fetch_dblp_record
from paperdb.ingest.http import build_client
from paperdb.ingest.openalex import fetch_openalex, search_openalex
from paperdb.ingest.openreview import fetch_openreview
from paperdb.ingest.s2 import fetch_s2
from paperdb.ingest.types import AuthorRecord, Candidate, InputRef, SourceRecord
from paperdb.ingest.venue_resolve import suggest_venue
from paperdb.venues.normalize import normalize_name
from paperdb.venues.rank import resolve_evaluation

_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_ARXIV_RE = re.compile(r"(?:arxiv[:\s]*)?(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
_ACL_RE = re.compile(r"aclanthology\.org/([^/?#]+)", re.IGNORECASE)
_OPENREVIEW_RE = re.compile(r"openreview\.net/forum\?id=([^&]+)", re.IGNORECASE)
_DBLP_RE = re.compile(r"dblp\.org/rec/([^?#\s]+)", re.IGNORECASE)

_SOURCE_ORDER = {
    "doi": ["crossref", "openalex", "dblp", "s2", "arxiv"],
    "arxiv": ["arxiv", "s2", "crossref", "dblp", "openalex"],
    "title": ["dblp", "crossref", "openalex", "s2", "arxiv"],
    "acl": ["acl", "dblp", "crossref", "openalex", "s2"],
    "openreview": ["openreview", "dblp", "crossref", "openalex", "s2"],
    "dblp": ["dblp", "crossref", "openalex", "s2"],
}


def identify_input(raw: str) -> InputRef:
    value = raw.strip()
    doi = _DOI_RE.search(value)
    if doi:
        return InputRef(raw=raw, kind="doi", value=doi.group(1).rstrip(".,;"))
    arxiv = _ARXIV_RE.search(value)
    if arxiv:
        return InputRef(raw=raw, kind="arxiv", value=arxiv.group(1))
    acl = _ACL_RE.search(value)
    if acl:
        return InputRef(raw=raw, kind="acl", value=acl.group(1).rstrip("/").removesuffix(".bib"))
    openreview = _OPENREVIEW_RE.search(value)
    if openreview:
        return InputRef(raw=raw, kind="openreview", value=openreview.group(1))
    dblp = _DBLP_RE.search(value)
    if dblp:
        return InputRef(
            raw=raw,
            kind="dblp",
            value=dblp.group(1).removesuffix(".html").removesuffix(".xml"),
        )
    return InputRef(raw=raw, kind="title", value=value)


def _pick(records: list[SourceRecord], attr: str, order: list[str]) -> Any:
    by_source = {record.source: record for record in records}
    for source in order:
        record = by_source.get(source)
        if record is not None:
            value = getattr(record, attr)
            if value not in (None, "", []):
                return value
    return None


def _similar(left: str, right: str) -> bool:
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return False
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.92


def _merge_authors(records: list[SourceRecord], order: list[str]) -> list[AuthorRecord]:
    by_source = {record.source: record for record in records}
    source_with_most = None
    best_count = 0
    for source in order:
        record = by_source.get(source)
        if record and len(record.authors) > best_count:
            source_with_most = record
            best_count = len(record.authors)
    if source_with_most is None:
        return []
    authors = [replace(author) for author in source_with_most.authors]
    for author in authors:
        key = normalize_name(author.name)
        for record in records:
            for other in record.authors:
                if normalize_name(other.name) != key:
                    continue
                if not author.orcid:
                    author.orcid = other.orcid
                if not author.dblp_pid:
                    author.dblp_pid = other.dblp_pid
                if not author.s2_author_id:
                    author.s2_author_id = other.s2_author_id
                if not author.openalex_id:
                    author.openalex_id = other.openalex_id
                if not author.affiliation:
                    author.affiliation = other.affiliation
    return authors


def _identifiers(records: list[SourceRecord]) -> list[dict[str, str | bool]]:
    wanted = [
        ("doi", "doi"),
        ("arxiv", "arxiv"),
        ("dblp", "dblp_key"),
        ("openalex", "openalex_id"),
        ("s2", "s2_id"),
        ("acl", "acl_id"),
        ("openreview", "openreview_id"),
    ]
    values: list[tuple[str, str]] = []
    for scheme, attr in wanted:
        for record in records:
            value = getattr(record, attr)
            if value:
                value = value.strip()
                if scheme == "doi":
                    value = value.lower()
                if (scheme, value) not in values:
                    values.append((scheme, value))
    primary = None
    for scheme in ("doi", "arxiv", "dblp", "openalex", "s2", "acl", "openreview"):
        for candidate_scheme, value in values:
            if candidate_scheme == scheme:
                primary = (candidate_scheme, value)
                break
        if primary:
            break
    return [
        {"scheme": scheme, "value": value, "isPrimary": (scheme, value) == primary}
        for scheme, value in values
    ]


async def _duplicate(
    session: AsyncSession, identifiers: list[dict[str, str | bool]]
) -> dict[str, str] | None:
    for identifier in identifiers:
        row = await session.scalar(
            select(PaperIdentifier)
            .join(Paper, Paper.id == PaperIdentifier.paper_id)
            .where(
                PaperIdentifier.scheme == identifier["scheme"],
                PaperIdentifier.normalized == identifier["value"],
                Paper.deleted_at.is_(None),
            )
        )
        if row is None:
            continue
        paper = await session.get(Paper, row.paper_id)
        if paper is not None:
            return {"paperId": str(paper.id), "citekey": paper.citekey}
    return None


async def find_duplicate(
    session: AsyncSession, identifiers: list[dict[str, str | bool]]
) -> dict[str, str] | None:
    return await _duplicate(session, identifiers)


async def evaluate_venue(session: AsyncSession, venue_id: str | None, year: int) -> dict[str, Any]:
    return await _evaluate(session, venue_id, year)


async def _evaluate(session: AsyncSession, venue_id: str | None, year: int) -> dict[str, Any]:
    if not venue_id:
        return {"ccf": None, "cas": None}
    venue = await session.get(Venue, uuid.UUID(venue_id))
    if venue is None:
        return {"ccf": None, "cas": None}
    ranks = list(
        (await session.scalars(select(VenueRank).where(VenueRank.venue_id == venue.id))).all()
    )
    return resolve_evaluation(venue, ranks, year)


async def _collect(
    ref: InputRef,
    client: httpx.AsyncClient,
    settings: Settings,
) -> list[SourceRecord]:
    if ref.kind == "doi":
        records = await asyncio.gather(
            fetch_crossref(client, ref.value, settings),
            fetch_openalex(client, ref.value, settings),
            fetch_s2(client, settings, doi=ref.value),
            return_exceptions=True,
        )
        found = [record for record in records if isinstance(record, SourceRecord)]
    elif ref.kind == "arxiv":
        records = await asyncio.gather(
            fetch_arxiv(client, ref.value, settings),
            fetch_s2(client, settings, arxiv=ref.value),
            return_exceptions=True,
        )
        found = [record for record in records if isinstance(record, SourceRecord)]
    elif ref.kind == "title":
        records = await asyncio.gather(
            search_dblp(client, ref.value, settings),
            search_openalex(client, ref.value, settings),
            return_exceptions=True,
        )
        found = []
        for result in records:
            if isinstance(result, list):
                found.extend(result)
            elif isinstance(result, SourceRecord):
                found.append(result)
        return found
    elif ref.kind == "acl":
        record = await fetch_acl(client, ref.value, settings)
        found = [record] if record else []
    elif ref.kind == "openreview":
        record = await fetch_openreview(client, ref.value, settings)
        found = [record] if record else []
    elif ref.kind == "dblp":
        record = await fetch_dblp_record(client, ref.value, settings)
        found = [record] if record else []
    else:
        return []

    # DBLP is the best venue/author-identity source. Enrich DOI/arXiv/ACL/
    # OpenReview lookups with a title search after the primary record is known.
    title = _pick(found, "title", ["crossref", "arxiv", "dblp", "openalex", "s2"])
    if title and ref.kind in {"doi", "arxiv", "acl", "openreview"}:
        dblp = await search_dblp(client, title, settings)
        for record in dblp:
            if record.title and _similar(record.title, title):
                found.append(record)
    return found


def _candidate(records: list[SourceRecord], input_kind: str, raw: str) -> Candidate | None:
    if not records:
        return None
    order = _SOURCE_ORDER.get(input_kind, _SOURCE_ORDER["title"])
    title = _pick(records, "title", order)
    if not title:
        return None
    kind = _pick(records, "kind", order)
    arxiv = _pick(records, "arxiv", order)
    if kind is None:
        kind = "preprint" if arxiv else "journal"
    year = _pick(records, "year", order)
    if not isinstance(year, int):
        year = date.today().year
    authors = _merge_authors(records, order)
    identifiers = _identifiers(records)
    venue_name = _pick(
        records, "venue", ["dblp", "crossref", "acl", "openreview", "openalex", "s2"]
    )
    dblp_venue_key = _pick(records, "dblp_venue_key", order)
    edition_key = _pick(records, "dblp_edition_key", order)
    return Candidate(
        confidence=0.0,
        sources=list(dict.fromkeys(record.source for record in records)),
        kind=kind,
        title=title,
        abstract=_pick(records, "abstract", order),
        year=year,
        publication_date=_pick(records, "publication_date", order),
        volume=_pick(records, "volume", order),
        issue=_pick(records, "issue", order),
        pages=_pick(records, "pages", order),
        authors=authors,
        identifiers=identifiers,
        venue=None,
        evaluation={"ccf": None, "cas": None},
        warnings=[],
        _venue_name=venue_name,
        _dblp_venue_key=dblp_venue_key,
        _edition_key=edition_key,
    )


async def resolve_input(
    session: AsyncSession,
    raw: str,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[Candidate | None, list[str]]:
    ref = identify_input(raw)
    warnings: list[str] = []
    owns_client = client is None
    client = client or build_client(settings)
    try:
        records = await _collect(ref, client, settings)
    finally:
        if owns_client:
            await client.aclose()
    candidate = _candidate(records, ref.kind, raw)
    if candidate is None:
        warnings.append("no metadata source returned a record")
        return None, warnings

    venue_name = candidate._venue_name
    dblp_venue_key = candidate._dblp_venue_key
    edition_key = candidate._edition_key
    candidate.venue = await suggest_venue(
        session,
        raw_name=venue_name,
        kind=candidate.kind,
        year=candidate.year,
        dblp_venue_key=dblp_venue_key,
        edition_dblp_key=edition_key,
    )
    if candidate.venue is not None:
        candidate.evaluation = await _evaluate(session, candidate.venue.venue_id, candidate.year)
        if candidate.venue.venue_id is None:
            warnings.append("venue not in library; provisional venue will be created on import")
    elif candidate.kind in {"conference", "journal"}:
        warnings.append("venue missing from all sources")
    if candidate.abstract is None:
        warnings.append("abstract missing")
    if len(candidate.authors) == 0:
        warnings.append("authors missing")

    candidate.duplicate_of = await _duplicate(session, candidate.identifiers)
    if candidate.duplicate_of:
        warnings.append("already in library")
    candidate.confidence = min(
        0.55
        + 0.15 * (len(candidate.sources) - 1)
        + (0.1 if candidate.venue and candidate.venue.venue_id else 0.0)
        + (0.05 if any(author.orcid or author.dblp_pid for author in candidate.authors) else 0.0),
        0.99,
    )
    candidate.warnings = warnings
    return candidate, warnings
