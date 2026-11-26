from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from paperdb.db.base import Base, Timestamped, UUIDPrimaryKey


class VenueKind(StrEnum):
    conference = "conference"
    journal = "journal"


class VenueStatus(StrEnum):
    provisional = "provisional"
    confirmed = "confirmed"
    merged = "merged"


class EditionTrack(StrEnum):
    main = "main"
    findings = "findings"
    demo = "demo"
    poster = "poster"
    doctoral = "doctoral"
    journal_track = "journal_track"
    companion = "companion"
    other = "other"


class PaperKind(StrEnum):
    conference = "conference"
    journal = "journal"
    preprint = "preprint"
    thesis = "thesis"
    book_chapter = "book_chapter"
    tech_report = "tech_report"


class IdentifierScheme(StrEnum):
    doi = "doi"
    arxiv = "arxiv"
    dblp = "dblp"
    s2 = "s2"
    openalex = "openalex"
    acl = "acl"
    openreview = "openreview"
    pmid = "pmid"
    isbn = "isbn"
    other = "other"


class RankScheme(StrEnum):
    ccf = "ccf"
    cas = "cas"


class RankFieldKind(StrEnum):
    ccf_field = "ccf_field"
    cas_major = "cas_major"
    cas_minor = "cas_minor"


class ReadingStatus(StrEnum):
    unread = "unread"
    queued = "queued"
    reading = "reading"
    read = "read"
    skimmed = "skimmed"


class AuthorStatus(StrEnum):
    provisional = "provisional"
    confirmed = "confirmed"
    merged = "merged"


