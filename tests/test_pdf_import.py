from __future__ import annotations

import uuid

import httpx
import pymupdf
import pytest

from paperdb.imports.pdf_import import inspect_pdf

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


def _pdf(title: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Auto PDF Import Test\n\nBody text for extraction.", fontsize=12)
    document.set_metadata(
        {
            "title": title,
            "author": "Alice Example; Bob Example",
            "creationDate": "D:20240101120000Z",
        }
    )
    data = document.tobytes()
    document.close()
    return data


def test_inspect_pdf_metadata() -> None:
    meta = inspect_pdf(_pdf("A PDF Metadata Title"), "paper.pdf")
    assert meta.title == "A PDF Metadata Title"
    assert meta.authors == ["Alice Example", "Bob Example"]
    assert meta.year == 2024


async def test_pdf_auto_import(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_lookup(session, raw, settings, **kwargs):
        return None, ["no external record"]

    monkeypatch.setattr("paperdb.imports.pdf_import.resolve_input", no_lookup)
    title = f"Auto Imported PDF {uuid.uuid4().hex[:8]}"
    response = await client.post(
        "/v1/imports/pdf",
        headers=SERVICE,
        files={"file": ("paper.pdf", _pdf(title), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["created"] is True
    assert result["title"] == title
    assert result["jobId"]

    from paperdb.jobs.worker import run_once

    assert await run_once() is True
    file = (await client.get(f"/v1/files/{result['fileId']}", headers=SERVICE)).json()
    assert file["textStatus"] == "extracted"
    assert file["pageCount"] == 1
    paper = (await client.get(f"/v1/papers/{result['paperId']}", headers=SERVICE)).json()
    assert paper["title"] == title
    assert paper["authors"][0]["displayName"] == "Alice Example"


async def test_pdf_import_rejects_non_pdf(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/imports/pdf",
        headers=SERVICE,
        files={"file": ("paper.pdf", b"not a pdf", "application/pdf")},
    )
    assert response.status_code == 415
