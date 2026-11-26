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
