from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.db.models import (
    Venue,
    VenueAlias,
    VenueEdition,
    VenueIdentifier,
    VenueKind,
    VenueStatus,
)
from paperdb.ingest.types import VenueSuggestion
from paperdb.venues.normalize import normalize_name


async def _by_abbrev(
    session: AsyncSession, abbrev: str, venue_kind: VenueKind | None
) -> Venue | None:
    stmt = select(Venue).where(
        func.lower(Venue.abbrev) == abbrev.lower(),
        Venue.merged_into_id.is_(None),
    )
    if venue_kind is not None:
        stmt = stmt.where(Venue.kind == venue_kind)
    venue = await session.scalar(stmt.limit(1))
    # The kind carried by metadata sources is often missing or wrong (e.g.
    # landing-page candidates default to "journal"); a unique abbrev match
    # should not be discarded just because the kind disagrees.
    if venue is None and venue_kind is not None:
        venue = await session.scalar(
            select(Venue)
            .where(
                func.lower(Venue.abbrev) == abbrev.lower(),
                Venue.merged_into_id.is_(None),
            )
            .limit(1)
        )
    return venue


async def _by_normalized_name(
    session: AsyncSession, normalized: str, venue_kind: VenueKind | None
) -> Venue | None:
    stmt = select(Venue).where(Venue.normalized_name == normalized)
    if venue_kind is not None:
        stmt = stmt.where(Venue.kind == venue_kind)
    venue = await session.scalar(stmt.limit(1))
    if venue is None and venue_kind is not None:
        venue = await session.scalar(
            select(Venue)
            .where(
                Venue.normalized_name == normalized,
                Venue.merged_into_id.is_(None),
            )
            .limit(1)
        )
    return venue


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    size = len(needle)
    if size < 3 or size > len(haystack):
        return False
    return any(haystack[i : i + size] == needle for i in range(len(haystack) - size + 1))


def _is_subsequence(haystack: list[str], needle: list[str]) -> bool:
    """True when every token of ``needle`` appears in order in ``haystack``.

    Tolerates publisher-inserted words ("ACM SIGSAC Conference on ..." vs CCF's
    "ACM Conference on ..."), while rejecting a different venue: a token the
    canonical name requires (e.g. "Asia" for AsiaCCS) is never skipped.
    """
    position = 0
    for token in needle:
        while position < len(haystack) and haystack[position] != token:
            position += 1
        if position == len(haystack):
            return False
        position += 1
    return True


async def _by_containment(
    session: AsyncSession, normalized: str, venue_kind: VenueKind | None
) -> Venue | None:
    """Match a decorated proceedings title to the canonical venue it embeds.

    Crossref and publisher pages spell venues out ("Proceedings 2026 Network
    and Distributed System Security Symposium", "Proceedings of the 2024 on ACM
    SIGSAC Conference on ..."), while the library stores the bare name. The
    canonical name is then an ordered token subsequence of the candidate name;
    a contiguous run beats a gapped one, and the longest name wins ties.
    """
    tokens = normalized.split()
    if len(tokens) < 3:
        return None
    stmt = select(Venue).where(Venue.merged_into_id.is_(None))
    if venue_kind is not None:
        stmt = stmt.where(Venue.kind == venue_kind)
    best: tuple[tuple[int, int], Venue] | None = None
    for candidate in (await session.scalars(stmt)).all():
        needle = candidate.normalized_name.split()
        if len(needle) < 3 or not _is_subsequence(tokens, needle):
            continue
        score = (1 if _contains_run(tokens, needle) else 0, len(needle))
        if best is None or score > best[0]:
            best = (score, candidate)
    if best is None and venue_kind is not None:
        return await _by_containment(session, normalized, None)
    return best[1] if best else None


