from __future__ import annotations

import re

import httpx

from paperdb.config import Settings
from paperdb.ingest.http import get_text
from paperdb.ingest.types import SourceRecord
from paperdb.papers.bibtex_import import parse_authors, parse_bibtex


async def fetch_acl(
    client: httpx.AsyncClient, acl_id: str, settings: Settings
) -> SourceRecord | None:
    text = await get_text(
        client,
        f"https://aclanthology.org/{acl_id}.bib",
        headers={"User-Agent": settings.arxiv_user_agent},
    )
    if not text:
        return None
    entries = parse_bibtex(text)
    if not entries:
        return None
    entry = entries[0]
    year_raw = entry.value("year") or ""
    match = re.search(r"(19|20)\d{2}", year_raw)
    year = int(match.group(0)) if match else None
    venue = entry.value("booktitle") or entry.value("journal")
    if not venue:
        venue = "ACL Findings" if ".findings-" in acl_id else "ACL"
    doi = entry.value("doi")
    if doi:
        doi = doi.replace("https://doi.org/", "").strip().lower()
    return SourceRecord(
        source="acl",
        title=entry.value("title"),
        abstract=entry.value("abstract"),
        year=year,
        kind="conference",
        venue=venue,
        pages=(entry.value("pages") or "").replace("--", "-") or None,
        doi=doi,
        acl_id=acl_id,
        url=f"https://aclanthology.org/{acl_id}/",
        authors=parse_authors(entry.value("author")),
        raw={"acl_id": acl_id},
    )
