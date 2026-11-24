from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx

from paperdb.config import Settings
from paperdb.ingest.crossref import clean_text
from paperdb.ingest.http import get_text
from paperdb.ingest.types import AuthorRecord, SourceRecord

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)


async def fetch_dblp_record(
    client: httpx.AsyncClient, key: str, settings: Settings
) -> SourceRecord | None:
    xml = await get_text(
        client,
        f"https://dblp.org/rec/{key}.xml",
        headers={"User-Agent": settings.arxiv_user_agent},
    )
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    info = root.find("info") if root.tag == "info" else root.find(".//info")
    if info is None:
        return None
    title = (clean_text(info.findtext("title")) or "").rstrip(".") or None
    if not title:
        return None
    authors: list[AuthorRecord] = []
    for node in info.findall("authors/author"):
        name = clean_text(node.text)
        if not name:
            continue
        parts = name.split()
        authors.append(
            AuthorRecord(
                name=name,
                family=parts[-1] if len(parts) > 1 else None,
                given=" ".join(parts[:-1]) if len(parts) > 1 else None,
                dblp_pid=node.get("pid"),
            )
        )
    ee = info.findtext("ee")
    doi = None
    if ee:
        match = _DOI_RE.search(ee)
        if match:
            doi = match.group(0).lower()
    year_text = info.findtext("year")
    try:
        year = int(year_text) if year_text else None
    except ValueError:
        year = None
    kind = (
        "conference"
        if key.startswith("conf/")
        else "journal"
        if key.startswith("journals/")
        else None
    )
    venue = clean_text(info.findtext("venue"))
    series_key = "/".join(key.split("/")[:2]) if kind else None
    return SourceRecord(
        source="dblp",
        title=title,
        year=year,
        kind=kind,
        venue=venue,
        dblp_venue_key=series_key,
        dblp_key=key,
        doi=doi,
        url=ee or f"https://dblp.org/rec/{key}",
        authors=authors,
        raw={"key": key},
    )
