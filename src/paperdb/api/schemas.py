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
    tags: list[str] = Field(default_factory=list)
    collections: list[uuid.UUID] = Field(default_factory=list)
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
    orcid: str | None = None
    dblp_pid: str | None = None
    s2_author_id: str | None = None
    openalex_id: str | None = None

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


class VenueCreate(ApiModel):
    kind: str
    full_name: str
    abbrev: str | None = None
    parent_id: uuid.UUID | None = None
    dblp_key: str | None = None
    issn_print: str | None = None
    issn_online: str | None = None
    homepage_url: str | None = None
    ccf_field: str | None = None
    ccf_field_key: str | None = None


class VenueUpdate(ApiModel):
    full_name: str | None = None
    abbrev: str | None = None
    parent_id: uuid.UUID | None = None
    dblp_key: str | None = None
    issn_print: str | None = None
    issn_online: str | None = None
    homepage_url: str | None = None
    ccf_field: str | None = None
    ccf_field_key: str | None = None


class AuthorCreate(ApiModel):
    display_name: str
    family_name: str | None = None
    given_name: str | None = None
    orcid: str | None = None
    dblp_pid: str | None = None
    s2_author_id: str | None = None
    openalex_id: str | None = None
    homepage_url: str | None = None
    is_me: bool = False


class AuthorUpdate(ApiModel):
    display_name: str | None = None
    family_name: str | None = None
    given_name: str | None = None
    orcid: str | None = None
    dblp_pid: str | None = None
    s2_author_id: str | None = None
    openalex_id: str | None = None
    homepage_url: str | None = None
    is_me: bool | None = None


class ResolveRequest(ApiModel):
    input: str
    kind: str = "auto"


class CandidateAuthorOut(ApiModel):
    display_name: str
    family_name: str | None = None
    given_name: str | None = None
    orcid: str | None = None
    dblp_pid: str | None = None
    s2_author_id: str | None = None
    openalex_id: str | None = None
    affiliation: str | None = None


class VenueSuggestionOut(ApiModel):
    venue_id: str | None = None
    full_name: str
    abbrev: str | None = None
    kind: str
    year: int | None = None
    track: str = "main"
    confidence: float = 0.5
    evidence: str = "unmatched"


class ResolveCandidateOut(ApiModel):
    confidence: float
    sources: list[str]
    kind: str
    title: str
    abstract: str | None = None
    year: int
    publication_date: date | None = None
    authors: list[CandidateAuthorOut] = Field(default_factory=list)
    identifiers: list[IdentifierInput] = Field(default_factory=list)
    venue: VenueSuggestionOut | None = None
    evaluation: Evaluation
    duplicate_of: dict[str, str] | None = None
    warnings: list[str] = Field(default_factory=list)


class ResolveResponse(ApiModel):
    input: dict[str, str]
    candidates: list[ResolveCandidateOut]
    warnings: list[str] = Field(default_factory=list)


class ImportOptions(ApiModel):
    on_duplicate: Literal["skip", "link", "create"] = "skip"
    default_tags: list[str] = Field(default_factory=list)
    default_collection_id: uuid.UUID | None = None


class ImportIdentifiersRequest(ApiModel):
    items: list[str] = Field(min_length=1, max_length=500)
    options: ImportOptions = Field(default_factory=ImportOptions)


class ImportCreated(ApiModel):
    import_id: uuid.UUID
    job_id: uuid.UUID
    status: str
    items: int


class ImportItemOut(ApiModel):
    id: uuid.UUID
    ordinal: int
    raw_input: str
    status: str
    paper_id: uuid.UUID | None = None
    error: str | None = None
    attempts: int = 0


class ImportJobOut(ApiModel):
    id: uuid.UUID
    kind: str
    status: str
    stats: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_summary: str | None = None


class ImportPage(ApiModel):
    items: list[ImportJobOut]
    next_cursor: str | None = None
    has_more: bool = False


class JobOut(ApiModel):
    id: uuid.UUID
    kind: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class DuplicateOut(ApiModel):
    paper: PaperSummary
    score: float
    reasons: list[str] = Field(default_factory=list)


class MergeRequest(ApiModel):
    into: uuid.UUID
    fields: dict[str, str] | None = None
    delete_source: bool = True


class EnrichCreated(ApiModel):
    job_id: uuid.UUID
    paper_id: uuid.UUID


