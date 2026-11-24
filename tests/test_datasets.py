from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from paperdb.db.models import (
    DatasetVersion,
    RankScheme,
    Venue,
    VenueIdentifier,
    VenueRank,
)
from paperdb.db.session import get_sessionmaker
from paperdb.venues.datasets import import_dataset, load_dataset


async def test_seeded_counts() -> None:
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(Venue)) == 677 + 547
        assert await session.scalar(select(func.count()).select_from(VenueRank)) == 681 + 2060
        assert (
            await session.scalar(
                select(func.count())
                .select_from(VenueRank)
                .where(VenueRank.scheme == RankScheme.ccf)
            )
            == 681
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(VenueRank)
                .where(VenueRank.scheme == RankScheme.cas)
            )
            == 2060
        )


async def test_ccf_rank_and_empty_abbrev() -> None:
    async with get_sessionmaker()() as session:
        aaai = await session.scalar(
            select(Venue).where(
                Venue.abbrev == "AAAI", Venue.normalized_name.like("aaai conference%")
            )
        )
        assert aaai is not None
        rank = await session.scalar(
            select(VenueRank).where(
                VenueRank.venue_id == aaai.id,
                VenueRank.scheme == RankScheme.ccf,
                VenueRank.is_primary.is_(True),
            )
        )
        assert rank is not None
        assert rank.level == "A"
        assert rank.valid_from == 2026

        parallel = await session.scalar(
            select(Venue).where(Venue.normalized_name == "parallel computing")
        )
        assert parallel is not None
        assert parallel.abbrev is None


async def test_cas_rank_and_top() -> None:
    async with get_sessionmaker()() as session:
        venue = await session.scalar(
            select(Venue).where(
                Venue.normalized_name == "ieee communications surveys and tutorials"
            )
        )
        assert venue is not None
        major = await session.scalar(
            select(VenueRank).where(
                VenueRank.venue_id == venue.id,
                VenueRank.scheme == RankScheme.cas,
                VenueRank.field_kind == "cas_major",
            )
        )
        assert major is not None
        assert major.level == "1"
        assert major.is_top is True
        minors = (
            await session.scalars(
                select(VenueRank).where(
                    VenueRank.venue_id == venue.id,
                    VenueRank.scheme == RankScheme.cas,
                    VenueRank.field_kind == "cas_minor",
                )
            )
        ).all()
        assert minors
        assert all(minor.field_name_en for minor in minors)


async def test_dblp_collision_is_not_merged() -> None:
    async with get_sessionmaker()() as session:
        fse = (
            await session.scalars(
                select(Venue).where(
                    Venue.normalized_name.in_(
                        [
                            "fast software encryption",
                            "acm international conference on the "
                            "foundations of software engineering",
                        ]
                    )
                )
            )
        ).all()
        assert len(fse) == 2
        with_key = [venue for venue in fse if venue.dblp_key == "conf/fse"]
        assert len(with_key) == 1
        identifiers = (
            await session.scalars(
                select(VenueIdentifier).where(
                    VenueIdentifier.source == "dblp", VenueIdentifier.normalized == "conf/fse"
                )
            )
        ).all()
        assert len(identifiers) == 1


async def test_dataset_import_is_idempotent() -> None:
    payload, checksum = load_dataset(Path("datasets/ccf-2026.json"))
    async with get_sessionmaker()() as session:
        stats = await import_dataset(
            session,
            scheme="ccf",
            version="2026",
            payload=payload,
            checksum=checksum,
            valid_from=2026,
        )
        await session.commit()
        assert stats.status == "skipped"
        assert await session.scalar(select(func.count()).select_from(DatasetVersion)) == 2
