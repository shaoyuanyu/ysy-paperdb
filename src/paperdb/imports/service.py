"""Import job orchestration (identifiers now; BibTeX/PDF next)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.api.schemas import (
    IdentifierInput,
    ImportIdentifiersRequest,
    ImportItemOut,
    ImportJobOut,
    ImportOptions,
    PaperAuthorInput,
    PaperCreate,
)
from paperdb.config import Settings
from paperdb.db.models import (
    Collection,
    CollectionPaper,
    IdentifierScheme,
    ImportItem,
    ImportItemStatus,
    ImportJob,
    ImportKind,
    JobStatus,
    Paper,
    PaperIdentifier,
    PaperTag,
    ResearchWorkspace,
)
from paperdb.errors import ApiError
from paperdb.ingest.resolve import (
    evaluate_venue,
    find_duplicate,
    fuzzy_library_match,
    resolve_input,
    suggest_venue,
)
from paperdb.ingest.types import Candidate
from paperdb.ingest.venue_resolve import ensure_venue
from paperdb.jobs.queue import enqueue
from paperdb.organize.rules import apply_rules_on
from paperdb.organize.tags import ensure_tag_path
from paperdb.organize.workspaces import stage_paper
from paperdb.papers.bibtex_import import (
    entry_as_json,
    entry_from_json,
    entry_to_candidate,
    parse_bibtex,
)
from paperdb.papers.service import create_paper, normalize_identifier

_BIBTEX_ENTRY_RE = re.compile(r"^\s*@[A-Za-z]+\s*[{(]")


async def merge_new_identifiers(
    session: AsyncSession, paper: Paper, identifiers: list[dict[str, str | bool]]
) -> list[str]:
    """Attach newly resolved identifiers to an existing paper.

    Cross-version imports (arXiv preprint vs published DOI) resolve through
    S2 with both ids; merging them into the surviving entry records the
    version linkage and makes future dedup work. Identifiers already owned
    by another paper are skipped silently.
    """
    added: list[str] = []
    for identifier in identifiers:
        try:
            scheme = IdentifierScheme(str(identifier.get("scheme", "")))
        except ValueError:
            continue
        value = str(identifier.get("value", ""))
        normalized = normalize_identifier(scheme, value)
        existing = await session.scalar(
            select(PaperIdentifier).where(
                PaperIdentifier.scheme == scheme,
                PaperIdentifier.normalized == normalized,
            )
        )
        if existing is not None:
            continue
        session.add(
            PaperIdentifier(
                paper_id=paper.id,
                scheme=scheme,
                raw_value=value,
                normalized=normalized,
                is_primary=False,
                source="import",
            )
        )
        added.append(f"{scheme.value}:{normalized}")
    if added:
        await session.flush()
    return added


def _duplicate_decision(candidate: Candidate, on_duplicate: str) -> tuple[str | None, str | None]:
    """Decide whether an import links to an existing paper.

    Returns (duplicate_paper_id, note). Identifier matches always win;
    a *strong* fuzzy match (exact normalized title, year within 1, first
    author not contradicting) additionally links under onDuplicate="link".
    """
    if candidate.duplicate_of and on_duplicate != "create":
        return candidate.duplicate_of["paperId"], None
    if (
        candidate.duplicate_of is None
        and on_duplicate == "link"
        and candidate.suggested_duplicate_of
        and candidate.suggested_duplicate_of.get("strong")
    ):
        suggested = candidate.suggested_duplicate_of
        note = f"linked by {suggested.get('evidence', 'fuzzy')} match"
        return str(suggested["paperId"]), note
    return None, None


async def _apply_import_options(
    session: AsyncSession, paper: Paper, options: dict, *, allow_stage: bool = True
) -> None:
    for path in options.get("defaultTags") or []:
        tag = await ensure_tag_path(session, str(path))
        if await session.get(PaperTag, (paper.id, tag.id)) is None:
            session.add(PaperTag(paper_id=paper.id, tag_id=tag.id))
    if options.get("defaultCollectionId"):
        collection = await session.get(Collection, uuid.UUID(str(options["defaultCollectionId"])))
        if collection is not None and (
            await session.get(CollectionPaper, (collection.id, paper.id)) is None
        ):
            session.add(CollectionPaper(collection_id=collection.id, paper_id=paper.id))
    if options.get("defaultWorkspaceId"):
        workspace = await session.get(
            ResearchWorkspace, uuid.UUID(str(options["defaultWorkspaceId"]))
        )
        # A paper already in the library keeps its visibility; only new or
        # already-staged papers are (re)attached to the staging area.
        if workspace is not None and workspace.closed_at is None and (allow_stage or paper.staged):
            await stage_paper(session, workspace, paper)


def _stats(items: list[ImportItem]) -> dict[str, int]:
    return {
        "total": len(items),
        "created": sum(1 for item in items if item.status == ImportItemStatus.created),
        "duplicates": sum(1 for item in items if item.status == ImportItemStatus.duplicate),
        "failed": sum(1 for item in items if item.status == ImportItemStatus.failed),
        "pending": sum(1 for item in items if item.status == ImportItemStatus.pending),
        "skipped": sum(1 for item in items if item.status == ImportItemStatus.skipped),
    }


async def create_import_job(
    session: AsyncSession,
    payload: ImportIdentifiersRequest,
    *,
    token_id: str | None = None,
) -> tuple[ImportJob, uuid.UUID]:
    # A pasted BibTeX entry sent through the identifiers endpoint would be split
    # by the client into one item per line and resolve random lines as titles.
    # Reject it here even if a third-party client bypasses the web UI.
    if any(_BIBTEX_ENTRY_RE.match(item) for item in payload.items):
        raise ApiError(
            422,
            "bibtex_in_identifiers",
            "This looks like BibTeX. Use the BibTeX import endpoint/file instead.",
        )
    import_job = ImportJob(
        id=uuid.uuid4(),
        kind=ImportKind.identifiers,
        status=JobStatus.pending,
        input={
            "options": {
                "onDuplicate": payload.options.on_duplicate,
                "defaultTags": payload.options.default_tags,
                "defaultCollectionId": (
                    str(payload.options.default_collection_id)
                    if payload.options.default_collection_id
                    else None
                ),
                "defaultWorkspaceId": (
                    str(payload.options.default_workspace_id)
                    if payload.options.default_workspace_id
                    else None
                ),
            }
        },
        stats={},
        created_by_token_id=uuid.UUID(token_id) if token_id else None,
    )
    import_job.items = [
        ImportItem(id=uuid.uuid4(), ordinal=index, raw_input=raw.strip())
        for index, raw in enumerate(payload.items, start=1)
    ]
    import_job.stats = _stats(import_job.items)
    session.add(import_job)
    await session.flush()
    job = await enqueue(
        session,
        "import_identifiers",
        {"import_id": str(import_job.id)},
    )
    import_job.job_id = job.id
    await session.flush()
    return import_job, job.id


def import_to_out(import_job: ImportJob) -> ImportJobOut:
    return ImportJobOut(
        id=import_job.id,
        kind=import_job.kind.value,
        status=import_job.status.value,
        stats=import_job.stats or {},
        created_at=import_job.created_at,
        started_at=import_job.started_at,
        finished_at=import_job.finished_at,
        error_summary=import_job.error_summary,
    )


def item_to_out(item: ImportItem) -> ImportItemOut:
    return ImportItemOut(
        id=item.id,
        ordinal=item.ordinal,
        raw_input=item.raw_input,
        status=item.status.value,
        paper_id=item.paper_id,
        error=item.error,
        attempts=item.attempts,
    )


def _paper_payload(
    candidate: Candidate, venue_id: uuid.UUID | None, edition_id: uuid.UUID | None
) -> PaperCreate:
    return PaperCreate(
        kind=candidate.kind,
        title=candidate.title,
        abstract=candidate.abstract,
        year=candidate.year,
        publication_date=candidate.publication_date,
        venue_id=venue_id,
        venue_edition_id=edition_id,
        volume=candidate.volume,
        issue=candidate.issue,
        pages=candidate.pages,
        citekey=candidate.citekey,
        added_via="import",
        authors=[
            PaperAuthorInput(
                display_name=author.name,
                family_name=author.family,
                given_name=author.given,
                orcid=author.orcid,
                dblp_pid=author.dblp_pid,
                s2_author_id=author.s2_author_id,
                openalex_id=author.openalex_id,
                affiliation=author.affiliation,
                position=index,
            )
            for index, author in enumerate(candidate.authors, start=1)
        ],
        identifiers=[
            IdentifierInput(
                scheme=str(identifier["scheme"]),
                value=str(identifier["value"]),
                is_primary=bool(identifier.get("isPrimary")),
            )
            for identifier in candidate.identifiers
        ],
    )


async def process_import(
    session: AsyncSession,
    import_id: uuid.UUID,
    settings: Settings,
) -> ImportJob:
    import_job = await session.get(ImportJob, import_id)
    if import_job is None:
        raise ApiError(404, "not_found", "Import job not found")
    items = list(
        (
            await session.scalars(
                select(ImportItem)
                .where(ImportItem.job_id == import_id)
                .order_by(ImportItem.ordinal)
            )
        ).all()
    )
    import_job.status = JobStatus.running
    import_job.started_at = import_job.started_at or datetime.now(UTC)
    await session.flush()

    for item in items:
        if item.status not in {ImportItemStatus.pending, ImportItemStatus.failed}:
            continue
        item.attempts += 1
        item.error = None
        try:
            candidate, warnings = await resolve_input(session, item.raw_input, settings)
            if candidate is None:
                raise ApiError(422, "resolve_failed", "; ".join(warnings) or "metadata not found")
            options = (import_job.input or {}).get("options") or {}
            on_duplicate = str(options.get("onDuplicate") or "link")
            duplicate_id, note = _duplicate_decision(candidate, on_duplicate)
            existing = None
            if duplicate_id:
                row = await session.get(Paper, uuid.UUID(duplicate_id))
                if row is not None and row.deleted_at is None:
                    existing = row
            if existing is not None:
                if on_duplicate == "skip":
                    item.status = ImportItemStatus.skipped
                else:
                    merged = await merge_new_identifiers(session, existing, candidate.identifiers)
                    if merged:
                        note = f"{note}; " if note else ""
                        note += f"+{len(merged)} identifier(s) merged"
                    item.status = ImportItemStatus.duplicate
                    item.paper_id = existing.id
                    await _apply_import_options(session, existing, options, allow_stage=False)
                    await apply_rules_on(session, existing, "import")
                if note:
                    item.error = note  # informational; status stays duplicate/skipped
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
                paper = await create_paper(
                    session,
                    _paper_payload(
                        candidate,
                        venue.id if venue else None,
                        edition.id if edition else None,
                    ),
                )
                await _apply_import_options(session, paper, options)
                await apply_rules_on(session, paper, "import")
                item.status = ImportItemStatus.created
                item.paper_id = paper.id
        except ApiError as exc:
            item.status = ImportItemStatus.failed
            item.error = str(exc.detail or exc.title)
        except Exception as exc:  # pragma: no cover - defensive boundary per item
            item.status = ImportItemStatus.failed
            item.error = f"internal: {exc}"
        import_job.stats = _stats(items)
        await session.commit()

    import_job.status = (
        JobStatus.succeeded if import_job.stats.get("failed", 0) == 0 else JobStatus.failed
    )
    import_job.finished_at = datetime.now(UTC)
    import_job.error_summary = (
        f"{import_job.stats.get('failed', 0)} item(s) failed"
        if import_job.stats.get("failed", 0)
        else None
    )
    await session.commit()
    return import_job


async def retry_item(session: AsyncSession, import_job: ImportJob, item: ImportItem) -> uuid.UUID:
    if item.status != ImportItemStatus.failed:
        raise ApiError(422, "not_retryable", "Only failed items can be retried")
    item.status = ImportItemStatus.pending
    item.error = None
    import_job.stats = _stats(list(import_job.items))
    import_job.status = JobStatus.pending
    job = await enqueue(session, "import_identifiers", {"import_id": str(import_job.id)})
    import_job.job_id = job.id
    await session.flush()
    return job.id


async def cancel_import(session: AsyncSession, import_job: ImportJob) -> None:
    import_job.status = JobStatus.dead
    import_job.finished_at = datetime.now(UTC)
    for item in import_job.items:
        if item.status == ImportItemStatus.pending:
            item.status = ImportItemStatus.skipped
    import_job.stats = _stats(list(import_job.items))
    await session.flush()


async def create_bibtex_import_job(
    session: AsyncSession,
    text: str,
    *,
    token_id: str | None = None,
    options: ImportOptions | None = None,
) -> tuple[ImportJob, uuid.UUID]:
    try:
        entries = parse_bibtex(text)
    except ValueError as exc:
        raise ApiError(422, "invalid_bibtex", str(exc)) from exc
    if not entries:
        raise ApiError(422, "invalid_bibtex", "No BibTeX entries found")
    if len(entries) > 500:
        raise ApiError(422, "too_many_entries", "At most 500 entries per import")
    options = options or ImportOptions()
    import_job = ImportJob(
        id=uuid.uuid4(),
        kind=ImportKind.bibtex,
        status=JobStatus.pending,
        input={
            "entries": len(entries),
            "options": {
                "onDuplicate": options.on_duplicate,
                "defaultTags": options.default_tags,
                "defaultCollectionId": (
                    str(options.default_collection_id) if options.default_collection_id else None
                ),
                "defaultWorkspaceId": (
                    str(options.default_workspace_id) if options.default_workspace_id else None
                ),
            },
        },
        stats={},
        created_by_token_id=uuid.UUID(token_id) if token_id else None,
    )
    import_job.items = [
        ImportItem(id=uuid.uuid4(), ordinal=index, raw_input=entry_as_json(entry))
        for index, entry in enumerate(entries, start=1)
    ]
    import_job.stats = _stats(import_job.items)
    session.add(import_job)
    await session.flush()
    job = await enqueue(session, "import_bibtex", {"import_id": str(import_job.id)})
    import_job.job_id = job.id
    await session.flush()
    return import_job, job.id


async def _bib_venue(session: AsyncSession, candidate: Candidate) -> None:
    if candidate.venue is None and candidate._venue_name:
        candidate.venue = await suggest_venue(
            session,
            raw_name=candidate._venue_name,
            kind=candidate.kind,
            year=candidate.year,
        )
    if candidate.venue and candidate.venue.venue_id:
        candidate.evaluation = await evaluate_venue(
            session, candidate.venue.venue_id, candidate.year
        )
    candidate.duplicate_of = await find_duplicate(session, candidate.identifiers)
    if candidate.duplicate_of is None:
        candidate.suggested_duplicate_of = await fuzzy_library_match(session, candidate)


async def process_bibtex(
    session: AsyncSession,
    import_id: uuid.UUID,
    settings: Settings,
) -> ImportJob:
    import_job = await session.get(ImportJob, import_id)
    if import_job is None:
        raise ApiError(404, "not_found", "Import job not found")
    items = list(
        (
            await session.scalars(
                select(ImportItem)
                .where(ImportItem.job_id == import_id)
                .order_by(ImportItem.ordinal)
            )
        ).all()
    )
    import_job.status = JobStatus.running
    import_job.started_at = import_job.started_at or datetime.now(UTC)
    await session.flush()

    for item in items:
        if item.status not in {ImportItemStatus.pending, ImportItemStatus.failed}:
            continue
        item.attempts += 1
        item.error = None
        options = (import_job.input or {}).get("options") or {}
        try:
            entry = entry_from_json(item.raw_input)
            bib_candidate = entry_to_candidate(entry)
            if bib_candidate is None:
                raise ApiError(422, "invalid_bibtex_entry", "Entry has no title")
            candidate = bib_candidate
            doi = next(
                (
                    str(identifier["value"])
                    for identifier in bib_candidate.identifiers
                    if identifier["scheme"] == "doi"
                ),
                None,
            )
            arxiv = next(
                (
                    str(identifier["value"])
                    for identifier in bib_candidate.identifiers
                    if identifier["scheme"] == "arxiv"
                ),
                None,
            )
            if doi or arxiv:
                resolved, _warnings = await resolve_input(
                    session, f"doi:{doi}" if doi else f"arxiv:{arxiv}", settings
                )
                if resolved is not None:
                    candidate = resolved
            candidate.citekey = bib_candidate.citekey or candidate.citekey
            if bib_candidate.kind != "preprint" and candidate.kind == "preprint":
                candidate.kind = bib_candidate.kind
                candidate._venue_name = bib_candidate._venue_name
            if candidate.venue is None:
                await _bib_venue(session, candidate)
            else:
                candidate.duplicate_of = await find_duplicate(session, candidate.identifiers)
                if candidate.duplicate_of is None:
                    candidate.suggested_duplicate_of = await fuzzy_library_match(session, candidate)
            duplicate_id, note = _duplicate_decision(candidate, "link")
            existing = None
            if duplicate_id:
                row = await session.get(Paper, uuid.UUID(duplicate_id))
                if row is not None and row.deleted_at is None:
                    existing = row
            if existing is not None:
                merged = await merge_new_identifiers(session, existing, candidate.identifiers)
                if merged:
                    note = f"{note}; " if note else ""
                    note += f"+{len(merged)} identifier(s) merged"
                item.status = ImportItemStatus.duplicate
                item.paper_id = existing.id
                await _apply_import_options(session, existing, options, allow_stage=False)
                if note:
                    item.error = note  # informational; status stays duplicate
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
                await _apply_import_options(session, paper, options)
                await apply_rules_on(session, paper, "import")
                item.status = ImportItemStatus.created
                item.paper_id = paper.id
        except ApiError as exc:
            item.status = ImportItemStatus.failed
            item.error = str(exc.detail or exc.title)
        except Exception as exc:
            item.status = ImportItemStatus.failed
            item.error = f"internal: {exc}"
        import_job.stats = _stats(items)
        await session.commit()

    import_job.status = (
        JobStatus.succeeded if import_job.stats.get("failed", 0) == 0 else JobStatus.failed
    )
    import_job.finished_at = datetime.now(UTC)
    import_job.error_summary = (
        f"{import_job.stats.get('failed', 0)} item(s) failed"
        if import_job.stats.get("failed", 0)
        else None
    )
    await session.commit()
    return import_job
