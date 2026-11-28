from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from paperdb import __version__
from paperdb.api.deps import get_principal, scoped
from paperdb.api.schemas import DatasetOut, LibraryStatsOut, MetaOut, StorageOut
from paperdb.auth.tokens import TokenPrincipal
from paperdb.config import Settings, get_settings
from paperdb.db.session import get_session
from paperdb.papers.stats import library_stats
from paperdb.venues.service import (
    library_counts,
    list_datasets,
    storage_meta,
    venue_gaps,
)

router = APIRouter(tags=["system"])


@router.get("/me")
async def me(principal: TokenPrincipal = Depends(get_principal)) -> dict[str, object]:
    return {
        "kind": principal.kind,
        "name": principal.name,
        "scopes": sorted(principal.scopes),
    }


@router.get("/library/stats", response_model=LibraryStatsOut)
async def library_stats_endpoint(
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> LibraryStatsOut:
    """Overview of the library in one call: totals, reading-status spread, and
    how much of it is actually readable (PDF present / text extracted)."""
    return LibraryStatsOut(**await library_stats(session))


@router.get("/meta", response_model=MetaOut)
async def meta(
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> MetaOut:
    datasets = await list_datasets(session)
    counts = await library_counts(session)
    gaps = await venue_gaps(session)
    storage = await storage_meta(settings)
    return MetaOut(
        version=__version__,
        datasets=[
            DatasetOut(
                scheme=item.scheme.value,
                version=item.version,
                label=item.label,
                source_url=item.source_url,
                checksum=item.checksum,
                imported_at=item.imported_at,
                is_current=item.is_current,
                valid_from=item.valid_from,
                valid_to=item.valid_to,
            )
            for item in datasets
        ],
        counts=counts,
        venues=gaps,
        storage=StorageOut(**storage),
        jobs={"pending": 0, "running": 0, "failed24h": 0},
    )


@router.get("/rank-datasets", response_model=list[DatasetOut])
async def rank_datasets(
    principal: TokenPrincipal = Depends(scoped("papers:read")),
    session: AsyncSession = Depends(get_session),
) -> list[DatasetOut]:
    datasets = await list_datasets(session)
    return [
        DatasetOut(
            scheme=item.scheme.value,
            version=item.version,
            label=item.label,
            source_url=item.source_url,
            checksum=item.checksum,
            imported_at=item.imported_at,
            is_current=item.is_current,
            valid_from=item.valid_from,
            valid_to=item.valid_to,
        )
        for item in datasets
    ]
