from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(slots=True)
class AuthorRecord:
    name: str
    family: str | None = None
    given: str | None = None
    orcid: str | None = None
    dblp_pid: str | None = None
    s2_author_id: str | None = None
    openalex_id: str | None = None
    affiliation: str | None = None


@dataclass(slots=True)
class SourceRecord:
    source: str
    title: str | None = None
    abstract: str | None = None
    year: int | None = None
    publication_date: date | None = None
    kind: str | None = None
    venue: str | None = None
    dblp_venue_key: str | None = None
    dblp_edition_key: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    arxiv: str | None = None
    dblp_key: str | None = None
    openalex_id: str | None = None
    s2_id: str | None = None
    acl_id: str | None = None
    openreview_id: str | None = None
    url: str | None = None
    authors: list[AuthorRecord] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InputRef:
    raw: str
    kind: str  # doi | arxiv | acl | openreview | dblp | url | title
    value: str


@dataclass(slots=True)
class VenueSuggestion:
    venue_id: str | None
    full_name: str
    abbrev: str | None
    kind: str
    year: int | None
    track: str = "main"
    edition_dblp_key: str | None = None
    confidence: float = 0.5
    evidence: str = "unmatched"


@dataclass(slots=True)
class Candidate:
    confidence: float
    sources: list[str]
    kind: str
    title: str
    abstract: str | None
    year: int
    publication_date: date | None
    authors: list[AuthorRecord]
    identifiers: list[dict[str, str | bool]]
    venue: VenueSuggestion | None
    evaluation: dict[str, Any]
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    duplicate_of: dict[str, str] | None = None
    warnings: list[str] = field(default_factory=list)
    citekey: str | None = None
    # Open-access PDF links discovered on the landing page (or elsewhere),
    # ordered by preference. Empty when none were found.
    pdf_urls: list[str] = field(default_factory=list)
    # Internal fields used while building/importing the candidate.
    _venue_name: str | None = None
    _dblp_venue_key: str | None = None
    _edition_key: str | None = None
    # False when `kind` came from the blind default ("preprint"/"journal")
    # rather than an explicit source statement; the matched venue may then
    # override it (see resolve_input).
    _kind_from_source: bool = False
