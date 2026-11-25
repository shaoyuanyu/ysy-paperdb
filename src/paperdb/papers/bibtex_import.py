from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from paperdb.ingest.types import AuthorRecord, Candidate

_WS_RE = re.compile(r"\s+")
_ENTRY_TYPES = {
    "inproceedings": "conference",
    "conference": "conference",
    "article": "journal",
    "misc": "preprint",
    "unpublished": "preprint",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "incollection": "book_chapter",
    "techreport": "tech_report",
}


@dataclass(slots=True)
class BibEntry:
    key: str
    entry_type: str
    fields: dict[str, str] = field(default_factory=dict)

    def value(self, name: str) -> str | None:
        value = self.fields.get(name)
        return _WS_RE.sub(" ", value).strip() if value else None


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _read_braced(text: str, index: int) -> tuple[str, int]:
    # index points at '{'
    depth = 1
    start = index + 1
    index += 1
    while index < len(text) and depth:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    if depth:
        raise ValueError("unbalanced braces in BibTeX value")
    return text[start : index - 1], index


def _read_quoted(text: str, index: int) -> tuple[str, int]:
    index += 1
    value: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            value.append(text[index : index + 2])
            index += 2
            continue
        if char == '"':
            return "".join(value), index + 1
        value.append(char)
        index += 1
    raise ValueError("unterminated quoted BibTeX value")


def _read_bare(text: str, index: int) -> tuple[str, int]:
    start = index
    while index < len(text) and text[index] not in ",\n}":
        index += 1
    return text[start:index].strip(), index


def _parse_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    index = 0
    while True:
        index = _skip_ws(text, index)
        while index < len(text) and text[index] == ",":
            index = _skip_ws(text, index + 1)
        if index >= len(text) or text[index] == "}":
            break
        name_start = index
        while index < len(text) and (text[index].isalnum() or text[index] in "_-"):
            index += 1
        name = text[name_start:index].strip().lower()
        index = _skip_ws(text, index)
        if not name or index >= len(text) or text[index] != "=":
            # Skip malformed field to the next comma.
            while index < len(text) and text[index] not in ",\n}":
                index += 1
            continue
        index = _skip_ws(text, index + 1)
        pieces: list[str] = []
        while True:
            index = _skip_ws(text, index)
            if index >= len(text):
                break
            if text[index] == "{":
                value, index = _read_braced(text, index)
            elif text[index] == '"':
                value, index = _read_quoted(text, index)
            else:
                value, index = _read_bare(text, index)
            pieces.append(value)
            index = _skip_ws(text, index)
            if index < len(text) and text[index] == "#":
                index += 1
                continue
            break
        fields[name] = "".join(pieces)
        while index < len(text) and text[index] not in ",\n}":
            index += 1
    return fields


def parse_bibtex(text: str) -> list[BibEntry]:
    # Strip whole-line comments; keep '@' inside values.
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("%")]
    source = "\n".join(lines)
    entries: list[BibEntry] = []
    index = 0
    while True:
        at = source.find("@", index)
        if at < 0:
            break
        index = at + 1
        while index < len(source) and source[index].isspace():
            index += 1
        type_start = index
        while index < len(source) and (source[index].isalnum() or source[index] == "_"):
            index += 1
        entry_type = source[type_start:index].lower()
        index = _skip_ws(source, index)
        if index >= len(source) or source[index] not in "{(":
            continue
        opening = source[index]
        closing = "}" if opening == "{" else ")"
        # Read the whole entry with brace/paren nesting.
        depth = 1
        start = index + 1
        index += 1
        while index < len(source) and depth:
            char = source[index]
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
            index += 1
        if depth:
            raise ValueError(f"unterminated BibTeX entry @{entry_type}")
        body = source[start : index - 1]
        if entry_type in {"comment", "preamble", "string"}:
            continue
        comma = body.find(",")
        if comma < 0:
            key = body.strip()
            field_text = ""
        else:
            key = body[:comma].strip()
            field_text = body[comma + 1 :]
        entries.append(
            BibEntry(
                key=_WS_RE.sub("", key),
                entry_type=entry_type,
                fields=_parse_fields(field_text),
            )
        )
    return entries


def _clean_name(value: str) -> str:
    return value.replace("{", "").replace("}", "").strip()


def _clean_title(value: str) -> str:
    # BibTeX braces protect capitalization ("{AI} Control"); they are not part
    # of the displayed title.
    return _WS_RE.sub(" ", value.replace("{", "").replace("}", "")).strip()


def parse_authors(value: str | None) -> list[AuthorRecord]:
    if not value:
        return []
    parts = re.split(r"\s+and\s+", value, flags=re.IGNORECASE)
    authors: list[AuthorRecord] = []
    for part in parts:
        name = _clean_name(part)
        if not name or name.lower() == "others":
            continue
        if "," in name:
            family, given = [piece.strip() for piece in name.split(",", 1)]
        else:
            words = name.split()
            family = words[-1] if len(words) > 1 else ""
            given = " ".join(words[:-1]) if len(words) > 1 else name
        authors.append(
            AuthorRecord(
                name=name,
                family=family or None,
                given=given or None,
            )
        )
    return authors


def entry_to_candidate(entry: BibEntry) -> Candidate | None:
    title = _clean_title(entry.value("title") or "")
    if not title:
        return None
    kind = _ENTRY_TYPES.get(entry.entry_type, "preprint")
    venue = entry.value("booktitle") or entry.value("journal")
    year_raw = entry.value("year")
    year = None
    if year_raw:
        match = re.search(r"(19|20)\d{2}", year_raw)
        if match:
            year = int(match.group(0))
    if year is None:
        from datetime import date

        year = date.today().year

    identifiers: list[dict[str, str | bool]] = []
    doi = entry.value("doi")
    if doi:
        doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "").strip().lower()
        identifiers.append({"scheme": "doi", "value": doi, "isPrimary": True})
    eprint = entry.value("eprint") or entry.value("arxiv")
    archive = (entry.value("archiveprefix") or entry.value("archivePrefix") or "").lower()
    if eprint and (archive in {"", "arxiv"} or "arxiv" in eprint.lower()):
        arxiv = re.sub(r"^arxiv:", "", eprint, flags=re.IGNORECASE)
        arxiv = re.sub(r"v\d+$", "", arxiv)
        identifiers.append({"scheme": "arxiv", "value": arxiv, "isPrimary": not bool(doi)})
    pages = entry.value("pages")
    if pages:
        pages = pages.replace("--", "-")
    key = entry.key.lower()
    citekey = key if re.fullmatch(r"[a-z][a-z0-9]{0,63}", key) else None
    return Candidate(
        confidence=0.7,
        sources=["bibtex"],
        kind=kind,
        title=title,
        abstract=entry.value("abstract"),
        year=year,
        publication_date=None,
        volume=entry.value("volume"),
        issue=entry.value("number"),
        pages=pages,
        authors=parse_authors(entry.value("author")),
        identifiers=identifiers,
        venue=None,
        evaluation={"ccf": None, "cas": None},
        warnings=[],
        citekey=citekey,
        _venue_name=venue,
    )


def entry_as_json(entry: BibEntry) -> str:
    return json.dumps(
        {"key": entry.key, "type": entry.entry_type, "fields": entry.fields},
        ensure_ascii=False,
    )


def entry_from_json(raw: str) -> BibEntry:
    payload: dict[str, Any] = json.loads(raw)
    return BibEntry(
        key=str(payload.get("key") or ""),
        entry_type=str(payload.get("type") or "misc"),
        fields={str(key): str(value) for key, value in (payload.get("fields") or {}).items()},
    )
