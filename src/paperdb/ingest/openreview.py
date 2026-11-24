from __future__ import annotations

import re

import httpx

from paperdb.config import Settings
from paperdb.ingest.crossref import clean_text
from paperdb.ingest.http import get_json
from paperdb.ingest.types import AuthorRecord, SourceRecord


def _value(content: dict, key: str):
    value = content.get(key)
    if isinstance(value, dict):
        return value.get("value")
    return value


async def fetch_openreview(
    client: httpx.AsyncClient, note_id: str, settings: Settings
) -> SourceRecord | None:
    data = await get_json(
        client,
        "https://api2.openreview.net/notes",
        params={"forum": note_id, "limit": 1},
        headers={"User-Agent": settings.arxiv_user_agent},
    )
    notes = (data or {}).get("notes") or []
    if not notes:
        return None
    note = notes[0]
    content = note.get("content") or {}
    title = clean_text(_value(content, "title"))
    if not title:
        return None
    authors_raw = _value(content, "authors") or []
    if isinstance(authors_raw, str):
        authors_raw = [authors_raw]
    authors = []
    for name in authors_raw:
        clean = clean_text(name) or "Unknown"
        parts = clean.split()
        authors.append(
            AuthorRecord(
                name=clean,
                family=parts[-1] if len(parts) > 1 else None,
                given=" ".join(parts[:-1]) if len(parts) > 1 else None,
            )
        )
    venue = clean_text(_value(content, "venue")) or clean_text(_value(content, "venueid"))
    year = None
    if venue:
        match = re.search(r"(19|20)\d{2}", venue)
        if match:
            year = int(match.group(0))
    doi = clean_text(_value(content, "doi"))
    if doi:
        doi = doi.replace("https://doi.org/", "").strip().lower()
    return SourceRecord(
        source="openreview",
        title=title,
        abstract=clean_text(_value(content, "abstract")),
        year=year,
        kind="conference",
        venue=venue,
        doi=doi,
        openreview_id=note.get("forum") or note_id,
        url=f"https://openreview.net/forum?id={note.get('forum') or note_id}",
        authors=authors,
        raw={"note": note},
    )
