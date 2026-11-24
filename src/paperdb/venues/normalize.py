from __future__ import annotations

import re
import unicodedata

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_ORDINAL_RE = re.compile(r"\b\d+(?:st|nd|rd|th)\b")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(value: str) -> str:
    """Deterministic venue/journal name key (mirrors the site's normName)."""
    text = unicodedata.normalize("NFKC", value).lower().strip()
    if text.startswith("the "):
        text = text[4:]
    text = text.replace("&", " and ")
    text = _YEAR_RE.sub(" ", text)
    text = _ORDINAL_RE.sub(" ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    return " ".join(text.split())


def normalize_issn(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", value).upper()


def parse_issn_pair(value: str) -> tuple[str | None, str | None]:
    """'1553-877X/1553-877X' -> (print, online); single value fills print."""
    parts = [part for part in value.split("/") if part.strip()]
    normalized = [normalize_issn(part) for part in parts]
    normalized = [part for part in normalized if part]
    if not normalized:
        return None, None
    if len(normalized) == 1:
        return normalized[0], None
    return normalized[0], normalized[1]


def dblp_series_key(url: str | None) -> str | None:
    """Extract conf/asplos or journals/pami from a DBLP db URL."""
    if not url:
        return None
    match = re.search(r"dblp\.org/db/(conf|journals)/([^/]+)/?", url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def ccf_field_key(value: str | None) -> str | None:
    if not value:
        return None
    return value.split("/")[0].strip() or None
