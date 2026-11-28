"""Library overview endpoint: the agent's "what do I have?" in one call."""

from __future__ import annotations

import uuid

import httpx
import pymupdf

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


async def _stats(client: httpx.AsyncClient) -> dict:
    response = await client.get("/v1/library/stats", headers=SERVICE)
    assert response.status_code == 200, response.text
    return response.json()


async def _paper(client: httpx.AsyncClient) -> dict:
    response = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "preprint",
            "title": f"Stats {uuid.uuid4().hex[:8]}",
            "year": 2026,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_stats_counts_track_the_library(client: httpx.AsyncClient) -> None:
    before = await _stats(client)
    paper = await _paper(client)
    collection = await client.post(
        "/v1/collections", headers=SERVICE, json={"name": f"Stats Shelf {uuid.uuid4().hex[:6]}"}
    )
    assert collection.status_code == 201, collection.text

    after = await _stats(client)
    assert after["papers"] == before["papers"] + 1
    assert after["collections"] == before["collections"] + 1
    assert after["byStatus"].get("unread", 0) == before["byStatus"].get("unread", 0) + 1

    # A staged paper is not library inventory.
    workspace = (
        await client.post(
            "/v1/workspaces", headers=SERVICE, json={"name": f"Stats WS {uuid.uuid4().hex[:6]}"}
        )
    ).json()
    await client.post(
        f"/v1/workspaces/{workspace['id']}/papers",
        headers=SERVICE,
        json={"paperRefs": [paper["id"]]},
    )
    staged = await _stats(client)
    assert staged["papers"] == after["papers"] - 1
    assert staged["staged"] == after["staged"] + 1


async def test_stats_reports_readability(client: httpx.AsyncClient, drain_queue: None) -> None:
    before = await _stats(client)
    paper = await _paper(client)
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "readable text", fontsize=11)
    data = document.tobytes()
    document.close()
    upload = await client.post(
        f"/v1/papers/{paper['id']}/files",
        headers=SERVICE,
        files={"file": ("s.pdf", data, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text

    from paperdb.jobs.worker import run_once

    while await run_once():
        pass

    after = await _stats(client)
    assert after["withPdf"] == before["withPdf"] + 1
    assert after["textIndexed"] == before["textIndexed"] + 1
