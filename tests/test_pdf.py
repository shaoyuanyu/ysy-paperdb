from __future__ import annotations

import uuid

import httpx
import pymupdf

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


def _pdf(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    data = document.tobytes()
    document.close()
    return data


async def _paper(client: httpx.AsyncClient) -> dict:
    response = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "preprint",
            "title": f"PDF Test {uuid.uuid4().hex[:8]}",
            "year": 2026,
            "identifiers": [{"scheme": "arxiv", "value": f"2601.{uuid.uuid4().int % 100000:05d}"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_pdf_upload_extract_search_annotate(client: httpx.AsyncClient) -> None:
    paper = await _paper(client)
    text = "Speculative decoding accelerates retrieval augmented generation."
    response = await client.post(
        f"/v1/papers/{paper['id']}/files",
        headers=SERVICE,
        files={"file": ("paper.pdf", _pdf(text), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    uploaded = response.json()
    file_id = uploaded["file"]["id"]
    job_id = uploaded["jobId"]
    assert uploaded["file"]["isPrimary"] is True

    from paperdb.jobs.worker import run_once

    assert await run_once() is True
    job = (await client.get(f"/v1/jobs/{job_id}", headers=SERVICE)).json()
    assert job["status"] == "succeeded", job
    file = (await client.get(f"/v1/files/{file_id}", headers=SERVICE)).json()
    assert file["textStatus"] == "extracted"
    assert file["pageCount"] == 1

    hits = await client.get(
        f"/v1/files/{file_id}/search", params={"q": "speculative decoding"}, headers=SERVICE
    )
    assert hits.status_code == 200, hits.text
    assert hits.json()["hits"][0]["page"] == 1
    assert "speculative" in hits.json()["hits"][0]["snippet"].lower()

    global_hits = await client.get(
        "/v1/search/fulltext", params={"q": "retrieval augmented"}, headers=SERVICE
    )
    assert global_hits.status_code == 200
    assert any(hit["fileId"] == file_id for hit in global_hits.json()["hits"])

    ranged = await client.get(
        f"/v1/files/{file_id}/content", headers={**SERVICE, "Range": "bytes=0-9"}
    )
    assert ranged.status_code == 206
    assert ranged.content.startswith(b"%PDF-")
    assert ranged.headers["content-range"].startswith("bytes 0-9/")

    annotation = await client.post(
        f"/v1/papers/{paper['id']}/annotations",
        headers=SERVICE,
        json={
            "pdfFileId": file_id,
            "kind": "highlight",
            "page": 1,
            "rects": [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.02}],
            "quote": "Speculative decoding",
            "comment": "Very relevant",
            "color": "yellow",
        },
    )
    assert annotation.status_code == 201, annotation.text
    annotation_id = annotation.json()["id"]
    listed = await client.get(f"/v1/papers/{paper['id']}/annotations", headers=SERVICE)
    assert len(listed.json()) == 1
    patched = await client.patch(
        f"/v1/annotations/{annotation_id}",
        headers=SERVICE,
        json={"color": "green", "comment": "Edited"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["color"] == "green"

    detail = (await client.get(f"/v1/papers/{paper['id']}", headers=SERVICE)).json()
    assert detail["counts"]["pdfFiles"] == 1
    assert detail["counts"]["annotations"] == 1

    deleted = await client.delete(f"/v1/annotations/{annotation_id}", headers=SERVICE)
    assert deleted.status_code == 204
    assert (await client.get(f"/v1/papers/{paper['id']}/annotations", headers=SERVICE)).json() == []


def test_clean_text_strips_nul_bytes() -> None:
    """A single 0x00 used to abort every extract_pdf job on Postgres."""
    from paperdb.pdf.extract import clean_text

    assert clean_text("before\x00after") == "beforeafter"
    assert clean_text("no nul") == "no nul"
