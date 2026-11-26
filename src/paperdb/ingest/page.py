"""Landing-page ingestion: fetch an arbitrary paper page and extract metadata.

Conference/journal paper pages form a long tail of templates. The extraction
strategy is deliberately layered, cheapest evidence first:

1. HighWire ``citation_*`` meta tags (used by most publishers: ACM DL, IEEE
   Xplore, Springer, Elsevier, Usenix, ...);
2. OpenGraph tags (``og:title`` / ``og:site_name`` / ``og:description``) with
   conservative site-suffix stripping;
3. ``<title>`` / ``<h1>`` fallbacks;
4. raw-text DOI / arXiv-ID regexes and ``.pdf`` link harvesting.

Anything this cannot find is expected to come from the aggregator sources
(Crossref/OpenAlex/DBLP/...) after the title is known. The page itself is the
*identification* bridge, not the metadata authority.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from paperdb.config import Settings
from paperdb.ingest.http import get_text

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
_ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_WS_RE = re.compile(r"\s+")
# Suffix separators used by og:title/<title> ("Paper - NDSS Symposium").
_TITLE_SEPARATORS = (" - ", " | ", " · ", " :: ", " — ")

# Some publisher pages carry no citation_conference tag and fall back to
# og:site_name ("USENIX"), which is not the venue's name. Their URL carries the
# venue, so map the common families explicitly; citation_* tags still win.
_URL_VENUE_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"usenix\.org/conference/usenixsecurity\d{0,2}", re.IGNORECASE),
        "USENIX Security Symposium",
    ),
    (
        re.compile(r"usenix\.org/conference/nsdi\d{0,2}", re.IGNORECASE),
        "USENIX Symposium on Networked Systems Design and Implementation",
    ),
    (
        re.compile(r"usenix\.org/conference/osdi\d{0,2}", re.IGNORECASE),
        "USENIX Symposium on Operating Systems Design and Implementation",
    ),
    (
        re.compile(r"usenix\.org/conference/atc\d{0,2}", re.IGNORECASE),
        "USENIX Annual Technical Conference",
    ),
    (
        re.compile(r"ndss-symposium\.org", re.IGNORECASE),
        "Network and Distributed System Security Symposium",
    ),
)


def venue_from_url(url: str) -> str | None:
    """Best-effort venue name encoded in a well-known publisher URL."""
    for pattern, name in _URL_VENUE_HINTS:
        if pattern.search(url):
            return name
    return None


# og:site_name values that identify the publisher, not the venue.
_BRAND_ONLY_HINTS = {"usenix", "usenix.org", "www.usenix.org", "usenix association"}


@dataclass(slots=True)
class PageMetadata:
    url: str
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv: str | None = None
    abstract: str | None = None
    venue_hint: str | None = None
    pdf_urls: list[str] = field(default_factory=list)


def _clean(value: str | None) -> str | None:
    text = _WS_RE.sub(" ", value or "").strip()
    return text or None


def _clean_doi(value: str | None) -> str | None:
    if not value:
        return None
    doi = value.strip().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    doi = doi.rstrip(".,;)]}>").lower()
    return doi or None


class _PageParser(HTMLParser):
    """Collect meta tags, <title>, <h1>, and anchor hrefs in one pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.meta_list: list[tuple[str, str]] = []  # repeatable keys (citation_author)
        self.title: str | None = None
        self.h1: str | None = None
        self.hrefs: list[str] = []
        self._title_buf: list[str] = []
        self._h1_buf: list[str] = []
        self._in_title = False
        self._in_h1 = False
        self._h1_done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        attributes = {key: (value or "") for key, value in attrs}
        if lower == "meta":
            key = attributes.get("name") or attributes.get("property") or attributes.get("itemprop")
            content = attributes.get("content")
            if key and content is not None:
                normalized = key.strip().lower()
                self.meta.setdefault(normalized, content)
                self.meta_list.append((normalized, content))
        elif lower == "title":
            self._in_title = True
        elif lower == "h1" and not self._h1_done:
            self._in_h1 = True
        elif lower == "a":
            href = attributes.get("href")
            if href:
                self.hrefs.append(href)
        elif lower == "link":
            # <link rel="cite-as-pdf" href="..."> and similar publisher variants
            rel = attributes.get("rel", "").lower()
            if "pdf" in rel and attributes.get("href"):
                self.hrefs.append(attributes["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
            self.title = _clean("".join(self._title_buf))
        elif tag.lower() == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_done = True
            self.h1 = _clean("".join(self._h1_buf))

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
        elif self._in_h1:
            self._h1_buf.append(data)

    def all(self, key: str) -> list[str]:
        return [value for name, value in self.meta_list if name == key]


def _strip_site_suffix(title: str, site: str | None) -> str:
    """Drop a trailing ' - Site' / ' | Site' decoration from a page title."""
    cleaned = title.strip()
    if site:
        tails = (f" - {site}", f" | {site}", f" · {site}", f" — {site}", f" – {site}")  # noqa: RUF001
        for tail in tails:
            if cleaned.lower().endswith(tail.lower()):
                cleaned = cleaned[: -len(tail)]
                return cleaned.strip()
    # No site name: strip a short trailing segment after a separator. Real
    # paper titles dominate in length over site suffixes like "- IEEE Xplore".
    for separator in _TITLE_SEPARATORS:
        if separator not in cleaned:
            continue
        parts = [part.strip() for part in cleaned.split(separator)]
        longest_prefix = max(len(part) for part in parts[:-1])
        if len(parts) >= 2 and len(parts[-1]) <= 30 and longest_prefix > len(parts[-1]):
            cleaned = max(parts[:-1], key=len)
            break
    return cleaned


def _looks_like_pdf(href: str) -> bool:
    path = urlsplit(href).path.lower()
    return path.endswith(".pdf") or path.endswith(".pdf/")


def extract_page(html: str, url: str) -> PageMetadata:
    parser = _PageParser()
    with contextlib.suppress(Exception):  # malformed HTML: keep what was collected
        parser.feed(html)

    meta = parser.meta
    site = _clean(meta.get("og:site_name"))

    # --- Title: citation_title > og:title (suffix-stripped) > <title> > <h1>
    title = _clean(meta.get("citation_title"))
    if not title:
        og_title = _clean(meta.get("og:title"))
        if og_title:
            title = _strip_site_suffix(og_title, site)
    if not title and parser.title:
        title = _strip_site_suffix(parser.title, site)
    if not title:
        title = parser.h1

    # --- Authors (repeatable citation_author tags)
    authors: list[str] = []
    for value in parser.all("citation_author"):
        name = _clean(value)
        if name and name.lower() not in {"unknown", "et al."}:
            authors.append(name)
    if not authors:
        inline = _clean(meta.get("citation_authors"))
        if inline:
            authors = [part.strip() for part in inline.split(";") if part.strip()]

    # --- Identifiers
    doi = _clean_doi(meta.get("citation_doi") or meta.get("dc.identifier"))
    identifier = meta.get("citation_identifier")
    if not doi and identifier and identifier.lower().startswith("doi:"):
        doi = _clean_doi(identifier[4:])
    arxiv = _clean(meta.get("citation_arxiv_id"))
    if not arxiv:
        text_pool = f"{url} {' '.join(parser.hrefs)}"
        match = _ARXIV_URL_RE.search(text_pool)
        if match:
            arxiv = match.group(1)
    if not doi:
        # Last resort: search the raw HTML. First hit wins; DOIs are rare
        # enough in markup that false positives are unlikely to dominate.
        match = _DOI_RE.search(html)
        if match:
            candidate = _clean_doi(match.group(0))
            if candidate and not candidate.startswith("10.0000/"):
                doi = candidate

    # --- Year
    year = None
    for key in ("citation_publication_date", "citation_date", "dc.date", "og:updated_time"):
        raw = meta.get(key) or ""
        match = _YEAR_RE.search(raw)
        if match:
            year = int(match.group(1))
            break

    # --- Abstract
    abstract = _clean(
        meta.get("citation_abstract") or meta.get("description") or meta.get("og:description")
    )
    if abstract and len(abstract) < 80:
        abstract = None  # og:description is often a site blurb, not a paper abstract

    # --- Venue hint
    venue_hint = _clean(
        meta.get("citation_conference")
        or meta.get("citation_journal_title")
        or meta.get("citation_journal")
        or site
    )
    url_hint = venue_from_url(url)
    if url_hint and (venue_hint is None or venue_hint.lower() in _BRAND_ONLY_HINTS):
        # og:site_name is often just the publisher brand ("USENIX"); the URL
        # knows the actual venue.
        venue_hint = url_hint

    # --- PDF links: citation_pdf_url first, then harvested .pdf anchors
    pdf_urls: list[str] = []
    citation_pdf_url = meta.get("citation_pdf_url")
    for href in [citation_pdf_url, *parser.hrefs]:
        if not href:
            continue
        absolute = urljoin(url, href.strip())
        is_pdf = href == citation_pdf_url or _looks_like_pdf(absolute)
        if is_pdf and absolute not in pdf_urls and absolute.startswith("http"):
            pdf_urls.append(absolute)
        if len(pdf_urls) >= 5:
            break

    return PageMetadata(
        url=url,
        title=title,
        authors=authors,
        year=year,
        doi=doi,
        arxiv=arxiv,
        abstract=abstract,
        venue_hint=venue_hint,
        pdf_urls=pdf_urls,
    )


async def fetch_page(
    client: httpx.AsyncClient, url: str, settings: Settings
) -> PageMetadata | None:
    """Fetch a landing page and extract its metadata. None if unreachable."""
    if not url.startswith(("http://", "https://")):
        return None
    text = await get_text(
        client,
        url,
        headers={
            "User-Agent": settings.arxiv_user_agent,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    if not text or "<html" not in text.lower():
        return None
    return extract_page(text, url)


def page_source_record(page: PageMetadata) -> dict[str, Any] | None:
    """Raw payload describing the page, consumed when building a Candidate."""
    return {
        "url": page.url,
        "pdf_urls": page.pdf_urls,
        "venue_hint": page.venue_hint,
    }
