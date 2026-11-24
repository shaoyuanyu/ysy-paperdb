from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from paperdb.config import Settings
from paperdb.ingest.http import get_json
from paperdb.ingest.types import AuthorRecord, SourceRecord

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", value)).strip()
    return text or None


def parse_date(value: Any) -> date | None:
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts or not parts[0]:
        return None
    numbers = [*list(parts[0]), 1, 1]
    try:
        return date(int(numbers[0]), int(numbers[1]), int(numbers[2]))
    except (TypeError, ValueError):
        return None


def _strip_orcid(value: str | None) -> str | None:
    if not value:
        return None
    return value.rsplit("/", 1)[-1]


async def fetch_crossref(
    client: httpx.AsyncClient, doi: str, settings: Settings
) -> SourceRecord | None:
    data = await get_json(
        client,
        f"https://api.crossref.org/works/{quote(doi, safe='')}",
        params={"mailto": settings.crossref_mailto},
        headers={"User-Agent": settings.arxiv_user_agent},
    )
    if not data or "message" not in data:
        return None
    message = data["message"]
    authors = [
        AuthorRecord(
            name=" ".join(part for part in (person.get("given"), person.get("family")) if part)
            or clean_text(person.get("name"))
            or "Unknown",
            family=clean_text(person.get("family")),
            given=clean_text(person.get("given")),
            orcid=_strip_orcid(person.get("ORCID")),
            affiliation=(
                person.get("affiliation", [{}])[0].get("name")
                if person.get("affiliation")
                else None
            ),
        )
        for person in message.get("author", [])
    ]
    container = clean_text(message.get("container-title"))
    event = message.get("event") or {}
    venue = container or clean_text(event.get("name"))
    raw_type = str(message.get("type") or "")
    kind = {
        "journal-article": "journal",
        "proceedings-article": "conference",
        "posted-content": "preprint",
    }.get(raw_type)
    issued = message.get("issued") or message.get("published") or message.get("created")
    publication_date = parse_date(issued)
    year = publication_date.year if publication_date else None
    return SourceRecord(
        source="crossref",
        title=clean_text(message.get("title")),
        abstract=clean_text(message.get("abstract")),
        year=year,
        publication_date=publication_date,
        kind=kind,
        venue=venue,
        volume=clean_text(message.get("volume")),
        issue=clean_text(message.get("issue")),
        pages=clean_text(message.get("page")),
        doi=(message.get("DOI") or doi).lower(),
        url=message.get("URL"),
        authors=authors,
        raw=message,
    )
