"""Local read confinement and the MCP page cache.

The MCP server runs with the owner's privileges, so the interesting question is
not whether it *can* read a path but what it is willing to read. These tests pin
that boundary (PDFs under the allowed roots only), and that a rendered page is
served from cache instead of being fetched twice.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from paperdb import mcp_local, page_cache


def _pdf(path: Path, body: bytes = b"%PDF-1.7 test") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_default_roots_cover_the_usual_places() -> None:
    roots = mcp_local.allowed_roots()
    names = {root.name for root in roots}
    assert {"Downloads", "Desktop", "Documents"} <= names
    import tempfile

    assert Path(tempfile.gettempdir()).resolve() in roots


def test_env_extends_the_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = tmp_path / "papers"
    other = tmp_path / "other"
    extra.mkdir()
    other.mkdir()
    monkeypatch.setenv(mcp_local.LOCAL_ROOTS_ENV, os.pathsep.join([str(extra), str(other)]))

    inside = _pdf(extra / "a.pdf")
    data, filename = mcp_local.read_pdf(str(inside))
    assert data.startswith(b"%PDF-") and filename == "a.pdf"

    # The second root is honoured too, not just the first.
    assert mcp_local.read_pdf(str(_pdf(other / "b.pdf")))[1] == "b.pdf"


def test_paths_outside_the_roots_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(mcp_local, "default_roots", lambda: ())
    monkeypatch.setenv(mcp_local.LOCAL_ROOTS_ENV, str(allowed))
    outside = _pdf(tmp_path / "elsewhere" / "private.pdf")
    with pytest.raises(ValueError, match="outside the allowed local roots"):
        mcp_local.read_pdf(str(outside))


def test_a_symlink_cannot_escape_a_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(mcp_local, "default_roots", lambda: ())
    monkeypatch.setenv(mcp_local.LOCAL_ROOTS_ENV, str(allowed))
    secret = _pdf(tmp_path / "secret.pdf")
    link = allowed / "link.pdf"
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="outside the allowed local roots"):
        mcp_local.read_pdf(str(link))


def test_relative_paths_and_non_pdfs_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(mcp_local.LOCAL_ROOTS_ENV, str(tmp_path))
    with pytest.raises(ValueError, match="absolute"):
        mcp_local.read_pdf("relative.pdf")
    not_pdf = tmp_path / "notes.txt"
    not_pdf.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="not a PDF"):
        mcp_local.read_pdf(str(not_pdf))
    with pytest.raises(ValueError, match="not a file"):
        mcp_local.read_pdf(str(tmp_path / "missing.pdf"))


def test_oversized_pdfs_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mcp_local.LOCAL_ROOTS_ENV, str(tmp_path))
    monkeypatch.setattr(mcp_local, "MAX_LOCAL_PDF_BYTES", 8)
    with pytest.raises(ValueError, match="over the 8 byte limit"):
        mcp_local.read_pdf(str(_pdf(tmp_path / "big.pdf")))


def test_page_cache_roundtrip_and_disable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(page_cache.CACHE_DIR_ENV, str(tmp_path / "cache"))
    assert page_cache.read("file-1", 3, 120) is None
    page_cache.write("file-1", 3, 120, b"\x89PNG fake")
    assert page_cache.read("file-1", 3, 120) == b"\x89PNG fake"
    # A different dpi is a different page image.
    assert page_cache.read("file-1", 3, 144) is None

    monkeypatch.setenv(page_cache.CACHE_MB_ENV, "0")
    assert page_cache.read("file-1", 3, 120) is None
    page_cache.write("file-1", 3, 120, b"ignored")
    assert page_cache.read("file-1", 3, 120) is None


def test_page_cache_prunes_the_oldest_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(page_cache.CACHE_DIR_ENV, str(tmp_path / "cache"))
    monkeypatch.setenv(page_cache.CACHE_MB_ENV, "1")
    monkeypatch.setattr(page_cache, "limit_bytes", lambda: 300)

    page_cache.write("file-1", 1, 120, b"a" * 200)
    page_cache.write("file-1", 2, 120, b"b" * 200)
    # Writing a third page pushes the total past 300 bytes: the oldest goes.
    page_cache.write("file-1", 3, 120, b"c" * 200)
    remaining = [page_cache.read("file-1", page, 120) for page in (1, 2, 3)]
    assert remaining.count(None) >= 1
    assert remaining[-1] == b"c" * 200


async def test_get_page_image_is_fetched_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from paperdb import mcp_server

    monkeypatch.setenv(page_cache.CACHE_DIR_ENV, str(tmp_path / "cache"))
    calls: list[str] = []

    async def fake_raw(method: str, path: str, **kwargs: object) -> object:
        calls.append(path)
        return type("R", (), {"content": b"\x89PNG rendered"})()

    monkeypatch.setattr(mcp_server, "_raw_request", fake_raw)
    first = await mcp_server.get_page_image("file-xyz", 2)
    second = await mcp_server.get_page_image("file-xyz", 2)
    assert calls == ["/v1/files/file-xyz/pages/2/image"]
    assert first.data == second.data == b"\x89PNG rendered"
