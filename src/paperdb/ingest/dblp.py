from __future__ import annotations

import re
from typing import Any

import httpx

from paperdb.config import Settings
from paperdb.ingest.crossref import clean_text
from paperdb.ingest.http import get_json
from paperdb.ingest.types import AuthorRecord, SourceRecord

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)


def _authors(info: dict[str, Any]) -> list[AuthorRecord]:
    raw = (info.get("authors") or {}).get("author")
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    authors = []
    for item in raw:
        name = clean_text(item.get("text")) or "Unknown"
        parts = name.split()
        authors.append(
            AuthorRecord(
                name=name,
                family=parts[-1] if len(parts) > 1 else None,
                given=" ".join(parts[:-1]) if len(parts) > 1 else None,
                dblp_pid=item.get("@pid"),
            )
        )
    return authors


def _doi_from_ee(info: dict[str, Any]) -> str | None:
    value = info.get("ee")
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    match = _DOI_RE.search(value)
    return match.group(0).lower() if match else None


def _record(info: dict[str, Any]) -> SourceRecord | None:
    title = (clean_text(info.get("title")) or "").rstrip(".") or None
    if not title:
        return None
    key = clean_text(info.get("key"))
    kind = None
    series_key = None
    if key:
        if key.startswith("conf/"):
            kind = "conference"
            series_key = "/".join(key.split("/")[:2])
        elif key.startswith("journals/"):
            kind = "journal"
            series_key = "/".join(key.split("/")[:2])
    year = info.get("year")
    try:
        year_int = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_int = None
    return SourceRecord(
        source="dblp",
        title=title,
        year=year_int,
        kind=kind,
        venue=clean_text(info.get("venue")),
        dblp_venue_key=series_key,
        dblp_key=key,
        doi=_doi_from_ee(info),
        url=clean_text(info.get("url")),
        authors=_authors(info),
        raw=info,
    )


async def search_dblp(
    client: httpx.AsyncClient, query: str, settings: Settings, *, rows: int = 5
) -> list[SourceRecord]:
    data = await get_json(
        client,
        "https://dblp.org/search/publ/api",
        params={"q": query, "format": "json", "h": rows},
        headers={"User-Agent": settings.arxiv_user_agent},
    )
    if not data:
        return []
    hits = (((data.get("result") or {}).get("hits") or {}).get("hit")) or []
    if isinstance(hits, dict):
        hits = [hits]
    records = []
    for hit in hits:
        record = _record(hit.get("info") or {})
        if record is not None:
            records.append(record)
    return records
