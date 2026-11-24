from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from paperdb.config import Settings
from paperdb.ingest.crossref import clean_text
from paperdb.ingest.http import get_json
from paperdb.ingest.types import AuthorRecord, SourceRecord

FIELDS = (
    "title,abstract,year,venue,publicationVenue,authors,externalIds,"
    "publicationDate,citationCount,referenceCount,url"
)


def _record(paper: dict[str, Any]) -> SourceRecord | None:
    title = clean_text(paper.get("title"))
    if not title:
        return None
    external = paper.get("externalIds") or {}
    publication_venue = paper.get("publicationVenue") or {}
    venue_type = str(publication_venue.get("type") or "").lower()
    kind = "journal" if venue_type in {"journal", "magazine"} else None
    if venue_type in {"conference", "conference-series"}:
        kind = "conference"
    authors = [
        AuthorRecord(
            name=clean_text(author.get("name")) or "Unknown",
            s2_author_id=str(author.get("authorId")) if author.get("authorId") else None,
        )
        for author in paper.get("authors") or []
    ]
    publication_date = paper.get("publicationDate")
    parsed_date = None
    if publication_date:
        from datetime import date

        try:
            parsed_date = date.fromisoformat(publication_date)
        except ValueError:
            parsed_date = None
    return SourceRecord(
        source="s2",
        title=title,
        abstract=clean_text(paper.get("abstract")),
        year=paper.get("year"),
        publication_date=parsed_date,
        kind=kind,
        venue=clean_text(paper.get("venue")) or clean_text(publication_venue.get("name")),
        doi=(external.get("DOI") or "").lower() or None,
        arxiv=external.get("ArXiv") or None,
        dblp_key=external.get("DBLP") or None,
        s2_id=paper.get("paperId"),
        url=paper.get("url"),
        authors=authors,
        raw=paper,
    )


async def fetch_s2(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    doi: str | None = None,
    arxiv: str | None = None,
) -> SourceRecord | None:
    if doi:
        paper_id = f"DOI:{doi}"
    elif arxiv:
        paper_id = f"arXiv:{arxiv}"
    else:
        return None
    headers = {"User-Agent": settings.arxiv_user_agent}
    if settings.s2_api_key:
        headers["x-api-key"] = settings.s2_api_key
    data = await get_json(
        client,
        f"https://api.semanticscholar.org/graph/v1/paper/{quote(paper_id, safe=':')}",
        params={"fields": FIELDS},
        headers=headers,
    )
    return _record(data) if data else None
