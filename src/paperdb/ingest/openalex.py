from __future__ import annotations

from typing import Any

import httpx

from paperdb.config import Settings
from paperdb.ingest.crossref import clean_text
from paperdb.ingest.http import get_json
from paperdb.ingest.types import AuthorRecord, SourceRecord


def _abstract(inverted: Any) -> str | None:
    if not isinstance(inverted, dict):
        return None
    positions: dict[int, str] = {}
    for word, indexes in inverted.items():
        if not isinstance(indexes, list):
            continue
        for index in indexes:
            if isinstance(index, int):
                positions[index] = word
    if not positions:
        return None
    return " ".join(positions[index] for index in sorted(positions))


def _strip_id(value: str | None) -> str | None:
    if not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def _record(work: dict[str, Any]) -> SourceRecord | None:
    title = clean_text(work.get("title"))
    if not title:
        return None
    authors = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        authors.append(
            AuthorRecord(
                name=clean_text(author.get("display_name")) or "Unknown",
                orcid=_strip_id(author.get("orcid")),
                openalex_id=_strip_id(author.get("id")),
                affiliation=((authorship.get("institutions") or [{}])[0].get("display_name")),
            )
        )
    source = (work.get("primary_location") or {}).get("source") or {}
    ids = work.get("ids") or {}
    doi = ids.get("doi") or work.get("doi")
    if isinstance(doi, str):
        doi = doi.replace("https://doi.org/", "").lower()
    biblio = work.get("biblio") or {}
    pages = None
    if biblio.get("first_page"):
        pages = biblio.get("first_page")
        if biblio.get("last_page"):
            pages = f"{pages}--{biblio['last_page']}"
    work_type = str(work.get("type") or "")
    kind = {
        "preprint": "preprint",
        "journal-article": "journal",
        "proceedings-article": "conference",
        "article": "journal",
    }.get(work_type)
    publication_date = work.get("publication_date")
    parsed_date = None
    if publication_date:
        from datetime import date

        try:
            parsed_date = date.fromisoformat(publication_date)
        except ValueError:
            parsed_date = None
    return SourceRecord(
        source="openalex",
        title=title,
        abstract=_abstract(work.get("abstract_inverted_index")),
        year=work.get("publication_year"),
        publication_date=parsed_date,
        kind=kind,
        venue=clean_text(source.get("display_name")),
        volume=clean_text(biblio.get("volume")),
        issue=clean_text(biblio.get("issue")),
        pages=pages,
        doi=doi,
        openalex_id=_strip_id(ids.get("openalex") or work.get("id")),
        url=work.get("id"),
        authors=authors,
        raw=work,
    )


async def fetch_openalex(
    client: httpx.AsyncClient, doi: str, settings: Settings
) -> SourceRecord | None:
    data = await get_json(
        client,
        "https://api.openalex.org/works",
        params={"filter": f"doi:{doi}", "per-page": 1, "mailto": settings.openalex_mailto},
        headers={"User-Agent": settings.arxiv_user_agent},
    )
    results = (data or {}).get("results") or []
    return _record(results[0]) if results else None


async def search_openalex(
    client: httpx.AsyncClient, query: str, settings: Settings
) -> SourceRecord | None:
    data = await get_json(
        client,
        "https://api.openalex.org/works",
        params={"search": query, "per-page": 1, "mailto": settings.openalex_mailto},
        headers={"User-Agent": settings.arxiv_user_agent},
    )
    results = (data or {}).get("results") or []
    return _record(results[0]) if results else None
