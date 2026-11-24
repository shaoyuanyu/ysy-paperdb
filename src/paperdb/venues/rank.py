from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from paperdb.db.models import RankScheme, Venue, VenueRank


@dataclass(slots=True)
class RankChoice:
    rank: VenueRank
    dataset_version: str
    fallback: bool


def _version_key(value: str) -> int:
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else 0


def choose_rank(
    rows: list[VenueRank], year: int, *, primary_only: bool = True
) -> RankChoice | None:
    pool = [row for row in rows if row.is_primary] if primary_only else list(rows)
    if not pool:
        return None

    applicable = [
        row
        for row in pool
        if (row.valid_from is None or row.valid_from <= year)
        and (row.valid_to is None or year <= row.valid_to)
    ]
    if applicable:
        best = max(applicable, key=lambda row: _version_key(row.dataset_version))
        return RankChoice(best, best.dataset_version, False)

    # Paper predates (or falls in a gap between) known editions: use the nearest
    # prior edition if any, otherwise the latest one, and mark the fallback.
    prior = [row for row in pool if row.valid_from is not None and row.valid_from <= year]
    candidates = prior or pool
    best = max(candidates, key=lambda row: (_version_key(row.dataset_version), row.valid_from or 0))
    return RankChoice(best, best.dataset_version, True)


def _ccf(rows: list[VenueRank], year: int) -> dict[str, Any] | None:
    choice = choose_rank([row for row in rows if row.scheme == RankScheme.ccf], year)
    if choice is None:
        return None
    rank = choice.rank
    return {
        "level": rank.level,
        "field": rank.field_name,
        "fieldKey": rank.field_key,
        "datasetVersion": choice.dataset_version,
        "fallback": choice.fallback,
    }


def _cas(rows: list[VenueRank], year: int, kind: str) -> dict[str, Any] | None:
    if kind != "journal":
        return None
    cas_rows = [row for row in rows if row.scheme == RankScheme.cas]
    major_rows = [row for row in cas_rows if row.field_kind == "cas_major"]
    choice = choose_rank(major_rows, year)
    if choice is None:
        return None
    major = choice.rank
    minors = [
        row
        for row in cas_rows
        if row.field_kind == "cas_minor" and row.dataset_version == choice.dataset_version
    ]
    return {
        "level": major.level,
        "isTop": major.is_top,
        "datasetVersion": choice.dataset_version,
        "fallback": choice.fallback,
        "majorField": major.field_name,
        "majorFieldEn": major.field_name_en,
        "minorFields": [
            {
                "fieldName": row.field_name,
                "fieldNameEn": row.field_name_en,
                "level": row.level,
                "rank": row.rank_in_field,
                "total": row.total_in_field,
            }
            for row in sorted(minors, key=lambda item: item.field_name)
        ],
    }


def resolve_evaluation(venue: Venue | None, rows: list[VenueRank], year: int) -> dict[str, Any]:
    if venue is None:
        return {"ccf": None, "cas": None}
    return {
        "ccf": _ccf(rows, year),
        "cas": _cas(rows, year, str(venue.kind)),
    }
