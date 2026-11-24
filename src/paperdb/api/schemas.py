from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class VenueEditionOut(ApiModel):
    id: uuid.UUID
    year: int
    track: str
    ordinal: int | None = None


class VenueRef(ApiModel):
    id: uuid.UUID
    kind: str
    abbrev: str | None = None
    full_name: str
    edition: VenueEditionOut | None = None
    ccf_field: str | None = None
    ccf_field_key: str | None = None


class CcfEvaluation(ApiModel):
    level: str
    field: str
    field_key: str | None = None
    dataset_version: str
    fallback: bool = False


class CasMinorField(ApiModel):
    field_name: str
    field_name_en: str | None = None
    level: str
    rank: int | None = None
    total: int | None = None


class CasEvaluation(ApiModel):
    level: str
    is_top: bool
    dataset_version: str
    fallback: bool = False
    major_field: str
    major_field_en: str | None = None
    minor_fields: list[CasMinorField] = Field(default_factory=list)


class Evaluation(ApiModel):
    ccf: CcfEvaluation | None = None
    cas: CasEvaluation | None = None


class AuthorSummary(ApiModel):
    id: uuid.UUID
    display_name: str
    position: int | None = None
    is_me: bool = False


class AuthorDetail(ApiModel):
    id: uuid.UUID
    display_name: str
    family_name: str | None = None
    given_name: str | None = None
    orcid: str | None = None
    dblp_pid: str | None = None
    s2_author_id: str | None = None
    openalex_id: str | None = None
    homepage_url: str | None = None
    is_me: bool = False
    paper_count: int = 0
    aliases: list[str] = Field(default_factory=list)


class PaperAuthorOut(ApiModel):
    id: uuid.UUID
    display_name: str
    family_name: str | None = None
    given_name: str | None = None
    position: int
    is_corresponding: bool = False
    equal_contribution: bool = False
    affiliation: str | None = None
    orcid: str | None = None
    is_me: bool = False


class IdentifierOut(ApiModel):
    id: uuid.UUID
    scheme: str
    value: str
    is_primary: bool = False


class StateOut(ApiModel):
    status: str
    priority: int = 0
    rating: int | None = None
    progress_page: int | None = None
    starred: bool = False
    archived: bool = False


class CountsOut(ApiModel):
    pdf_files: int = 0
    annotations: int = 0
    cited_by: int = 0


class PaperSummary(ApiModel):
    id: uuid.UUID
    kind: str
    title: str
    citekey: str
    year: int
    venue: VenueRef | None = None
    evaluation: Evaluation
    authors: list[PaperAuthorOut] = Field(default_factory=list)
    author_count: int = 0
    state: StateOut
    counts: CountsOut = Field(default_factory=CountsOut)
    created_at: datetime
    updated_at: datetime
    version: int


class PaperDetail(PaperSummary):
    abstract: str | None = None
    publication_date: date | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    institution: str | None = None
    language: str = "en"
    author_keywords: list[str] = Field(default_factory=list)
    user_locked_fields: list[str] = Field(default_factory=list)
    urls: dict[str, Any] = Field(default_factory=dict)
    identifiers: list[IdentifierOut] = Field(default_factory=list)


class PaperAuthorInput(ApiModel):
    author_id: uuid.UUID | None = None
    display_name: str | None = None
    family_name: str | None = None
    given_name: str | None = None
    position: int | None = None
    is_corresponding: bool = False
    equal_contribution: bool = False
    affiliation: str | None = None

    @field_validator("display_name")
    @classmethod
    def _display_or_id(cls, value: str | None, info):
        if value is not None:
            value = value.strip()
        return value


class IdentifierInput(ApiModel):
    scheme: str
    value: str
    is_primary: bool = False

    @field_validator("value")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("empty identifier")
        return value


class StateInput(ApiModel):
    status: Literal["unread", "queued", "reading", "read", "skimmed"] | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    rating: int | None = Field(default=None, ge=1, le=5)
    progress_page: int | None = Field(default=None, ge=0)
    starred: bool | None = None
    archived: bool | None = None


