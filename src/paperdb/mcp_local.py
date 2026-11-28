"""Which local files this MCP server is allowed to read.

``upload_pdf`` / ``import_pdf_file`` read a path on the host, so the process's
own privileges bound what a remote model can exfiltrate. Instead of "any
absolute path", reads are confined to a small set of roots and must be PDFs:

* ``~/Downloads``, ``~/Desktop``, ``~/Documents``, the OS temp directory
  (``/tmp``, or ``/var/folders/...`` on macOS)
* anything listed in ``PAPERDB_MCP_LOCAL_ROOTS`` (``os.pathsep``-separated)

The resolved path (symlinks followed) must live under one of those roots, so a
symlink cannot be used to escape. Everything else is refused with an error that
names the roots and the variable that extends them.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

LOCAL_ROOTS_ENV = "PAPERDB_MCP_LOCAL_ROOTS"
MAX_LOCAL_PDF_BYTES = 200 * 1024 * 1024


def default_roots() -> tuple[Path, ...]:
    return (
        Path.home() / "Downloads",
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path(tempfile.gettempdir()),
    )


def allowed_roots() -> tuple[Path, ...]:
    """Defaults plus ``PAPERDB_MCP_LOCAL_ROOTS``, resolved and de-duplicated."""
    roots = list(default_roots())
    for part in os.environ.get(LOCAL_ROOTS_ENV, "").split(os.pathsep):
        if part.strip():
            roots.append(Path(part.strip()).expanduser())
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:  # pragma: no cover - a broken root cannot be used anyway
            continue
    return tuple(dict.fromkeys(resolved))


def is_allowed(path: Path) -> bool:
    try:
        candidate = path.resolve()
    except OSError:  # pragma: no cover - resolve only fails on bad inputs
        return False
    return any(candidate.is_relative_to(root) for root in allowed_roots())


def read_pdf(path: str) -> tuple[bytes, str]:
    """Read a PDF from an allowed local root.

    Validating here turns "wrong path / outside the allowed roots / not a PDF /
    too large" into an immediate, readable error instead of an opaque upload
    failure.
    """
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ValueError(f"path must be absolute: {path}")
    try:
        candidate = raw.resolve()
    except OSError as exc:  # pragma: no cover - rare, but keep the message clean
        raise ValueError(f"cannot resolve {path}: {exc}") from exc
    roots = allowed_roots()
    if not any(candidate.is_relative_to(root) for root in roots):
        raise ValueError(
            f"path is outside the allowed local roots: {candidate}. "
            f"Allowed: {', '.join(str(root) for root in roots)}. "
            f"Extend with {LOCAL_ROOTS_ENV}=<dir>{os.pathsep}<dir>."
        )
    if not candidate.is_file():
        raise ValueError(f"not a file: {candidate}")
    data = candidate.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ValueError(f"not a PDF (missing %PDF- header): {candidate}")
    if len(data) > MAX_LOCAL_PDF_BYTES:
        raise ValueError(f"PDF is {len(data)} bytes, over the {MAX_LOCAL_PDF_BYTES} byte limit")
    return data, candidate.name
