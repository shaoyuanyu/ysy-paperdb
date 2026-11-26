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
from paperdb.ingest.page import fetch_page
from paperdb.ingest.s2 import fetch_s2
from paperdb.ingest.types import AuthorRecord, Candidate, InputRef, SourceRecord
from paperdb.ingest.venue_resolve import suggest_venue
from paperdb.papers.service import normalize_title
from paperdb.venues.normalize import normalize_name
from paperdb.venues.rank import resolve_evaluation

_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_ARXIV_RE = re.compile(r"(?:arxiv[:\s]*)?(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
_ACL_RE = re.compile(r"aclanthology\.org/([^/?#]+)", re.IGNORECASE)
_OPENREVIEW_RE = re.compile(r"openreview\.net/forum\?id=([^&]+)", re.IGNORECASE)
_DBLP_RE = re.compile(r"dblp\.org/rec/([^?#\s]+)", re.IGNORECASE)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+\.\S+", re.IGNORECASE)

# Venues that describe the hosting repository rather than the publication.
# They must never win over a real conference/journal venue carried by another
# source for the same paper (typically the arXiv twin of a USENIX/NDSS paper).
_PREPRINT_VENUE_KEYS = {
    "arxiv",
    "arxiv org",
    "arxiv preprint",
    "ssrn",
    "biorxiv",
    "medrxiv",
    "research square",
    "techrxiv",
    "preprints org",
}

# "page" is always the last resort: landing-page metadata only fills fields
# the aggregator sources could not provide.
_SOURCE_ORDER = {
    "doi": ["crossref", "openalex", "dblp", "s2", "arxiv", "page"],
    "arxiv": ["arxiv", "s2", "crossref", "dblp", "openalex", "page"],
    "title": ["dblp", "crossref", "openalex", "s2", "arxiv", "page"],
    "acl": ["acl", "dblp", "crossref", "openalex", "s2", "page"],
    "openreview": ["openreview", "dblp", "crossref", "openalex", "s2", "page"],
    "dblp": ["dblp", "crossref", "openalex", "s2", "page"],
    "url": ["dblp", "crossref", "openalex", "s2", "arxiv", "acl", "openreview", "page"],
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
    if _URL_RE.match(value):
        # A generic landing page (venue site, publisher page, repository).
        # The specific academic URLs above matched first; anything left is
        # fetched and mined for identifiers/title by the url branch.
        url = value if value.startswith(("http://", "https://")) else f"https://{value}"
        return InputRef(raw=raw, kind="url", value=url)
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


def _title_matches(query: str, result_title: str | None) -> bool:
    """Accept a title-search result only when it plausibly answers the query.

    Aggregator search returns *some* paper for almost any text (verified: a
    Chinese small-talk string returns an unrelated paper). Guard the import
    path with: exact normalized equality, near-exact similarity, or most of
    the query's tokens appearing in the result title.
    """
    if not result_title:
        return False
    q = normalize_title(query)
    r = normalize_title(result_title)
    if not q or not r:
        return False
    if q == r or SequenceMatcher(None, q, r).ratio() >= 0.92:
        return True
    q_tokens = set(q.split())
    r_tokens = set(r.split())
    return bool(q_tokens) and len(q_tokens & r_tokens) / len(q_tokens) >= 0.6


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


def _first_author_family(name: str | None) -> str | None:
    tokens = (name or "").split()
    return tokens[-1].lower() if tokens else None


async def fuzzy_library_match(session: AsyncSession, candidate: Candidate) -> dict[str, Any] | None:
    """Exact-normalized-title match against the library (suggestion only).

    Papers imported without identifiers (PDF fallback, old imports) are
    invisible to the identifier-level duplicate check; this closes that hole
    at the only precision level fuzzy matching is allowed: exact normalized
    title, with year proximity and first-author agreement as corroboration.
    """
    normalized = normalize_title(candidate.title)
    if not normalized:
        return None
    rows = list(
        (
            await session.scalars(
                select(Paper)
                .where(Paper.title_normalized == normalized, Paper.deleted_at.is_(None))
                .limit(5)
            )
        ).all()
    )
    if not rows:
        return None

    def year_delta(row: Paper) -> int | None:
        if row.year is None:
            return None
        return abs(row.year - candidate.year)

    def delta_or_big(row: Paper) -> int:
        delta = year_delta(row)
        return delta if delta is not None else 999

    best = min(rows, key=delta_or_big)
    delta = year_delta(best)
    candidate_first = _first_author_family(candidate.authors[0].name if candidate.authors else None)
    library_first = _first_author_family((best.paper_author_names or "").split(",")[0])
    author_match: bool | None = None
    if candidate_first and library_first:
        author_match = candidate_first == library_first
    strong = delta is not None and delta <= 1 and author_match is not False
    evidence = "exact-title"
    if delta is not None:
        evidence += f"+year({delta})"
    if author_match:
        evidence += "+first-author"
    return {
        "paperId": str(best.id),
        "citekey": best.citekey,
        "title": best.title,
        "year": best.year,
        "yearDelta": delta,
        "authorMatch": author_match,
        "strong": strong,
        "evidence": evidence,
    }


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


def _page_record(page) -> SourceRecord | None:
    """Build a last-resort SourceRecord from the landing page itself."""
    if not page.title:
        return None
    authors = []
    for name in page.authors:
        parts = name.split()
        authors.append(
            AuthorRecord(
                name=name,
                family=parts[-1] if len(parts) > 1 else None,
                given=" ".join(parts[:-1]) if len(parts) > 1 else None,
            )
        )
    return SourceRecord(
        source="page",
        title=page.title,
        abstract=page.abstract,
        year=page.year,
        kind=None,
        venue=page.venue_hint,
        doi=page.doi,
        arxiv=page.arxiv,
        url=page.url,
        authors=authors,
        raw={"pdf_urls": page.pdf_urls, "venue_hint": page.venue_hint, "page_url": page.url},
    )


async def _collect(
    ref: InputRef,
    client: httpx.AsyncClient,
    settings: Settings,
) -> tuple[list[SourceRecord], str]:
    """Gather SourceRecords for an input. Returns records plus the effective
    input kind used for field-priority ordering (URL inputs delegate to the
    identifier/title branch and inherit its ordering)."""
    if ref.kind == "url":
        page = await fetch_page(client, ref.value, settings)
        if page is None:
            return [], "url"
        sub: InputRef
        if page.doi:
            sub = InputRef(raw=ref.raw, kind="doi", value=page.doi)
        elif page.arxiv:
            sub = InputRef(raw=ref.raw, kind="arxiv", value=page.arxiv)
        elif page.title:
            sub = InputRef(raw=ref.raw, kind="title", value=page.title)
        else:
            page_record = _page_record(page)
            return ([page_record] if page_record else []), "url"
        records, effective = await _collect(sub, client, settings)
        page_record = _page_record(page)
        if page_record is not None:
            records.append(page_record)
        return records, effective

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
        # Search engines rank by relevance but still answer *any* query with
        # *some* paper; drop results whose title does not plausibly match.
        found = [record for record in found if _title_matches(ref.value, record.title)]
        return found, "title"
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
        return [], ref.kind

    # DBLP is the best venue/author-identity source. Enrich DOI/arXiv/ACL/
    # OpenReview lookups with a title search after the primary record is known.
    title = _pick(found, "title", ["crossref", "arxiv", "dblp", "openalex", "s2"])
    if title and ref.kind in {"doi", "arxiv", "acl", "openreview"}:
        dblp = await search_dblp(client, title, settings)
        for record in dblp:
            if record.title and _similar(record.title, title):
                found.append(record)
    return found, ref.kind


def _candidate(records: list[SourceRecord], input_kind: str, raw: str) -> Candidate | None:
    if not records:
        return None
    order = _SOURCE_ORDER.get(input_kind, _SOURCE_ORDER["title"])
    title = _pick(records, "title", order)
    if not title:
        return None
    kind = _pick(records, "kind", order)
    kind_from_source = kind is not None
    arxiv = _pick(records, "arxiv", order)
    if kind is None:
        kind = "preprint" if arxiv else "journal"
    year = _pick(records, "year", order)
    if not isinstance(year, int):
        year = date.today().year
    authors = _merge_authors(records, order)
    identifiers = _identifiers(records)
    venue_name = _pick(
        records, "venue", ["dblp", "crossref", "acl", "openreview", "openalex", "s2", "page"]
    )
    if venue_name and normalize_name(venue_name) in _PREPRINT_VENUE_KEYS:
        # A preprint-server venue is a property of the *record*, not the paper.
        # Prefer any source that knows the real venue (DBLP > Crossref > ... >
        # the landing page's own hint); only then fall back to "arXiv".
        for source in order:
            record = next((item for item in records if item.source == source), None)
            if record is None or not record.venue:
                continue
            if normalize_name(record.venue) not in _PREPRINT_VENUE_KEYS:
                venue_name = record.venue
                break
    dblp_venue_key = _pick(records, "dblp_venue_key", order)
    edition_key = _pick(records, "dblp_edition_key", order)
    page_record = next((record for record in records if record.source == "page"), None)
    pdf_urls = list((page_record.raw or {}).get("pdf_urls") or []) if page_record else []
    if not pdf_urls:
        # A URL that is just an arXiv/ACL identifier has no landing-page record,
        # so derive the open-access PDF from the identifier instead of silently
        # importing metadata only.
        arxiv_id = next(
            (str(item["value"]) for item in identifiers if item["scheme"] == "arxiv"), None
        )
        acl_id = next((str(item["value"]) for item in identifiers if item["scheme"] == "acl"), None)
        if arxiv_id:
            pdf_urls.append(f"https://arxiv.org/pdf/{arxiv_id}")
        elif acl_id:
            pdf_urls.append(f"https://aclanthology.org/{acl_id}.pdf")
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
        pdf_urls=pdf_urls,
        _venue_name=venue_name,
        _dblp_venue_key=dblp_venue_key,
        _edition_key=edition_key,
        _kind_from_source=kind_from_source,
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
        records, effective_kind = await _collect(ref, client, settings)
    finally:
        if owns_client:
            await client.aclose()
    candidate = _candidate(records, effective_kind, raw)
    if candidate is None:
        if ref.kind == "url":
            warnings.append("landing page unreachable or no title found")
        else:
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
        elif not candidate._kind_from_source and candidate.venue.kind in {"conference", "journal"}:
            # kind was a blind default ("journal"); a matched in-library venue
            # states the real kind. Without this, journal-kind + conference
            # venue would later collide with the edition validation.
            candidate.kind = candidate.venue.kind
    elif candidate.kind in {"conference", "journal"}:
        warnings.append("venue missing from all sources")
    if candidate.abstract is None:
        warnings.append("abstract missing")
    if len(candidate.authors) == 0:
        warnings.append("authors missing")

    candidate.duplicate_of = await _duplicate(session, candidate.identifiers)
    if candidate.duplicate_of:
        warnings.append("already in library")
    elif (suggested := await fuzzy_library_match(session, candidate)) is not None:
        candidate.suggested_duplicate_of = suggested
        warnings.append(
            f"possible duplicate in library: {suggested['citekey']} — {suggested['title']}"
        )
    candidate.confidence = min(
        0.55
        + 0.15 * (len(candidate.sources) - 1)
        + (0.1 if candidate.venue and candidate.venue.venue_id else 0.0)
        + (0.05 if any(author.orcid or author.dblp_pid for author in candidate.authors) else 0.0),
        0.99,
    )
    candidate.warnings = warnings
    return candidate, warnings