async def suggest_venue(
    session: AsyncSession,
    *,
    raw_name: str | None,
    kind: str | None,
    year: int | None,
    dblp_venue_key: str | None = None,
    edition_dblp_key: str | None = None,
) -> VenueSuggestion | None:
    if not raw_name and not dblp_venue_key:
        return None
    try:
        venue_kind = VenueKind(kind) if kind else None
    except ValueError:
        venue_kind = None

    venue = None
    evidence = "unmatched"
    if dblp_venue_key:
        identifier = await session.scalar(
            select(VenueIdentifier).where(
                VenueIdentifier.source == "dblp",
                VenueIdentifier.normalized == dblp_venue_key,
            )
        )
        if identifier is not None:
            venue = await session.get(Venue, identifier.venue_id)
            evidence = "dblp"

    if venue is None and raw_name:
        # ACL Anthology / Crossref proceedings titles often spell the venue out
        # ("Proceedings of the 62nd Annual Meeting of the Association for
        # Computational Linguistics") without the acronym used by CCF. Map the
        # common NLP families before the exact/alias lookup fails.
        lower_name = raw_name.lower()
        if "findings of the" not in lower_name:
            proceedings_hints = (
                (
                    "north american chapter of the association for computational linguistics",
                    "NAACL",
                ),
                (
                    "european chapter of the association for computational linguistics",
                    "EACL",
                ),
                ("association for computational linguistics", "ACL"),
                ("international conference on machine learning", "ICML"),
                ("empirical methods in natural language processing", "EMNLP"),
                ("international conference on computational linguistics", "COLING"),
                ("transactions of the association for computational linguistics", "TACL"),
            )
            for needle, abbrev in proceedings_hints:
                if needle not in lower_name:
                    continue
                venue = await _by_abbrev(session, abbrev, venue_kind)
                if venue is not None:
                    evidence = "proceedings"
                break

    if venue is None and raw_name:
        normalized = normalize_name(raw_name)
        venue = await _by_normalized_name(session, normalized, venue_kind)
        if venue is not None:
            evidence = "name"
        else:
            alias = await session.scalar(
                select(VenueAlias).where(VenueAlias.normalized_alias == normalized).limit(1)
            )
            if alias is not None:
                venue = await session.get(Venue, alias.venue_id)
                evidence = "alias"

    if venue is None and raw_name:
        # Proceedings titles often use an acronym ("Proceedings of the 2024 CHI
        # Conference ..."), while CCF stores the full name under a short abbrev.
        # Landing pages contribute venue hints like "NDSS Symposium".
        acronyms = {
            word.lower()
            for word in re.findall(r"[A-Z][A-Za-z0-9&+-]{2,}", raw_name)
            if word.isupper()
        }
        if acronyms:
            stmt = select(Venue).where(
                Venue.merged_into_id.is_(None),
                func.lower(Venue.abbrev).in_(acronyms),
            )
            if venue_kind is not None:
                stmt = stmt.where(Venue.kind == venue_kind)
            matches = list((await session.scalars(stmt.limit(10))).all())
            if not matches and venue_kind is not None:
                matches = list(
                    (
                        await session.scalars(
                            select(Venue)
                            .where(
                                Venue.merged_into_id.is_(None),
                                func.lower(Venue.abbrev).in_(acronyms),
                            )
                            .limit(10)
                        )
                    ).all()
                )
            tokens = set(normalize_name(raw_name).split())
            scored = [
                (len(tokens & set(normalize_name(item.full_name).split())), item)
                for item in matches
            ]
            scored.sort(key=lambda pair: pair[0], reverse=True)
            if scored and (len(scored) == 1 or scored[0][0] >= 2):
                venue = scored[0][1]
                evidence = "acronym"

    if (
        venue is None
        and raw_name
        and re.match(r"(?i)^(?:in\s+)?(?:proceedings|proc\.?|procs\.?)\b", raw_name.strip())
    ):
        # "Proceedings ..." titles: the canonical venue is embedded verbatim.
        venue = await _by_containment(session, normalize_name(raw_name), venue_kind)
        if venue is not None:
            evidence = "proceedings"

    if venue is None:
        return VenueSuggestion(
            venue_id=None,
            full_name=raw_name or dblp_venue_key or "",
            abbrev=None,
            kind=kind or "conference",
            year=year,
            track="main",
            edition_dblp_key=edition_dblp_key,
            confidence=0.4,
            evidence=evidence,
        )

    return VenueSuggestion(
        venue_id=str(venue.id),
        full_name=venue.full_name,
        abbrev=venue.abbrev,
        kind=venue.kind.value,
        year=year,
        track="main",
        edition_dblp_key=edition_dblp_key,
        confidence=0.95 if evidence == "dblp" else 0.85,
        evidence=evidence,
    )


async def ensure_venue(
    session: AsyncSession,
    suggestion: VenueSuggestion,
    *,
    raw_name: str | None,
    kind: str,
    year: int | None,
    dblp_venue_key: str | None = None,
    edition_dblp_key: str | None = None,
) -> tuple[Venue | None, VenueEdition | None]:
    venue = (
        await session.get(Venue, uuid.UUID(suggestion.venue_id)) if suggestion.venue_id else None
    )
    if venue is None:
        name = (raw_name or suggestion.full_name or "").strip()
        if not name:
            return None, None
        try:
            venue_kind = VenueKind(kind)
        except ValueError:
            venue_kind = VenueKind.conference
        normalized = normalize_name(name)
        existing = await session.scalar(
            select(Venue).where(
                Venue.kind == venue_kind,
                Venue.normalized_name == normalized,
                Venue.merged_into_id.is_(None),
            )
        )
        if existing is not None:
            venue = existing
        else:
            venue = Venue(
                id=uuid.uuid4(),
                kind=venue_kind,
                full_name=name,
                normalized_name=normalized,
                abbrev=None,
                dblp_key=None,
                status=VenueStatus.provisional,
            )
            session.add(venue)
            await session.flush()
            session.add(
                VenueAlias(
                    venue_id=venue.id,
                    alias=name,
                    normalized_alias=normalized,
                    source="ingest",
                )
            )
        if dblp_venue_key and venue.dblp_key is None:
            identifier = await session.scalar(
                select(VenueIdentifier).where(
                    VenueIdentifier.source == "dblp",
                    VenueIdentifier.normalized == dblp_venue_key,
                )
            )
            if identifier is None:
                session.add(
                    VenueIdentifier(
                        venue_id=venue.id,
                        source="dblp",
                        value=dblp_venue_key,
                        normalized=dblp_venue_key,
                        is_primary=True,
                    )
                )
                venue.dblp_key = dblp_venue_key

    edition = None
    if venue.kind == VenueKind.conference and year is not None:
        edition = await session.scalar(
            select(VenueEdition).where(
                VenueEdition.venue_id == venue.id,
                VenueEdition.year == year,
                VenueEdition.track == "main",
            )
        )
        if edition is None:
            edition = VenueEdition(
                id=uuid.uuid4(),
                venue_id=venue.id,
                year=year,
                track="main",
                dblp_key=edition_dblp_key,
                is_primary=True,
            )
            session.add(edition)
            await session.flush()
    await session.flush()
    return venue, edition