class JobStatus(StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    dead = "dead"


class PdfTextStatus(StrEnum):
    pending = "pending"
    extracted = "extracted"
    ocr_needed = "ocr_needed"
    failed = "failed"


class AnnotationKind(StrEnum):
    highlight = "highlight"
    underline = "underline"
    note = "note"
    region = "region"


class ImportKind(StrEnum):
    identifiers = "identifiers"
    bibtex = "bibtex"
    pdf = "pdf"


class ImportItemStatus(StrEnum):
    pending = "pending"
    created = "created"
    duplicate = "duplicate"
    failed = "failed"
    skipped = "skipped"


def _enum(enum_cls: type[StrEnum], name: str, length: int = 32) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


class DatasetVersion(Base):
    __tablename__ = "dataset_version"

    scheme: Mapped[RankScheme] = mapped_column(
        _enum(RankScheme, "rank_scheme", 8), primary_key=True
    )
    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[date | None] = mapped_column(Date)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    checksum: Mapped[str] = mapped_column(String(80), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    valid_from: Mapped[int | None] = mapped_column(SmallInteger)
    valid_to: Mapped[int | None] = mapped_column(SmallInteger)
    notes: Mapped[str | None] = mapped_column(Text)


class Venue(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "venue"
    __table_args__ = (
        Index(
            "uq_venue_kind_normalized",
            "kind",
            "normalized_name",
            unique=True,
            postgresql_where=text("merged_into_id IS NULL"),
        ),
        Index(
            "uq_venue_dblp_key",
            "dblp_key",
            unique=True,
            postgresql_where=text("dblp_key IS NOT NULL"),
        ),
        Index("ix_venue_ccf_field_key", "ccf_field_key"),
        Index("ix_venue_issn_print", "issn_print"),
        Index("ix_venue_issn_online", "issn_online"),
    )

    kind: Mapped[VenueKind] = mapped_column(_enum(VenueKind, "venue_kind", 16), nullable=False)
    abbrev: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("venue.id"))
    dblp_key: Mapped[str | None] = mapped_column(String(255))
    issn_print: Mapped[str | None] = mapped_column(String(16))
    issn_online: Mapped[str | None] = mapped_column(String(16))
    homepage_url: Mapped[str | None] = mapped_column(Text)
    ccf_field: Mapped[str | None] = mapped_column(String(255))
    ccf_field_key: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[VenueStatus] = mapped_column(
        _enum(VenueStatus, "venue_status", 16),
        default=VenueStatus.provisional,
        nullable=False,
    )
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("venue.id"))

    parent: Mapped[Venue | None] = relationship(
        "Venue",
        remote_side="Venue.id",
        foreign_keys=[parent_id],
        back_populates="children",
    )
    children: Mapped[list[Venue]] = relationship(
        "Venue",
        foreign_keys=[parent_id],
        back_populates="parent",
    )
    aliases: Mapped[list[VenueAlias]] = relationship(
        back_populates="venue", cascade="all, delete-orphan"
    )
    identifiers: Mapped[list[VenueIdentifier]] = relationship(
        back_populates="venue", cascade="all, delete-orphan"
    )
    editions: Mapped[list[VenueEdition]] = relationship(
        back_populates="venue", cascade="all, delete-orphan"
    )
    ranks: Mapped[list[VenueRank]] = relationship(
        back_populates="venue", cascade="all, delete-orphan"
    )


class VenueAlias(UUIDPrimaryKey, Base):
    __tablename__ = "venue_alias"
    __table_args__ = (UniqueConstraint("venue_id", "normalized_alias", name="uq_venue_alias"),)

    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venue.id"), nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    venue: Mapped[Venue] = relationship(back_populates="aliases")


class VenueIdentifier(UUIDPrimaryKey, Base):
    __tablename__ = "venue_identifier"
    __table_args__ = (
        UniqueConstraint("source", "normalized", name="uq_venue_identifier_source_normalized"),
    )

    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venue.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[str] = mapped_column(String(255), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    venue: Mapped[Venue] = relationship(back_populates="identifiers")


class VenueEdition(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "venue_edition"
    __table_args__ = (
        UniqueConstraint("venue_id", "year", "track", name="uq_venue_edition_year_track"),
        Index(
            "uq_venue_edition_dblp_key",
            "dblp_key",
            unique=True,
            postgresql_where=text("dblp_key IS NOT NULL"),
        ),
    )

    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venue.id"), nullable=False)
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    ordinal: Mapped[int | None] = mapped_column(SmallInteger)
    track: Mapped[EditionTrack] = mapped_column(
        _enum(EditionTrack, "edition_track", 16), default=EditionTrack.main, nullable=False
    )
    dblp_key: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    website_url: Mapped[str | None] = mapped_column(Text)
    proceedings_url: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    venue: Mapped[Venue] = relationship(back_populates="editions")


class VenueRank(UUIDPrimaryKey, Base):
    __tablename__ = "venue_rank"
    __table_args__ = (
        ForeignKeyConstraint(
            ["scheme", "dataset_version"],
            ["dataset_version.scheme", "dataset_version.version"],
            name="fk_venue_rank_dataset",
        ),
        UniqueConstraint(
            "venue_id",
            "scheme",
            "dataset_version",
            "field_kind",
            "field_name",
            name="uq_venue_rank_field",
        ),
        Index(
            "uq_venue_rank_primary",
            "venue_id",
            "scheme",
            "dataset_version",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        Index("ix_venue_rank_scheme_level", "scheme", "level"),
        Index("ix_venue_rank_venue", "venue_id"),
        Index("ix_venue_rank_field_key", "field_key"),
    )

    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venue.id"), nullable=False)
    scheme: Mapped[RankScheme] = mapped_column(_enum(RankScheme, "rank_scheme", 8), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(16), nullable=False)
    field_kind: Mapped[RankFieldKind] = mapped_column(
        _enum(RankFieldKind, "rank_field_kind", 16), nullable=False
    )
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    field_name_en: Mapped[str | None] = mapped_column(Text)
    field_key: Mapped[str | None] = mapped_column(String(128))
    level: Mapped[str] = mapped_column(String(4), nullable=False)
    is_top: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rank_in_field: Mapped[int | None] = mapped_column(Integer)
    total_in_field: Mapped[int | None] = mapped_column(Integer)
    valid_from: Mapped[int | None] = mapped_column(SmallInteger)
    valid_to: Mapped[int | None] = mapped_column(SmallInteger)
    source_url: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    venue: Mapped[Venue] = relationship(back_populates="ranks")
    dataset: Mapped[DatasetVersion] = relationship()


class Paper(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "paper"
    __table_args__ = (
        Index(
            "uq_paper_citekey",
            "citekey",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint("citekey ~ '^[a-z][a-z0-9]{0,63}$'", name="citekey_format"),
        CheckConstraint(
            "kind <> 'journal' OR (venue_id IS NOT NULL AND venue_edition_id IS NULL)",
            name="journal_venue",
        ),
        CheckConstraint("kind <> 'conference' OR venue_id IS NOT NULL", name="conference_venue"),
        Index("ix_paper_year", "year"),
        Index("ix_paper_venue_year", "venue_id", "year"),
        Index("ix_paper_edition", "venue_edition_id"),
        Index("ix_paper_created_at_id", "created_at", "id"),
        Index("ix_paper_staged", "staged", postgresql_where=text("staged")),
    )

    kind: Mapped[PaperKind] = mapped_column(_enum(PaperKind, "paper_kind", 16), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_normalized: Mapped[str] = mapped_column(String(1024), nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    publication_date: Mapped[date | None] = mapped_column(Date)
    venue_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("venue.id"))
    venue_edition_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("venue_edition.id"))
    volume: Mapped[str | None] = mapped_column(String(64))
    issue: Mapped[str | None] = mapped_column(String(64))
    pages: Mapped[str | None] = mapped_column(String(64))
    institution: Mapped[str | None] = mapped_column(Text)
    citekey: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    author_keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    paper_author_names: Mapped[str] = mapped_column(Text, default="", nullable=False)
    urls: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    added_via: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    user_locked_fields: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    citations_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # True while the paper lives only in a research workspace: it is not part
    # of the library yet and is hidden from the default listing/search.
    staged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    venue: Mapped[Venue | None] = relationship(foreign_keys=[venue_id])
    venue_edition: Mapped[VenueEdition | None] = relationship(foreign_keys=[venue_edition_id])
    authors: Mapped[list[PaperAuthor]] = relationship(
        back_populates="paper",
        cascade="all, delete-orphan",
        order_by="PaperAuthor.position",
    )
    identifiers: Mapped[list[PaperIdentifier]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )
    state: Mapped[PaperState | None] = relationship(
        back_populates="paper", cascade="all, delete-orphan", uselist=False
    )
    tags: Mapped[list[Tag]] = relationship("Tag", secondary="paper_tag", viewonly=True)
    collections: Mapped[list[Collection]] = relationship(
        "Collection", secondary="collection_paper", viewonly=True
    )


class PaperIdentifier(UUIDPrimaryKey, Base):
    __tablename__ = "paper_identifier"
    __table_args__ = (
        UniqueConstraint("scheme", "normalized", name="uq_paper_identifier_scheme_normalized"),
    )

    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), nullable=False)
    scheme: Mapped[IdentifierScheme] = mapped_column(
        _enum(IdentifierScheme, "identifier_scheme", 16), nullable=False
    )
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized: Mapped[str] = mapped_column(String(512), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    paper: Mapped[Paper] = relationship(back_populates="identifiers")


class Author(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "author"
    __table_args__ = (
        Index(
            "uq_author_orcid",
            "orcid",
            unique=True,
            postgresql_where=text("orcid IS NOT NULL"),
        ),
        Index(
            "uq_author_dblp_pid",
            "dblp_pid",
            unique=True,
            postgresql_where=text("dblp_pid IS NOT NULL"),
        ),
        Index("uq_author_is_me", "is_me", unique=True, postgresql_where=text("is_me")),
        Index("ix_author_display_name", "display_name"),
    )

    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    family_name: Mapped[str | None] = mapped_column(String(128))
    given_name: Mapped[str | None] = mapped_column(String(128))
    orcid: Mapped[str | None] = mapped_column(String(19))
    dblp_pid: Mapped[str | None] = mapped_column(String(255))
    s2_author_id: Mapped[str | None] = mapped_column(String(64))
    openalex_id: Mapped[str | None] = mapped_column(String(64))
    homepage_url: Mapped[str | None] = mapped_column(Text)
    is_me: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[AuthorStatus] = mapped_column(
        _enum(AuthorStatus, "author_status", 16),
        default=AuthorStatus.provisional,
        nullable=False,
    )
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("author.id"))

    aliases: Mapped[list[AuthorAlias]] = relationship(
        back_populates="author", cascade="all, delete-orphan"
    )
    paper_authors: Mapped[list[PaperAuthor]] = relationship(back_populates="author")


class AuthorAlias(UUIDPrimaryKey, Base):
    __tablename__ = "author_alias"
    __table_args__ = (UniqueConstraint("author_id", "normalized_name", name="uq_author_alias"),)

    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("author.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)

    author: Mapped[Author] = relationship(back_populates="aliases")


class PaperAuthor(Base):
    __tablename__ = "paper_author"
    __table_args__ = (
        UniqueConstraint("paper_id", "author_id", name="uq_paper_author_author"),
        Index("ix_paper_author_author", "author_id", "paper_id"),
    )

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper.id"), primary_key=True, nullable=False
    )
    position: Mapped[int] = mapped_column(SmallInteger, primary_key=True, nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("author.id"), nullable=False)
    is_corresponding: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    equal_contribution: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    affiliation_text: Mapped[str | None] = mapped_column(Text)
    raw_name: Mapped[str | None] = mapped_column(Text)

    paper: Mapped[Paper] = relationship(back_populates="authors")
    author: Mapped[Author] = relationship(back_populates="paper_authors")


class PaperState(Base):
    __tablename__ = "paper_state"

    paper_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper.id"), primary_key=True, nullable=False
    )
    status: Mapped[ReadingStatus] = mapped_column(
        _enum(ReadingStatus, "reading_status", 16),
        default=ReadingStatus.unread,
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    progress_page: Mapped[int | None] = mapped_column(Integer)
    starred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    paper: Mapped[Paper] = relationship(back_populates="state")


class ApiToken(UUIDPrimaryKey, Base):
    __tablename__ = "api_token"
    __table_args__ = (UniqueConstraint("prefix", name="uq_api_token_prefix"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExportToken(UUIDPrimaryKey, Base):
    __tablename__ = "export_token"
    __table_args__ = (UniqueConstraint("prefix", name="uq_export_token_prefix"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    citekey_template: Mapped[str | None] = mapped_column(String(255))
    include_abstract: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(UUIDPrimaryKey, Base):
    __tablename__ = "job"
    __table_args__ = (
        Index(
            "ix_job_claim",
            "status",
            "run_at",
            postgresql_where=text("status IN ('pending','failed')"),
        ),
        Index("ix_job_kind_status", "kind", "status"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status", 16), default=JobStatus.pending, nullable=False
    )
    priority: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, default=5, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportJob(UUIDPrimaryKey, Base):
    __tablename__ = "import_job"

    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("job.id"))
    kind: Mapped[ImportKind] = mapped_column(_enum(ImportKind, "import_kind", 16), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status", 16), default=JobStatus.pending, nullable=False
    )
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_by_token_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[ImportItem]] = relationship(
        back_populates="import_job", cascade="all, delete-orphan"
    )


class ImportItem(UUIDPrimaryKey, Base):
    __tablename__ = "import_item"
    __table_args__ = (
        UniqueConstraint("job_id", "ordinal", name="uq_import_item_ordinal"),
        Index("ix_import_item_job_status", "job_id", "status"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_job.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_input: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ImportItemStatus] = mapped_column(
        _enum(ImportItemStatus, "import_item_status", 16),
        default=ImportItemStatus.pending,
        nullable=False,
    )
    paper_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paper.id"))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)

    import_job: Mapped[ImportJob] = relationship(back_populates="items")


class MergeRedirect(Base):
    __tablename__ = "merge_redirect"

    entity_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    old_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    new_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Collection(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "collection"
    __table_args__ = (
        Index("ix_collection_parent_name", "parent_id", "name"),
        Index("ix_collection_parent_position", "parent_id", "position"),
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("collection.id"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    papers: Mapped[list[CollectionPaper]] = relationship(
        back_populates="collection", cascade="all, delete-orphan"
    )


class CollectionPaper(Base):
    __tablename__ = "collection_paper"
    __table_args__ = (
        Index("ix_collection_paper_paper", "paper_id"),
        Index("ix_collection_paper_position", "collection_id", "position"),
    )

    collection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collection.id"), primary_key=True)
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), primary_key=True)
    position: Mapped[int | None] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    collection: Mapped[Collection] = relationship(back_populates="papers")


class Tag(UUIDPrimaryKey, Base):
    __tablename__ = "tag"
    __table_args__ = (
        UniqueConstraint("path", name="uq_tag_path"),
        Index("ix_tag_namespace", "namespace"),
        Index("ix_tag_parent", "parent_id"),
    )

    path: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tag.id"))
    color: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    papers: Mapped[list[PaperTag]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )


class PaperTag(Base):
    __tablename__ = "paper_tag"
    __table_args__ = (Index("ix_paper_tag_tag", "tag_id"),)

    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), primary_key=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tag.id"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tag: Mapped[Tag] = relationship(back_populates="papers")


class SavedView(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "saved_view"
    __table_args__ = (UniqueConstraint("name", name="uq_saved_view_name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    filter: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    sort: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    columns: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    view_mode: Mapped[str] = mapped_column(String(16), default="table", nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AutoRule(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "auto_rule"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    filter: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    run_on: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("name", name="uq_project_name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    citekey_template: Mapped[str | None] = mapped_column(String(255))
    export_include_abstract: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    collections: Mapped[list[ProjectCollection]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    papers: Mapped[list[ProjectPaper]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectCollection(Base):
    __tablename__ = "project_collection"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"), primary_key=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collection.id"), primary_key=True)

    project: Mapped[Project] = relationship(back_populates="collections")


class ProjectPaper(Base):
    __tablename__ = "project_paper"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"), primary_key=True)
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="papers")


class ResearchWorkspace(UUIDPrimaryKey, Timestamped, Base):
    """Staging area for batch research whose results may not be kept.

    Peer of projects and collections, but its papers are *staged*: hidden from
    the library until promoted. Closing a workspace keeps the record.
    """

    __tablename__ = "research_workspace"
    __table_args__ = (
        Index(
            "uq_research_workspace_name_active",
            "name",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    papers: Mapped[list[ResearchWorkspacePaper]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class ResearchWorkspacePaper(Base):
    __tablename__ = "research_workspace_paper"
    __table_args__ = (
        Index("ix_research_workspace_paper_paper", "paper_id"),
        Index("ix_research_workspace_paper_position", "workspace_id", "position"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_workspace.id"), primary_key=True
    )
    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), primary_key=True)
    position: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped[ResearchWorkspace] = relationship(back_populates="papers")


class PdfFile(UUIDPrimaryKey, Base):
    __tablename__ = "pdf_file"
    __table_args__ = (
        Index("ix_pdf_file_paper", "paper_id"),
        Index("ix_pdf_file_sha256", "sha256"),
        Index("ix_pdf_file_storage_key", "storage_backend", "storage_key"),
        Index(
            "uq_pdf_file_primary",
            "paper_id",
            unique=True,
            postgresql_where=text("is_primary AND deleted_at IS NULL"),
        ),
    )

    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    mime: Mapped[str] = mapped_column(String(64), default="application/pdf", nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    source_url: Mapped[str | None] = mapped_column(Text)
    license_note: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    text_status: Mapped[PdfTextStatus] = mapped_column(
        _enum(PdfTextStatus, "pdf_text_status", 16),
        default=PdfTextStatus.pending,
        nullable=False,
    )
    text_language: Mapped[str | None] = mapped_column(String(16))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    paper: Mapped[Paper] = relationship("Paper", foreign_keys=[paper_id])
    pages: Mapped[list[PdfPageText]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class PdfPageText(Base):
    __tablename__ = "pdf_page_text"
    __table_args__ = (
        Index("ix_pdf_page_text_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_pdf_page_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
    )

    pdf_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pdf_file.id"), primary_key=True)
    page: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(text, ''))", persisted=True),
        nullable=False,
    )
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    file: Mapped[PdfFile] = relationship(back_populates="pages")


class Annotation(UUIDPrimaryKey, Base):
    __tablename__ = "annotation"
    __table_args__ = (
        Index("ix_annotation_paper_page", "paper_id", "page"),
        Index("ix_annotation_file", "pdf_file_id"),
    )

    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), nullable=False)
    pdf_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pdf_file.id"), nullable=False)
    kind: Mapped[AnnotationKind] = mapped_column(
        _enum(AnnotationKind, "annotation_kind", 16), nullable=False
    )
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    rects: Mapped[list[dict[str, float]]] = mapped_column(JSONB, default=list, nullable=False)
    quote: Mapped[str] = mapped_column(Text, default="", nullable=False)
    prefix: Mapped[str | None] = mapped_column(Text)
    suffix: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str] = mapped_column(String(16), default="yellow", nullable=False)
    anchor_status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CitationEdge(UUIDPrimaryKey, Base):
    __tablename__ = "citation_edge"
    __table_args__ = (
        UniqueConstraint("citing_paper_id", "cited_paper_id", name="uq_citation_edge"),
        CheckConstraint("citing_paper_id <> cited_paper_id", name="ck_citation_self"),
        Index("ix_citation_edge_citing", "citing_paper_id"),
        Index("ix_citation_edge_cited", "cited_paper_id"),
    )

    citing_paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), nullable=False)
    cited_paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="openalex", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExternalWork(UUIDPrimaryKey, Base):
    __tablename__ = "external_work"
    __table_args__ = (
        Index(
            "uq_external_work_openalex",
            "openalex_id",
            unique=True,
            postgresql_where=text("openalex_id IS NOT NULL"),
        ),
        Index(
            "uq_external_work_doi",
            "doi",
            unique=True,
            postgresql_where=text("doi IS NOT NULL"),
        ),
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int | None] = mapped_column(SmallInteger)
    doi: Mapped[str | None] = mapped_column(String(255))
    openalex_id: Mapped[str | None] = mapped_column(String(64))
    s2_id: Mapped[str | None] = mapped_column(String(64))
    cited_by_count: Mapped[int | None] = mapped_column(Integer)
    resolved_paper_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("paper.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExternalCitation(Base):
    __tablename__ = "external_citation"
    __table_args__ = (Index("ix_external_citation_work", "external_work_id"),)

    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), primary_key=True)
    external_work_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("external_work.id"), primary_key=True
    )
    direction: Mapped[str] = mapped_column(String(16), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), default="openalex", nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CitationStat(Base):
    __tablename__ = "citation_stat"

    paper_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("paper.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, primary_key=True)
    cited_by_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
