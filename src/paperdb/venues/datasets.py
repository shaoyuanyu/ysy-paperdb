from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb.db.models import (
    DatasetVersion,
    RankFieldKind,
    RankScheme,
    Venue,
    VenueAlias,
    VenueIdentifier,
    VenueKind,
    VenueRank,
    VenueStatus,
)
from paperdb.venues.normalize import (
    ccf_field_key,
    dblp_series_key,
    normalize_issn,
    normalize_name,
    parse_issn_pair,
)

CAS_MAJOR_FIELD_EN = {"计算机科学": "Computer Science"}


@dataclass(slots=True)
class ImportStats:
    scheme: str
    version: str
    status: str = "imported"
    rows: int = 0
    venues_created: int = 0
    matched_by_identifier: int = 0
    matched_by_name: int = 0
    duplicates_merged: int = 0
    missing_abbrev: int = 0
    ranks: int = 0
    checksum: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_dataset(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


class _Indexes:
    def __init__(
        self,
        venues: list[Venue],
        aliases: list[VenueAlias],
        identifiers: list[VenueIdentifier],
    ):
        self.name: dict[tuple[str, str], Venue] = {
            (str(venue.kind), venue.normalized_name): venue for venue in venues
        }
        self.dblp: dict[str, Venue] = {}
        self.issn: dict[str, Venue] = {}
        for venue in venues:
            if venue.dblp_key:
                self.dblp[venue.dblp_key] = venue
            if venue.issn_print:
                self.issn[normalize_issn(venue.issn_print)] = venue
            if venue.issn_online:
                self.issn[normalize_issn(venue.issn_online)] = venue
        self.alias_keys: set[tuple[uuid.UUID, str]] = set()
        for alias in aliases:
            self.alias_keys.add((alias.venue_id, alias.normalized_alias))
        self.identifier_keys: set[tuple[str, str]] = set()
        by_id = {venue.id: venue for venue in venues}
        for identifier in identifiers:
            self.identifier_keys.add((identifier.source, identifier.normalized))
            venue = by_id.get(identifier.venue_id)
            if venue is not None and identifier.source == "dblp":
                self.dblp[identifier.normalized] = venue
            if venue is not None and identifier.source == "issn":
                self.issn[identifier.normalized] = venue


async def _load_indexes(session: AsyncSession) -> _Indexes:
    venues = list((await session.scalars(select(Venue))).all())
    aliases = list((await session.scalars(select(VenueAlias))).all())
    identifiers = list((await session.scalars(select(VenueIdentifier))).all())
    return _Indexes(venues, aliases, identifiers)


def _add_alias(
    session: AsyncSession,
    indexes: _Indexes,
    venue: Venue,
    alias: str | None,
    source: str,
) -> None:
    if not alias:
        return
    normalized = normalize_name(alias)
    if not normalized:
        return
    key = (venue.id, normalized)
    if key in indexes.alias_keys:
        return
    session.add(
        VenueAlias(
            venue_id=venue.id,
            alias=alias,
            normalized_alias=normalized,
            source=source,
        )
    )
    indexes.alias_keys.add(key)


def _add_identifier(
    session: AsyncSession,
    indexes: _Indexes,
    venue: Venue,
    source: str,
    value: str,
    normalized: str,
    *,
    is_primary: bool = False,
) -> None:
    key = (source, normalized)
    if key in indexes.identifier_keys:
        return
    session.add(
        VenueIdentifier(
            venue_id=venue.id,
            source=source,
            value=value,
            normalized=normalized,
            is_primary=is_primary,
        )
    )
    indexes.identifier_keys.add(key)


def _new_venue(
    *,
    kind: VenueKind,
    full_name: str,
    normalized_name: str,
    abbrev: str | None,
    status: VenueStatus,
) -> Venue:
    return Venue(
        id=uuid.uuid4(),
        kind=kind,
        full_name=full_name,
        normalized_name=normalized_name,
        abbrev=abbrev or None,
        status=status,
    )


async def import_dataset(
    session: AsyncSession,
    *,
    scheme: str,
    version: str,
    payload: dict[str, Any],
    checksum: str,
    valid_from: int,
    valid_to: int | None = None,
    label: str | None = None,
    source_url: str | None = None,
    force: bool = False,
) -> ImportStats:
    scheme_enum = RankScheme(scheme)
    stats = ImportStats(scheme=scheme_enum.value, version=version, checksum=checksum)

    existing = await session.get(DatasetVersion, (scheme_enum, version))
    if existing is not None and existing.checksum == checksum and not force:
        stats.status = "skipped"
        return stats

    if existing is None:
        existing = DatasetVersion(
            scheme=scheme_enum,
            version=version,
            label=label or f"{scheme_enum.value.upper()} {version}",
            source_url=source_url,
            checksum=checksum,
            is_current=True,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        session.add(existing)
    else:
        existing.label = label or existing.label
        existing.source_url = source_url or existing.source_url
        existing.checksum = checksum
        existing.is_current = True
        existing.valid_from = valid_from
        existing.valid_to = valid_to
    await session.execute(
        update(DatasetVersion).where(DatasetVersion.scheme == scheme_enum).values(is_current=False)
    )
    existing.is_current = True
    await session.flush()

    await session.execute(
        delete(VenueRank).where(
            VenueRank.scheme == scheme_enum,
            VenueRank.dataset_version == version,
        )
    )
    indexes = await _load_indexes(session)
    rank_keys: set[tuple[uuid.UUID, str, str, str]] = set()
    ranks: list[VenueRank] = []
    primary_ccf: set[uuid.UUID] = set()

    if scheme_enum == RankScheme.ccf:
        stats.rows = len(payload.get("conferences", [])) + len(payload.get("journals", []))
        for kind, entries in (
            (VenueKind.conference, payload.get("conferences", [])),
            (VenueKind.journal, payload.get("journals", [])),
        ):
            for entry in entries:
                full_name = str(entry["n"]).strip()
                abbrev = str(entry.get("a") or "").strip()
                normalized = normalize_name(full_name)
                key = dblp_series_key(entry.get("d"))

                matched_by_identifier = False
                matched_by_name = False
                venue = None
                if key:
                    candidate = indexes.dblp.get(key)
                    # DBLP series keys are not globally unique in the CCF snapshot:
                    # the same key can label two different venues. Only accept it
                    # when the normalized name agrees; otherwise fall back to name.
                    if candidate is not None and candidate.normalized_name == normalized:
                        venue = candidate
                        matched_by_identifier = True
                    elif candidate is not None:
                        stats.warnings.append(
                            f"dblp key collision ignored: {key} "
                            f"({candidate.normalized_name!r} vs {normalized!r})"
                        )
                if venue is None:
                    venue = indexes.name.get((kind.value, normalized))
                    matched_by_name = venue is not None

                if venue is None:
                    venue = _new_venue(
                        kind=kind,
                        full_name=full_name,
                        normalized_name=normalized,
                        abbrev=abbrev or None,
                        status=VenueStatus.confirmed if key else VenueStatus.provisional,
                    )
                    session.add(venue)
                    indexes.name[(kind.value, normalized)] = venue
                    stats.venues_created += 1
                else:
                    if matched_by_identifier:
                        stats.matched_by_identifier += 1
                    elif matched_by_name:
                        stats.matched_by_name += 1
                        if venue.full_name != full_name:
                            stats.duplicates_merged += 1
                    if venue.abbrev is None and abbrev:
                        venue.abbrev = abbrev

                if key and venue.dblp_key is None:
                    # venue.dblp_key is globally unique; never repoint it to a
                    # second venue with the same key.
                    if ("dblp", key) not in indexes.identifier_keys:
                        venue.dblp_key = key
                    else:
                        stats.warnings.append(f"dblp key already attached: {key}")
                if key:
                    indexes.dblp[key] = venue
                    _add_identifier(session, indexes, venue, "dblp", key, key, is_primary=True)
                if not abbrev:
                    stats.missing_abbrev += 1
                _add_alias(session, indexes, venue, full_name, "ccf")
                _add_alias(session, indexes, venue, abbrev, "ccf")

                field = str(entry.get("f") or "").strip() or None
                if field and not venue.ccf_field:
                    venue.ccf_field = field
                field_key = ccf_field_key(field)
                if field_key and not venue.ccf_field_key:
                    venue.ccf_field_key = field_key

                rank_key = (venue.id, RankFieldKind.ccf_field.value, field or "", version)
                if rank_key in rank_keys:
                    continue
                rank_keys.add(rank_key)
                is_primary = venue.id not in primary_ccf
                if is_primary:
                    primary_ccf.add(venue.id)
                ranks.append(
                    VenueRank(
                        id=uuid.uuid4(),
                        venue_id=venue.id,
                        scheme=RankScheme.ccf,
                        dataset_version=version,
                        field_kind=RankFieldKind.ccf_field,
                        field_name=field or "(unknown)",
                        field_key=field_key,
                        level=str(entry.get("l") or ""),
                        is_primary=is_primary,
                        valid_from=valid_from,
                        valid_to=valid_to,
                        source_url=source_url,
                        imported_at=datetime.now(UTC),
                    )
                )

    elif scheme_enum == RankScheme.cas:
        stats.rows = len(payload.get("rows", []))
        for row in payload.get("rows", []):
            full_name = str(row["n"]).strip()
            normalized = normalize_name(full_name)
            issn_print_raw, issn_online_raw = parse_issn_pair(str(row.get("i") or ""))
            venue = None
            for issn in (issn_print_raw, issn_online_raw):
                if issn and issn in indexes.issn:
                    venue = indexes.issn[issn]
                    stats.matched_by_identifier += 1
                    break
            if venue is None:
                venue = indexes.name.get((VenueKind.journal.value, normalized))
                if venue is not None:
                    stats.matched_by_name += 1
            if venue is None:
                venue = _new_venue(
                    kind=VenueKind.journal,
                    full_name=full_name,
                    normalized_name=normalized,
                    abbrev=None,
                    status=(
                        VenueStatus.confirmed
                        if issn_print_raw or issn_online_raw
                        else VenueStatus.provisional
                    ),
                )
                session.add(venue)
                indexes.name[(VenueKind.journal.value, normalized)] = venue
                stats.venues_created += 1
            if issn_print_raw and not venue.issn_print:
                venue.issn_print = issn_print_raw
            if issn_online_raw and not venue.issn_online:
                venue.issn_online = issn_online_raw
            for issn in (issn_print_raw, issn_online_raw):
                if issn:
                    indexes.issn[issn] = venue
                    _add_identifier(
                        session,
                        indexes,
                        venue,
                        "issn",
                        issn,
                        issn,
                        is_primary=issn is issn_print_raw,
                    )
            _add_alias(session, indexes, venue, full_name, "cas")

            major = row.get("m") or []
            if len(major) >= 4:
                field_name = str(major[0])
                rank_key = (venue.id, RankFieldKind.cas_major.value, field_name, version)
                if rank_key not in rank_keys:
                    rank_keys.add(rank_key)
                    ranks.append(
                        VenueRank(
                            id=uuid.uuid4(),
                            venue_id=venue.id,
                            scheme=RankScheme.cas,
                            dataset_version=version,
                            field_kind=RankFieldKind.cas_major,
                            field_name=field_name,
                            field_name_en=CAS_MAJOR_FIELD_EN.get(field_name),
                            level=str(major[1]),
                            is_top=bool(row.get("top")),
                            is_primary=True,
                            rank_in_field=int(major[2]),
                            total_in_field=int(major[3]),
                            valid_from=valid_from,
                            valid_to=valid_to,
                            source_url=source_url,
                            imported_at=datetime.now(UTC),
                        )
                    )
            for minor in row.get("s") or []:
                if len(minor) < 5:
                    continue
                en, zh, level, rank, total = minor
                field_name = str(zh)
                rank_key = (venue.id, RankFieldKind.cas_minor.value, field_name, version)
                if rank_key in rank_keys:
                    continue
                rank_keys.add(rank_key)
                ranks.append(
                    VenueRank(
                        id=uuid.uuid4(),
                        venue_id=venue.id,
                        scheme=RankScheme.cas,
                        dataset_version=version,
                        field_kind=RankFieldKind.cas_minor,
                        field_name=field_name,
                        field_name_en=str(en),
                        level=str(level),
                        is_primary=False,
                        rank_in_field=int(rank),
                        total_in_field=int(total),
                        valid_from=valid_from,
                        valid_to=valid_to,
                        source_url=source_url,
                        imported_at=datetime.now(UTC),
                    )
                )
    else:
        raise ValueError(f"unknown scheme: {scheme}")

    session.add_all(ranks)
    stats.ranks = len(ranks)
    await session.flush()
    return stats