class PaperCreate(ApiModel):
    kind: str
    title: str
    abstract: str | None = None
    year: int = Field(ge=1900, le=2100)
    publication_date: date | None = None
    venue_id: uuid.UUID | None = None
    venue_edition_id: uuid.UUID | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    institution: str | None = None
    citekey: str | None = None
    language: str = "en"
    author_keywords: list[str] = Field(default_factory=list)
    urls: dict[str, str] = Field(default_factory=dict)
    added_via: str = "manual"
    authors: list[PaperAuthorInput] = Field(default_factory=list)
    identifiers: list[IdentifierInput] = Field(default_factory=list)
    state: StateInput | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title is required")
        return value


class PaperUpdate(ApiModel):
    kind: str | None = None
    title: str | None = None
    abstract: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    publication_date: date | None = None
    venue_id: uuid.UUID | None = None
    venue_edition_id: uuid.UUID | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    institution: str | None = None
    citekey: str | None = None
    language: str | None = None
    author_keywords: list[str] | None = None
    urls: dict[str, str] | None = None


class PaperPage(ApiModel):
    items: list[PaperSummary]
    next_cursor: str | None = None
    has_more: bool = False


class VenueRankOut(ApiModel):
    id: uuid.UUID
    scheme: str
    dataset_version: str
    field_kind: str
    field_name: str
    field_name_en: str | None = None
    field_key: str | None = None
    level: str
    is_top: bool = False
    is_primary: bool = False
    rank_in_field: int | None = None
    total_in_field: int | None = None


class VenueAliasOut(ApiModel):
    id: uuid.UUID
    alias: str
    source: str


class VenueIdentifierOut(ApiModel):
    id: uuid.UUID
    source: str
    value: str
    is_primary: bool = False


class VenueSummary(ApiModel):
    id: uuid.UUID
    kind: str
    abbrev: str | None = None
    full_name: str
    ccf_field: str | None = None
    ccf_field_key: str | None = None
    status: str


class VenueDetail(VenueSummary):
    dblp_key: str | None = None
    issn_print: str | None = None
    issn_online: str | None = None
    homepage_url: str | None = None
    aliases: list[VenueAliasOut] = Field(default_factory=list)
    identifiers: list[VenueIdentifierOut] = Field(default_factory=list)
    editions: list[VenueEditionOut] = Field(default_factory=list)
    ranks: list[VenueRankOut] = Field(default_factory=list)
    paper_count: int = 0


class VenuePage(ApiModel):
    items: list[VenueSummary]
    next_cursor: str | None = None
    has_more: bool = False


class AuthorPage(ApiModel):
    items: list[AuthorDetail]
    next_cursor: str | None = None
    has_more: bool = False


class DatasetOut(ApiModel):
    scheme: str
    version: str
    label: str
    source_url: str | None = None
    checksum: str
    imported_at: datetime
    is_current: bool
    valid_from: int | None = None
    valid_to: int | None = None


class StorageOut(ApiModel):
    backend: str
    bytes_used: int
    files: int
    blobs: int


class MetaOut(ApiModel):
    version: str
    datasets: list[DatasetOut]
    counts: dict[str, int]
    venues: dict[str, int]
    storage: StorageOut
    jobs: dict[str, int]


class TokenCreate(ApiModel):
    name: str
    scopes: list[str]
    expires_at: datetime | None = None


class TokenCreated(ApiModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    token: str
    expires_at: datetime | None = None


class TokenOut(ApiModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class ProblemOut(ApiModel):
    type: str
    title: str
    status: int
    detail: str | None = None
    code: str
    request_id: str | None = None


class ExportTokenCreate(ApiModel):
    name: str
    scope_type: str = "library"
    scope_id: uuid.UUID | None = None
    citekey_template: str | None = None
    include_abstract: bool = False


class ExportTokenCreated(ApiModel):
    id: uuid.UUID
    name: str
    prefix: str
    scope_type: str
    scope_id: uuid.UUID | None = None
    url: str
    created_at: datetime


class ExportTokenOut(ApiModel):
    id: uuid.UUID
    name: str
    prefix: str
    scope_type: str
    scope_id: uuid.UUID | None = None
    include_abstract: bool = False
    created_at: datetime
    last_accessed_at: datetime | None = None
    access_count: int = 0
    revoked_at: datetime | None = None