class CollectionOut(ApiModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None = None
    name: str
    slug: str | None = None
    description: str | None = None
    position: int = 0
    paper_count: int = 0
    total_count: int = 0
    children: list[CollectionOut] = Field(default_factory=list)


class CollectionCreate(ApiModel):
    parent_id: uuid.UUID | None = None
    name: str
    slug: str | None = None
    description: str | None = None


class CollectionUpdate(ApiModel):
    name: str | None = None
    slug: str | None = None
    description: str | None = None


class CollectionMove(ApiModel):
    parent_id: uuid.UUID | None = None
    position: int = 0


class CollectionPapers(ApiModel):
    paper_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    position: int | None = None


class TagOut(ApiModel):
    id: uuid.UUID
    path: str
    namespace: str
    name: str
    parent_id: uuid.UUID | None = None
    color: str | None = None
    description: str | None = None
    paper_count: int = 0
    children: list[TagOut] = Field(default_factory=list)


class TagCreate(ApiModel):
    path: str
    color: str | None = None
    description: str | None = None


class TagUpdate(ApiModel):
    name: str | None = None
    color: str | None = None
    description: str | None = None


class TagMerge(ApiModel):
    into_tag_id: uuid.UUID


CollectionOut.model_rebuild()
TagOut.model_rebuild()


class PaperTagsInput(ApiModel):
    paths: list[str] = Field(min_length=1, max_length=100)


class PaperCollectionsInput(ApiModel):
    collection_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class SavedViewCreate(ApiModel):
    name: str
    description: str | None = None
    filter: dict = Field(default_factory=dict)
    sort: list[dict] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    view_mode: str = "table"
    is_pinned: bool = False
    position: int = 0


class SavedViewUpdate(ApiModel):
    name: str | None = None
    description: str | None = None
    filter: dict | None = None
    sort: list[dict] | None = None
    columns: list[str] | None = None
    view_mode: str | None = None
    is_pinned: bool | None = None
    position: int | None = None


class SavedViewOut(ApiModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    filter: dict
    sort: list[dict]
    columns: list[str]
    view_mode: str
    is_pinned: bool
    position: int
    count: int = 0
    created_at: datetime
    updated_at: datetime


class ViewCountOut(ApiModel):
    count: int


class FilterValidateRequest(ApiModel):
    filter: dict = Field(default_factory=dict)


class FilterValidateOut(ApiModel):
    valid: bool
    rules: int


class ProjectCreate(ApiModel):
    name: str
    description: str | None = None
    citekey_template: str | None = None
    export_include_abstract: bool = False


class ProjectUpdate(ApiModel):
    name: str | None = None
    description: str | None = None
    citekey_template: str | None = None
    export_include_abstract: bool | None = None


class ProjectOut(ApiModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    citekey_template: str | None = None
    export_include_abstract: bool = False
    collection_ids: list[uuid.UUID] = Field(default_factory=list)
    paper_ids: list[uuid.UUID] = Field(default_factory=list)
    paper_count: int = 0
    created_at: datetime
    updated_at: datetime


class ProjectCollectionsInput(ApiModel):
    collection_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class ProjectPapersInput(ApiModel):
    paper_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class AutoRuleCreate(ApiModel):
    name: str
    filter: dict = Field(default_factory=dict)
    actions: list[dict] = Field(default_factory=list)
    run_on: list[str] = Field(default_factory=lambda: ["manual"])
    enabled: bool = True


class AutoRuleUpdate(ApiModel):
    name: str | None = None
    filter: dict | None = None
    actions: list[dict] | None = None
    run_on: list[str] | None = None
    enabled: bool | None = None


class AutoRuleOut(ApiModel):
    id: uuid.UUID
    name: str
    filter: dict
    actions: list[dict]
    run_on: list[str]
    enabled: bool
    last_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class BulkPatch(ApiModel):
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    add_collections: list[uuid.UUID] = Field(default_factory=list)
    remove_collections: list[uuid.UUID] = Field(default_factory=list)
    state: StateInput | None = None


class PaperBulkRequest(ApiModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    patch: BulkPatch


class PaperBulkResult(ApiModel):
    updated: int
    failed: list[dict[str, str]] = Field(default_factory=list)


class PdfFileOut(ApiModel):
    id: uuid.UUID
    paper_id: uuid.UUID
    is_primary: bool
    byte_size: int
    page_count: int | None = None
    mime: str
    original_filename: str | None = None
    text_status: str
    text_language: str | None = None
    created_at: datetime


class PdfUploadOut(ApiModel):
    file: PdfFileOut
    job_id: uuid.UUID | None = None


class AnnotationCreate(ApiModel):
    pdf_file_id: uuid.UUID
    kind: str = "highlight"
    page: int = Field(ge=1)
    rects: list[dict[str, float]] = Field(min_length=1, max_length=64)
    quote: str = ""
    prefix: str | None = None
    suffix: str | None = None
    comment: str | None = None
    color: str = "yellow"


class AnnotationUpdate(ApiModel):
    comment: str | None = None
    color: str | None = None
    rects: list[dict[str, float]] | None = None


class AnnotationOut(ApiModel):
    id: uuid.UUID
    paper_id: uuid.UUID
    pdf_file_id: uuid.UUID
    kind: str
    page: int
    rects: list[dict[str, float]]
    quote: str
    prefix: str | None = None
    suffix: str | None = None
    comment: str | None = None
    color: str
    anchor_status: str
    created_at: datetime
    updated_at: datetime


class SearchHitOut(ApiModel):
    paper_id: uuid.UUID
    paper_title: str
    file_id: uuid.UUID
    page: int
    snippet: str


class FulltextSearchOut(ApiModel):
    query: str
    hits: list[SearchHitOut]
    has_more: bool = False


class CitationPaperOut(ApiModel):
    id: uuid.UUID
    title: str
    year: int
    citekey: str


class ExternalWorkOut(ApiModel):
    id: uuid.UUID
    title: str
    year: int | None = None
    doi: str | None = None
    openalex_id: str | None = None
    cited_by_count: int | None = None
    resolved_paper_id: uuid.UUID | None = None


class CitationItemOut(ApiModel):
    in_library: bool
    paper: CitationPaperOut | None = None
    external: ExternalWorkOut | None = None
    source: str


class CitationPage(ApiModel):
    items: list[CitationItemOut]
    has_more: bool = False


class CitationSyncRequest(ApiModel):
    paper_ids: list[uuid.UUID] | None = None


class CitationSyncCreated(ApiModel):
    job_id: uuid.UUID
    papers: int


class PdfImportCreated(ApiModel):
    paper_id: uuid.UUID
    file_id: uuid.UUID
    job_id: uuid.UUID | None = None
    created: bool
    title: str
    warnings: list[str] = Field(default_factory=list)
