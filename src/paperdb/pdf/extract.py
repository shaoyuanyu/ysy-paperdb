from __future__ import annotations

import re

import pymupdf

# Postgres text columns reject NUL bytes, and PyMuPDF happily emits them for
# some embedded fonts; a single 0x00 used to abort the whole extract job.
_NUL_RE = re.compile("\x00")


def clean_text(text: str) -> str:
    return _NUL_RE.sub("", text)


def extract_pdf(data: bytes) -> tuple[int, list[str], str]:
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        if document.needs_pass:
            raise ValueError("encrypted PDF")
        pages = [clean_text(str(page.get_text("text") or "")) for page in document]
        sample = "".join(pages)[:4000]
        non_ascii = sum(1 for char in sample if ord(char) > 127)
        language = "zh" if non_ascii > len(sample) * 0.2 else "en"
        return len(pages), pages, language
    finally:
        document.close()


def extract_outline(data: bytes) -> list[dict[str, object]]:
    """The PDF's bookmark tree as sections, when it has one.

    Papers ship a table of contents far less often than books do; an empty list
    is a normal answer, not an error. When present it lets a reader jump to a
    section instead of pulling the whole paper.
    """
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        if document.needs_pass:
            raise ValueError("encrypted PDF")
        return [
            {"level": level, "title": title.strip(), "page": page}
            for level, title, page in document.get_toc(simple=True)
            if title and title.strip()
        ]
    finally:
        document.close()


def render_page_png(data: bytes, page: int, dpi: int = 144) -> bytes:
    """Render one page to PNG.

    Text extraction loses figures, tables and layout; an image is how an agent
    reads the parts of a paper a human sees rather than reads.
    """
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        if document.needs_pass:
            raise ValueError("encrypted PDF")
        if page < 1 or page > document.page_count:
            raise ValueError(f"page {page} outside 1..{document.page_count}")
        return document[page - 1].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        document.close()


_SNIPPET_WS = re.compile(r"\s+")


def snippet(text: str, query: str, radius: int = 120) -> str:
    cleaned = _SNIPPET_WS.sub(" ", text).strip()
    lowered = cleaned.lower()
    needle = query.lower().strip()
    index = lowered.find(needle) if needle else -1
    if index < 0:
        return cleaned[: radius * 2]
    start = max(index - radius, 0)
    end = min(index + len(needle) + radius, len(cleaned))
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(cleaned) else ""
    return f"{prefix}{cleaned[start:end]}{suffix}"
