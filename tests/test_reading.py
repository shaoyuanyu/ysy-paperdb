"""Reading surface: page-range text, OCR detection, scoped full-text search.

These cover the agent's read path end to end: the batch page-text endpoint,
the ``ocr_needed`` status, and the scoping rules that let a research-workspace
paper be searched and read without being promoted into the library.
"""

from __future__ import annotations

import uuid

import httpx
import pymupdf

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


def _pdf(pages: list[str]) -> bytes:
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        if text:
            # A textbox wraps, so the extracted page really is as long as the
            # string; insert_text would clip everything past the page edge.
            page.insert_textbox(pymupdf.Rect(40, 40, 560, 800), text, fontsize=8)
    data = document.tobytes()
    document.close()
    return data


async def _paper(client: httpx.AsyncClient, title: str | None = None) -> dict:
    response = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "preprint",
            "title": title or f"Reading Test {uuid.uuid4().hex[:8]}",
            "year": 2026,
            "authors": [{"displayName": "Alice Example"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _upload(client: httpx.AsyncClient, paper_id: str, pages: list[str]) -> dict:
    response = await client.post(
        f"/v1/papers/{paper_id}/files",
        headers=SERVICE,
        files={"file": ("paper.pdf", _pdf(pages), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return response.json()["file"]


async def _drain() -> None:
    from paperdb.jobs.worker import run_once

    while await run_once():
        pass


async def _workspace(client: httpx.AsyncClient) -> dict:
    response = await client.post(
        "/v1/workspaces",
        headers=SERVICE,
        json={"name": f"Reading Survey {uuid.uuid4().hex[:8]}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _stage(client: httpx.AsyncClient, workspace_id: str, paper_id: str) -> None:
    response = await client.post(
        f"/v1/workspaces/{workspace_id}/papers",
        headers=SERVICE,
        json={"paperRefs": [paper_id]},
    )
    assert response.status_code == 200, response.text


async def test_page_text_endpoint_returns_whole_pages(
    client: httpx.AsyncClient, drain_queue: None
) -> None:
    paper = await _paper(client)
    file = await _upload(
        client,
        paper["id"],
        ["Introduction to the first page.", "Method section here.", "Results and discussion."],
    )
    await _drain()

    full = await client.get(f"/v1/files/{file['id']}/text", headers=SERVICE)
    assert full.status_code == 200, full.text
    body = full.json()
    assert body["textStatus"] == "extracted"
    assert [page["page"] for page in body["pages"]] == [1, 2, 3]
    assert "Method section" in body["pages"][1]["text"]
    assert body["truncated"] is False

    ranged = await client.get(
        f"/v1/files/{file['id']}/text", params={"from": 2, "to": 3}, headers=SERVICE
    )
    assert [page["page"] for page in ranged.json()["pages"]] == [2, 3]


async def test_page_text_window_is_measured_in_pages(
    client: httpx.AsyncClient, drain_queue: None
) -> None:
    """Default read = whole paper; maxPages is the knob that truncates."""
    paper = await _paper(client)
    filler = "speculative decoding overhead analysis " * 40
    file = await _upload(client, paper["id"], [filler, filler, filler])
    await _drain()

    whole = (await client.get(f"/v1/files/{file['id']}/text", headers=SERVICE)).json()
    assert [page["page"] for page in whole["pages"]] == [1, 2, 3]
    assert whole["truncated"] is False

    window = await client.get(
        f"/v1/files/{file['id']}/text", params={"maxPages": 2}, headers=SERVICE
    )
    body = window.json()
    assert [page["page"] for page in body["pages"]] == [1, 2], body
    assert body["truncated"] is True
    assert body["nextPage"] == 3

    rest = await client.get(
        f"/v1/files/{file['id']}/text",
        params={"from": body["nextPage"]},
        headers=SERVICE,
    )
    assert [page["page"] for page in rest.json()["pages"]] == [3]


async def test_page_text_char_budget_is_opt_in(
    client: httpx.AsyncClient, drain_queue: None
) -> None:
    paper = await _paper(client)
    filler = "speculative decoding overhead analysis " * 40
    file = await _upload(client, paper["id"], [filler, filler, filler])
    await _drain()

    body = (
        await client.get(f"/v1/files/{file['id']}/text", params={"maxChars": 1000}, headers=SERVICE)
    ).json()
    assert len(body["pages"]) == 1, body
    assert body["truncated"] is True
    assert body["nextPage"] == 2


async def test_image_only_pdf_is_marked_ocr_needed(
    client: httpx.AsyncClient, drain_queue: None
) -> None:
    paper = await _paper(client)
    file = await _upload(client, paper["id"], ["", ""])
    await _drain()

    detail = await client.get(f"/v1/files/{file['id']}", headers=SERVICE)
    assert detail.json()["textStatus"] == "ocr_needed", detail.text

    body = (await client.get(f"/v1/files/{file['id']}/text", headers=SERVICE)).json()
    assert body["textStatus"] == "ocr_needed"
    # The pages exist, they just carry no characters — that is the distinction
    # textStatus draws, not an empty page list.
    assert [page["page"] for page in body["pages"]] == [1, 2]
    assert all(page["text"] == "" for page in body["pages"])


async def test_scoped_search_reaches_staged_papers(
    client: httpx.AsyncClient, drain_queue: None
) -> None:
    workspace = await _workspace(client)
    marker = f"qwlz{uuid.uuid4().hex[:8]}"
    paper = await _paper(client)
    file = await _upload(client, paper["id"], [f"The {marker} token anchors this page."])
    await _drain()
    await _stage(client, workspace["id"], paper["id"])

    # Library scope still hides staging.
    library = await client.get("/v1/search/fulltext", params={"q": marker}, headers=SERVICE)
    assert library.json()["hits"] == []

    by_workspace = await client.get(
        "/v1/search/fulltext", params={"q": marker, "workspaceId": workspace["id"]}, headers=SERVICE
    )
    assert by_workspace.status_code == 200, by_workspace.text
    hits = by_workspace.json()["hits"]
    assert [hit["paperId"] for hit in hits] == [paper["id"]]
    assert hits[0]["fileId"] == file["id"]

    by_paper = await client.get(
        "/v1/search/fulltext", params={"q": marker, "paperId": paper["id"]}, headers=SERVICE
    )
    assert by_paper.json()["hits"][0]["paperId"] == paper["id"]

    # And the page itself is readable while the paper is still staged.
    text = await client.get(
        f"/v1/files/{file['id']}/text", params={"from": 1, "to": 1}, headers=SERVICE
    )
    assert marker in text.json()["pages"][0]["text"]


async def test_search_context_and_per_paper_cap(
    client: httpx.AsyncClient, drain_queue: None
) -> None:
    paper = await _paper(client)
    marker = f"ctx{uuid.uuid4().hex[:8]}"
    line = f"{marker} " + "context words around the hit " * 10
    file = await _upload(client, paper["id"], [line, line, line])
    await _drain()

    narrow = await client.get(
        "/v1/search/fulltext", params={"q": marker, "contextChars": 60}, headers=SERVICE
    )
    wide = await client.get(
        "/v1/search/fulltext", params={"q": marker, "contextChars": 600}, headers=SERVICE
    )
    assert len(wide.json()["hits"][0]["snippet"]) > len(narrow.json()["hits"][0]["snippet"])

    capped = await client.get(
        "/v1/search/fulltext",
        params={"q": marker, "paperId": paper["id"], "maxPerPaper": 1},
        headers=SERVICE,
    )
    hits = capped.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["fileId"] == file["id"]


async def test_scoped_search_rejects_ambiguous_scope(client: httpx.AsyncClient) -> None:
    workspace = await _workspace(client)
    paper = await _paper(client)
    response = await client.get(
        "/v1/search/fulltext",
        params={
            "q": "anything",
            "paperId": paper["id"],
            "workspaceId": workspace["id"],
        },
        headers=SERVICE,
    )
    assert response.status_code == 422, response.text


async def test_workspace_rows_expose_readability(
    client: httpx.AsyncClient, drain_queue: None
) -> None:
    workspace = await _workspace(client)
    with_pdf = await _paper(client)
    without_pdf = await _paper(client)
    await _upload(client, with_pdf["id"], ["Readable page text."])
    await _drain()
    await _stage(client, workspace["id"], with_pdf["id"])
    await _stage(client, workspace["id"], without_pdf["id"])

    detail = (await client.get(f"/v1/workspaces/{workspace['id']}", headers=SERVICE)).json()
    rows = {row["paperId"]: row for row in detail["papers"]}
    assert rows[with_pdf["id"]]["pdfCount"] == 1
    assert rows[with_pdf["id"]]["textStatus"] == "extracted"
    assert rows[without_pdf["id"]]["pdfCount"] == 0
    assert rows[without_pdf["id"]]["textStatus"] is None


async def test_page_image_renders_png(client: httpx.AsyncClient, drain_queue: None) -> None:
    paper = await _paper(client)
    file = await _upload(client, paper["id"], ["Figure 1: a chart, not text."])
    await _drain()

    response = await client.get(f"/v1/files/{file['id']}/pages/1/image", headers=SERVICE)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")

    out_of_range = await client.get(f"/v1/files/{file['id']}/pages/99/image", headers=SERVICE)
    assert out_of_range.status_code == 422


def _pdf_with_toc() -> bytes:
    document = pymupdf.open()
    for index in range(3):
        page = document.new_page()
        page.insert_text((72, 72), f"Section body on page {index + 1}", fontsize=11)
    document.set_toc(
        [
            [1, "Introduction", 1],
            [2, "Motivation", 1],
            [1, "Method", 2],
            [1, "Results", 3],
        ]
    )
    data = document.tobytes()
    document.close()
    return data


async def test_page_outline_from_bookmarks(client: httpx.AsyncClient, drain_queue: None) -> None:
    paper = await _paper(client)
    response = await client.post(
        f"/v1/papers/{paper['id']}/files",
        headers=SERVICE,
        files={"file": ("toc.pdf", _pdf_with_toc(), "application/pdf")},
    )
    file_id = response.json()["file"]["id"]
    await _drain()

    outline = await client.get(f"/v1/files/{file_id}/outline", headers=SERVICE)
    assert outline.status_code == 200, outline.text
    body = outline.json()
    assert body["pageCount"] == 3
    assert [section["title"] for section in body["sections"]] == [
        "Introduction",
        "Motivation",
        "Method",
        "Results",
    ]
    assert [section["page"] for section in body["sections"]] == [1, 1, 2, 3]
    assert body["sections"][1]["level"] == 2


async def test_page_outline_empty_without_bookmarks(
    client: httpx.AsyncClient, drain_queue: None
) -> None:
    paper = await _paper(client)
    file = await _upload(client, paper["id"], ["No bookmarks here."])
    await _drain()

    body = (await client.get(f"/v1/files/{file['id']}/outline", headers=SERVICE)).json()
    assert body["sections"] == []
    assert body["pageCount"] == 1
