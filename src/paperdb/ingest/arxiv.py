from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

import httpx

from paperdb.config import Settings
from paperdb.ingest.http import get_text
from paperdb.ingest.types import AuthorRecord, SourceRecord

_WS_RE = re.compile(r"\s+")
_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _text(node: ET.Element, path: str) -> str | None:
    value = node.findtext(path, namespaces=_ATOM)
    if value is None:
        return None
    value = _WS_RE.sub(" ", value).strip()
    return value or None


async def fetch_arxiv(
    client: httpx.AsyncClient, arxiv_id: str, settings: Settings
) -> SourceRecord | None:
    xml = await get_text(
        client,
        "https://export.arxiv.org/api/query",
        params={"id_list": arxiv_id, "max_results": 1},
        headers={"User-Agent": settings.arxiv_user_agent},
    )
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    entry = root.find("a:entry", _ATOM)
    if entry is None:
        return None
    title = _text(entry, "a:title")
    if not title:
        return None
    authors: list[AuthorRecord] = []
    for author in entry.findall("a:author", _ATOM):
        name = _text(author, "a:name")
        if not name:
            continue
        parts = name.split()
        authors.append(
            AuthorRecord(
                name=name,
                family=parts[-1] if len(parts) > 1 else None,
                given=" ".join(parts[:-1]) if len(parts) > 1 else None,
            )
        )
    published = _text(entry, "a:published")
    publication_date = None
    if published:
        try:
            publication_date = date.fromisoformat(published[:10])
        except ValueError:
            publication_date = None
    doi = _text(entry, "arxiv:doi")
    journal_ref = _text(entry, "arxiv:journal_ref")
    entry_id = _text(entry, "a:id") or ""
    arxiv_from_url = entry_id.rstrip("/").rsplit("/", 1)[-1]
    arxiv_from_url = re.sub(r"v\d+$", "", arxiv_from_url)
    return SourceRecord(
        source="arxiv",
        title=title,
        abstract=_text(entry, "a:summary"),
        year=publication_date.year if publication_date else None,
        publication_date=publication_date,
        kind="preprint",
        venue=journal_ref,
        arxiv=arxiv_from_url or arxiv_id,
        doi=doi.lower() if doi else None,
        url=entry_id or None,
        authors=authors,
        raw={"id": entry_id, "journal_ref": journal_ref},
    )
