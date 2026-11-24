from __future__ import annotations

import uuid

import httpx

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


async def _paper(client: httpx.AsyncClient, title: str) -> dict:
    venue = (
        await client.get("/v1/venues", params={"q": "AAAI", "limit": 1}, headers=SERVICE)
    ).json()["items"][0]
    response = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "conference",
            "title": title,
            "year": 2026,
            "venueId": venue["id"],
            "identifiers": [{"scheme": "doi", "value": f"10.9999/view-{uuid.uuid4().hex}"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_saved_view_and_filter(client: httpx.AsyncClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    title = f"View Target {suffix}"
    paper = await _paper(client, title)

    view = await client.post(
        "/v1/views",
        headers=SERVICE,
        json={
            "name": f"View {suffix}",
            "filter": {
                "version": 1,
                "match": "all",
                "rules": [{"field": "title", "op": "contains", "value": suffix}],
            },
            "sort": [{"field": "year", "direction": "desc"}],
        },
    )
    assert view.status_code == 201, view.text
    view_id = view.json()["id"]
    assert view.json()["count"] == 1

    listed = await client.get("/v1/papers", params={"view": view_id}, headers=SERVICE)
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == paper["id"] for item in listed.json()["items"])

    count = await client.get(f"/v1/views/{view_id}/count", headers=SERVICE)
    assert count.json()["count"] == 1

    invalid = await client.post(
        "/v1/views/validate",
        headers=SERVICE,
        json={"filter": {"version": 1, "rules": [{"field": "nope", "op": "eq", "value": 1}]}},
    )
    assert invalid.status_code == 422


async def test_project_collection_and_pinned_paper(client: httpx.AsyncClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    paper = await _paper(client, f"Project Target {suffix}")
    collection = await client.post(
        "/v1/collections",
        headers=SERVICE,
        json={"name": f"Project Collection {suffix}"},
    )
    collection_id = collection.json()["id"]
    await client.post(
        f"/v1/collections/{collection_id}/papers",
        headers=SERVICE,
        json={"paperIds": [paper["id"]]},
    )
    project = await client.post(
        "/v1/projects",
        headers=SERVICE,
        json={"name": f"Project {suffix}"},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    added = await client.post(
        f"/v1/projects/{project_id}/collections",
        headers=SERVICE,
        json={"collectionIds": [collection_id]},
    )
    assert added.status_code == 200, added.text
    fetched = await client.get(f"/v1/projects/{project_id}", headers=SERVICE)
    assert fetched.json()["paperCount"] == 1
    papers = await client.get(f"/v1/projects/{project_id}/papers", headers=SERVICE)
    assert any(item["id"] == paper["id"] for item in papers.json()["items"])


async def test_auto_rule_apply(client: httpx.AsyncClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    paper = await _paper(client, f"Rule Target {suffix}")
    rule = await client.post(
        "/v1/auto-rules",
        headers=SERVICE,
        json={
            "name": f"Rule {suffix}",
            "filter": {
                "version": 1,
                "rules": [{"field": "title", "op": "contains", "value": suffix}],
            },
            "actions": [{"type": "add_tag", "path": f"auto/rule-{suffix}"}],
            "runOn": ["manual"],
        },
    )
    assert rule.status_code == 201, rule.text
    rule_id = rule.json()["id"]
    applied = await client.post(f"/v1/auto-rules/{rule_id}/apply", headers=SERVICE)
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] == 1
    fetched = (await client.get(f"/v1/papers/{paper['id']}", headers=SERVICE)).json()
    assert f"auto/rule-{suffix}" in fetched["tags"]


async def test_bulk_patch(client: httpx.AsyncClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    paper = await _paper(client, f"Bulk Target {suffix}")
    collection = await client.post(
        "/v1/collections",
        headers=SERVICE,
        json={"name": f"Bulk Collection {suffix}"},
    )
    collection_id = collection.json()["id"]
    response = await client.patch(
        "/v1/papers/bulk",
        headers=SERVICE,
        json={
            "ids": [paper["id"]],
            "patch": {
                "addTags": [f"bulk/tag-{suffix}"],
                "addCollections": [collection_id],
                "state": {"status": "read", "rating": 4},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 1
    fetched = (await client.get(f"/v1/papers/{paper['id']}", headers=SERVICE)).json()
    assert f"bulk/tag-{suffix}" in fetched["tags"]
    assert collection_id in fetched["collections"]
    assert fetched["state"]["status"] == "read"
    assert fetched["state"]["rating"] == 4
