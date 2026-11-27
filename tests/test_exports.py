from __future__ import annotations

import uuid

import httpx

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


async def _create_paper(client: httpx.AsyncClient) -> dict:
    venues = await client.get("/v1/venues", params={"q": "AAAI", "limit": 1}, headers=SERVICE)
    venue_id = venues.json()["items"][0]["id"]
    response = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "conference",
            "title": "BibTeX Export Test",
            "year": 2026,
            "venueId": venue_id,
            "authors": [
                {"displayName": "Shaoyuan Yu", "familyName": "Yu", "givenName": "Shaoyuan"},
                {"displayName": "Jane Doe"},
            ],
            "identifiers": [
                {"scheme": "doi", "value": f"10.9999/bib-{uuid.uuid4().hex}", "isPrimary": True},
                {"scheme": "arxiv", "value": f"2601.{uuid.uuid4().int % 100000:05d}"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_paper_bibtex(client: httpx.AsyncClient) -> None:
    paper = await _create_paper(client)
    response = await client.get(f"/v1/papers/{paper['id']}/bibtex", headers=SERVICE)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-bibtex")
    body = response.text
    assert f"@inproceedings{{{paper['citekey']}," in body
    assert "booktitle = {AAAI 2026}" in body
    assert "author = {Yu, Shaoyuan" in body
    assert "Doe" in body
    assert "archivePrefix = {arXiv}" in body


async def test_export_token_lifecycle(client: httpx.AsyncClient) -> None:
    paper = await _create_paper(client)
    created = await client.post(
        "/v1/export-tokens",
        headers=SERVICE,
        json={"name": "pytest-overleaf", "scopeType": "library"},
    )
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["url"].endswith(".bib")
    token_id = data["id"]

    path = data["url"].split("127.0.0.1:9720", 1)[-1]
    # No Authorization header: the URL itself is the credential.
    fetched = await client.get(path)
    assert fetched.status_code == 200, fetched.text
    assert paper["citekey"] in fetched.text
    etag = fetched.headers["etag"]
    assert etag

    not_modified = await client.get(path, headers={"If-None-Match": etag})
    assert not_modified.status_code == 304

    revoked = await client.delete(f"/v1/export-tokens/{token_id}", headers=SERVICE)
    assert revoked.status_code == 204
    assert (await client.get(path)).status_code == 404


async def test_paper_scoped_export(client: httpx.AsyncClient) -> None:
    paper = await _create_paper(client)
    created = await client.post(
        "/v1/export-tokens",
        headers=SERVICE,
        json={"name": "single", "scopeType": "paper", "scopeId": paper["id"]},
    )
    assert created.status_code == 201, created.text
    path = created.json()["url"].split("127.0.0.1:9720", 1)[-1]
    body = (await client.get(path)).text
    assert paper["citekey"] in body
    assert body.count("@inproceedings") == 1


async def test_collection_and_project_scoped_export(client: httpx.AsyncClient) -> None:
    paper = await _create_paper(client)
    suffix = uuid.uuid4().hex[:8]
    collection = await client.post(
        "/v1/collections", headers=SERVICE, json={"name": f"Export Collection {suffix}"}
    )
    assert collection.status_code == 201, collection.text
    collection_id = collection.json()["id"]
    added = await client.post(
        f"/v1/collections/{collection_id}/papers",
        headers=SERVICE,
        json={"paperIds": [paper["id"]]},
    )
    assert added.status_code == 200, added.text
    project = await client.post(
        "/v1/projects", headers=SERVICE, json={"name": f"Export Project {suffix}"}
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    pinned = await client.post(
        f"/v1/projects/{project_id}/papers",
        headers=SERVICE,
        json={"paperIds": [paper["id"]]},
    )
    assert pinned.status_code == 200, pinned.text

    for scope_type, scope_id in [
        ("collection", collection_id),
        ("project", project_id),
    ]:
        created = await client.post(
            "/v1/export-tokens",
            headers=SERVICE,
            json={
                "name": f"pytest-{scope_type}",
                "scopeType": scope_type,
                "scopeId": scope_id,
            },
        )
        assert created.status_code == 201, created.text
        data = created.json()
        path = data["url"].split("127.0.0.1:9720", 1)[-1]
        fetched = await client.get(path)
        assert fetched.status_code == 200, fetched.text
        assert paper["citekey"] in fetched.text
        revoked = await client.delete(f"/v1/export-tokens/{data['id']}", headers=SERVICE)
        assert revoked.status_code == 204
