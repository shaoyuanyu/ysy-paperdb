from __future__ import annotations

import uuid

import httpx
import pymupdf
import pytest

from paperdb.imports.pdf_import import inspect_pdf
from paperdb.ingest.types import AuthorRecord, Candidate

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


def _url_candidate(title: str, doi: str, pdf_urls: list[str]) -> Candidate:
    return Candidate(
        confidence=0.8,
        sources=["openalex", "page"],
        kind="preprint",
        title=title,
        abstract="An abstract from the landing page.",
        year=2026,
        publication_date=None,
        authors=[AuthorRecord(name="Alice Example", family="Example", given="Alice")],
        identifiers=[{"scheme": "doi", "value": doi, "isPrimary": True}],
        venue=None,
        evaluation={"ccf": None, "cas": None},
        warnings=[],
        pdf_urls=pdf_urls,
    )


async def test_import_pdf_from_url_with_page(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    title = f"URL Imported Paper {uuid.uuid4().hex[:8]}"
    doi = f"10.9999/{uuid.uuid4().hex}"
    pdf_data = _pdf(title)

    async def fake_resolve(session, raw, settings, *, client=None):
        return _url_candidate(title, doi, ["https://example.org/paper.pdf"]), []

    async def fake_fetch(http_client, url, settings):
        return pdf_data if url == "https://example.org/paper.pdf" else None

    monkeypatch.setattr("paperdb.imports.pdf_import.resolve_input", fake_resolve)
    monkeypatch.setattr("paperdb.imports.pdf_import._fetch_bytes", fake_fetch)

    response = await client.post(
        "/v1/imports/pdf-url",
        headers=SERVICE,
        json={"url": "https://example.org/landing/1"},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["created"] is True
    assert result["title"] == title
    assert result["fileId"]

    from paperdb.jobs.worker import run_once

    assert await run_once() is True
    paper = (await client.get(f"/v1/papers/{result['paperId']}", headers=SERVICE)).json()
    assert paper["title"] == title
    assert paper["kind"] == "preprint"
    files = (await client.get(f"/v1/papers/{result['paperId']}/files", headers=SERVICE)).json()
    assert len(files) == 1

    # re-importing the same URL links to the existing paper (no duplicate)
    response2 = await client.post(
        "/v1/imports/pdf-url",
        headers=SERVICE,
        json={"url": "https://example.org/landing/1"},
    )
    assert response2.status_code == 201, response2.text
    assert response2.json()["created"] is False
    assert response2.json()["paperId"] == result["paperId"]


async def test_import_pdf_from_url_no_pdf_metadata_only(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    title = f"Metadata Only Paper {uuid.uuid4().hex[:8]}"
    doi = f"10.9999/{uuid.uuid4().hex}"

    async def fake_resolve(session, raw, settings, *, client=None):
        return _url_candidate(title, doi, []), []

    async def fake_fetch(http_client, url, settings):
        return None

    monkeypatch.setattr("paperdb.imports.pdf_import.resolve_input", fake_resolve)
    monkeypatch.setattr("paperdb.imports.pdf_import._fetch_bytes", fake_fetch)

    response = await client.post(
        "/v1/imports/pdf-url",
        headers=SERVICE,
        json={"url": "https://example.org/landing/2"},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["created"] is True
    assert result["fileId"] is None
    assert any("no PDF" in warning for warning in result["warnings"])
    paper = (await client.get(f"/v1/papers/{result['paperId']}", headers=SERVICE)).json()
    assert paper["title"] == title


async def test_import_pdf_from_url_direct_pdf(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    title = f"Direct PDF Paper {uuid.uuid4().hex[:8]}"
    pdf_data = _pdf(title)

    async def no_lookup(session, raw, settings, *, client=None):
        return None, ["landing page unreachable"]

    async def fake_fetch(http_client, url, settings):
        return pdf_data

    monkeypatch.setattr("paperdb.imports.pdf_import.resolve_input", no_lookup)
    monkeypatch.setattr("paperdb.imports.pdf_import._fetch_bytes", fake_fetch)

    response = await client.post(
        "/v1/imports/pdf-url",
        headers=SERVICE,
        json={"url": "https://example.org/files/direct.pdf"},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["title"] == title
    assert result["fileId"]


async def test_import_pdf_from_url_invalid_url(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/imports/pdf-url",
        headers=SERVICE,
        json={"url": "ftp://example.org/paper.pdf"},
    )
    assert response.status_code == 422
