"""Local disk cache for rendered pages, used by the MCP reader.

The MCP process re-fetches a page image every time a model asks for it, which
is both slow and — for a multi-megabyte PNG — paid again on the wire. A page of
a given PDF never changes (file ids are content-addressed), so the rendered PNG
is cached by (file id, page, dpi) under a size-capped directory. Writes are
atomic, and the oldest entries are pruned to stay under the cap.

Configuration:
    PAPERDB_MCP_CACHE_DIR   cache root (default ~/.cache/paperdb/mcp)
    PAPERDB_MCP_CACHE_MB    size cap in MiB (default 256, 0 disables the cache)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

CACHE_DIR_ENV = "PAPERDB_MCP_CACHE_DIR"
CACHE_MB_ENV = "PAPERDB_MCP_CACHE_MB"
DEFAULT_CACHE_MB = 256


def cache_root() -> Path:
    raw = os.environ.get(CACHE_DIR_ENV)
    return Path(raw).expanduser() if raw else Path.home() / ".cache" / "paperdb" / "mcp"


def limit_bytes() -> int:
    raw = os.environ.get(CACHE_MB_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_CACHE_MB * 1024 * 1024
    try:
        return max(0, int(raw)) * 1024 * 1024
    except ValueError:
        return DEFAULT_CACHE_MB * 1024 * 1024


def page_path(file_id: str, page: int, dpi: int) -> Path:
    return cache_root() / "pages" / file_id / f"{page}-{dpi}.png"


def read(file_id: str, page: int, dpi: int) -> bytes | None:
    if limit_bytes() <= 0:
        return None
    path = page_path(file_id, page, dpi)
    try:
        return path.read_bytes()
    except OSError:
        return None


def write(file_id: str, page: int, dpi: int, png: bytes) -> None:
    """Store a page PNG and keep the cache under its size cap."""
    limit = limit_bytes()
    if limit <= 0:
        return
    path = page_path(file_id, page, dpi)
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(png)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        return
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    _prune(limit)


def _prune(limit: int) -> None:
    root = cache_root() / "pages"
    files: list[tuple[float, int, Path]] = []
    total = 0
    try:
        for path in root.rglob("*.png"):
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
    except OSError:
        return
    if total <= limit:
        return
    for _, size, path in sorted(files):
        if total <= limit:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            continue
