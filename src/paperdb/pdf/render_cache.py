"""Bounded in-process cache for rendered PDF pages.

A page image is a pure function of (content sha256, page number, dpi) and the
content is addressed by its own hash, so a cache key can never go stale. What
it saves is the expensive part: PyMuPDF re-parsing the whole file to rasterize
one page. Every client benefits — the MCP reader, the web UI, and any script
hitting `/v1/files/{id}/pages/{page}/image` twice.

Set ``PAPERDB_RENDER_CACHE_MB=0`` to disable it.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict

RENDER_CACHE_MB_ENV = "PAPERDB_RENDER_CACHE_MB"
DEFAULT_RENDER_CACHE_MB = 64

Key = tuple[str, int, int]

_lock = threading.Lock()
_entries: OrderedDict[Key, bytes] = OrderedDict()
_bytes = 0


def limit_bytes() -> int:
    raw = os.environ.get(RENDER_CACHE_MB_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_RENDER_CACHE_MB * 1024 * 1024
    try:
        megabytes = max(0, int(raw))
    except ValueError:
        return DEFAULT_RENDER_CACHE_MB * 1024 * 1024
    return megabytes * 1024 * 1024


def get(key: Key) -> bytes | None:
    global _bytes
    with _lock:
        png = _entries.get(key)
        if png is None:
            return None
        _entries.move_to_end(key)
        return png


def put(key: Key, png: bytes) -> None:
    global _bytes
    limit = limit_bytes()
    if limit <= 0 or len(png) > limit:
        return
    with _lock:
        existing = _entries.pop(key, None)
        if existing is not None:
            _bytes -= len(existing)
        _entries[key] = png
        _bytes += len(png)
        while _bytes > limit and _entries:
            _, evicted = _entries.popitem(last=False)
            _bytes -= len(evicted)


def clear() -> None:
    global _bytes
    with _lock:
        _entries.clear()
        _bytes = 0


def stats() -> dict[str, int]:
    with _lock:
        return {"entries": len(_entries), "bytes": _bytes, "limit": limit_bytes()}
