from __future__ import annotations

import uuid

import httpx
import pymupdf
import pytest

from paperdb.ingest.types import AuthorRecord, Candidate

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


async def _workspace(client: httpx.AsyncClient, name: str | None = None) -> dict:
    response = await client.post(
        "/v1/workspaces",
        headers=SERVICE,
        json={"name": name or f"Survey {uuid.uuid4().hex[:8]}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _paper(client: httpx.AsyncClient, title: str) -> dict:
    response = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "preprint",
            "title": title,
            "year": 2026,
            "authors": [{"displayName": "Alice Example"}],
        },
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
    assert response.json()["added"] == 1


async def test_workspace_stages_hides_and_promotes(client: httpx.AsyncClient) -> None:
    workspace = await _workspace(client)
    title = f"Staged Paper {uuid.uuid4().hex[:8]}"
    paper = await _paper(client, title)
    await _stage(client, workspace["id"], paper["id"])

    detail = (await client.get(f"/v1/workspaces/{workspace['id']}", headers=SERVICE)).json()
    assert detail["paperCount"] == 1
    assert detail["pendingCount"] == 1
    assert detail["papers"][0]["paperId"] == paper["id"]
    assert detail["papers"][0]["promoted"] is False

    listed = (
        await client.get("/v1/papers", headers=SERVICE, params={"q": title})
    ).json()["items"]
    assert all(item["id"] != paper["id"] for item in listed)

    included = (
        await client.get(
            "/v1/papers", headers=SERVICE, params={"q": title, "includeStaged": True}
        )
    ).json()["items"]
    assert any(item["id"] == paper["id"] for item in included)

    in_workspace = (
        await client.get("/v1/papers", headers=SERVICE, params={"workspace": workspace["id"]})
    ).json()["items"]
    assert [item["id"] for item in in_workspace] == [paper["id"]]

    collection = (
        await client.post(
            "/v1/collections",
            headers=SERVICE,
            json={"name": f"Kept {uuid.uuid4().hex[:8]}"},
        )
    ).json()
    promoted = await client.post(
        f"/v1/workspaces/{workspace['id']}/promote",
        headers=SERVICE,
        json={"paperRefs": [paper["id"]], "collectionId": collection["id"]},
    )
    assert promoted.status_code == 200, promoted.text

    detail = (await client.get(f"/v1/workspaces/{workspace['id']}", headers=SERVICE)).json()
    assert detail["pendingCount"] == 0
    assert detail["papers"][0]["promoted"] is True

    listed = (
        await client.get("/v1/papers", headers=SERVICE, params={"q": title})
    ).json()["items"]
    assert any(item["id"] == paper["id"] for item in listed)
    fetched = (await client.get(f"/v1/papers/{paper['id']}", headers=SERVICE)).json()
    assert fetched["staged"] is False
    assert collection["id"] in fetched["collections"]


async def test_workspace_discard_soft_deletes_staged_paper(client: httpx.AsyncClient) -> None:
    workspace = await _workspace(client)
    paper = await _paper(client, f"Discarded Paper {uuid.uuid4().hex[:8]}")
    await _stage(client, workspace["id"], paper["id"])

    response = await client.post(
        f"/v1/workspaces/{workspace['id']}/discard",
        headers=SERVICE,
        json={"paperRefs": [paper["id"]]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["discarded"] == 1
    gone = await client.get(f"/v1/papers/{paper['id']}", headers=SERVICE)
    assert gone.status_code == 404


async def test_close_workspace_discards_unpromoted(client: httpx.AsyncClient) -> None:
    workspace = await _workspace(client)
    paper = await _paper(client, f"Unpromoted Paper {uuid.uuid4().hex[:8]}")
    await _stage(client, workspace["id"], paper["id"])

    closed = await client.post(
        f"/v1/workspaces/{workspace['id']}/close",
        headers=SERVICE,
        json={"discardUnpromoted": True},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["closedAt"] is not None
    gone = await client.get(f"/v1/papers/{paper['id']}", headers=SERVICE)
    assert gone.status_code == 404

    listed = (await client.get("/v1/workspaces", headers=SERVICE)).json()["items"]
    assert all(item["id"] != workspace["id"] for item in listed)


async def test_import_into_workspace_stages_paper(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = await _workspace(client)
    title = f"Workspace Import {uuid.uuid4().hex[:8]}"
    doi = f"10.9999/{uuid.uuid4().hex}"

    async def fake_resolve(session, raw, settings, *, client=None):
        return (
            Candidate(
                confidence=0.9,
                sources=["crossref"],
                kind="preprint",
                title=title,
                abstract="Staged abstract.",
                year=2026,
                publication_date=None,
                authors=[AuthorRecord(name="Alice Example", family="Example", given="Alice")],
                identifiers=[{"scheme": "doi", "value": doi, "isPrimary": True}],
                venue=None,
                evaluation={"ccf": None, "cas": None},
            ),
            [],
        )

    monkeypatch.setattr("paperdb.imports.service.resolve_input", fake_resolve)
    created = await client.post(
        "/v1/imports/identifiers",
        headers=SERVICE,
        json={
            "items": [doi],
            "options": {"defaultWorkspaceId": workspace["id"]},
        },
    )
    assert created.status_code == 202, created.text
    import_id = created.json()["importId"]

    from paperdb.jobs.worker import run_once

    while await run_once():
        pass

    items = (await client.get(f"/v1/imports/{import_id}/items", headers=SERVICE)).json()
    assert items[0]["status"] == "created", items
    paper_id = items[0]["paperId"]
    paper = (await client.get(f"/v1/papers/{paper_id}", headers=SERVICE)).json()
    assert paper["staged"] is True

    listed = (
        await client.get("/v1/papers", headers=SERVICE, params={"q": title})
    ).json()["items"]
    assert listed == []
    detail = (await client.get(f"/v1/workspaces/{workspace['id']}", headers=SERVICE)).json()
    assert detail["papers"][0]["paperId"] == paper_id


async def test_staged_paper_hidden_from_fulltext(
    client: httpx.AsyncClient, drain_queue: None
) -> None:
    workspace = await _workspace(client)
    marker = f"zzyzx{uuid.uuid4().hex[:8]}"
    paper = await _paper(client, f"Searchable {marker}")

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), f"Abstract. The {marker} token appears here.", fontsize=12)
    data = document.tobytes()
    document.close()
    uploaded = await client.post(
        f"/v1/papers/{paper['id']}/files",
        headers=SERVICE,
        files={"file": ("paper.pdf", data, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text

    from paperdb.jobs.worker import run_once

    while await run_once():
        pass

    await _stage(client, workspace["id"], paper["id"])
    hidden = (
        await client.get("/v1/search/fulltext", headers=SERVICE, params={"q": marker})
    ).json()["hits"]
    assert hidden == []
    visible = (
        await client.get(
            "/v1/search/fulltext",
            headers=SERVICE,
            params={"q": marker, "includeStaged": True},
        )
    ).json()["hits"]
    assert any(hit["paperId"] == paper["id"] for hit in visible)
