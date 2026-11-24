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
            "identifiers": [{"scheme": "doi", "value": f"10.9999/org-{uuid.uuid4().hex}"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_collection_tree_and_membership(client: httpx.AsyncClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    root = await client.post(
        "/v1/collections",
        headers=SERVICE,
        json={"name": f"Root {suffix}"},
    )
    assert root.status_code == 201, root.text
    root_id = root.json()["id"]
    child = await client.post(
        "/v1/collections",
        headers=SERVICE,
        json={"name": f"Child {suffix}", "parentId": root_id},
    )
    assert child.status_code == 201, child.text
    child_id = child.json()["id"]

    duplicate = await client.post(
        "/v1/collections",
        headers=SERVICE,
        json={"name": f"Child {suffix}", "parentId": root_id},
    )
    assert duplicate.status_code == 409

    paper = await _paper(client, f"Organized Paper {suffix}")
    added = await client.post(
        f"/v1/collections/{child_id}/papers",
        headers=SERVICE,
        json={"paperIds": [paper["id"]]},
    )
    assert added.status_code == 200, added.text
    assert added.json()["added"] == 1

    tree = await client.get("/v1/collections/tree", headers=SERVICE)
    assert tree.status_code == 200
    nodes = {}

    def flatten(items):
        for item in items:
            nodes[item["id"]] = item
            flatten(item.get("children") or [])

    flatten(tree.json())
    assert nodes[root_id]["totalCount"] == 1
    assert nodes[child_id]["paperCount"] == 1

    fetched = (await client.get(f"/v1/papers/{paper['id']}", headers=SERVICE)).json()
    assert child_id in fetched["collections"]

    cycle = await client.post(
        f"/v1/collections/{root_id}/move",
        headers=SERVICE,
        json={"parentId": child_id},
    )
    assert cycle.status_code == 422


async def test_tag_hierarchy_membership_rename_merge(client: httpx.AsyncClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    tag = await client.post(
        "/v1/tags",
        headers=SERVICE,
        json={"path": f"topic/llm/rag-{suffix}"},
    )
    assert tag.status_code == 201, tag.text
    tag_id = tag.json()["id"]
    assert tag.json()["path"] == f"topic/llm/rag-{suffix}"

    tree = (await client.get("/v1/tags/tree", headers=SERVICE)).json()
    paths = set()

    def flatten_tags(items):
        for item in items:
            paths.add(item["path"])
            flatten_tags(item.get("children") or [])

    flatten_tags(tree)
    assert f"topic/llm/rag-{suffix}" in paths

    paper = await _paper(client, f"Tagged Paper {suffix}")
    added = await client.post(
        f"/v1/papers/{paper['id']}/tags",
        headers=SERVICE,
        json={"paths": [f"topic/llm/rag-{suffix}"]},
    )
    assert added.status_code == 200, added.text
    assert added.json()["added"] == 1

    fetched = (await client.get(f"/v1/papers/{paper['id']}", headers=SERVICE)).json()
    assert f"topic/llm/rag-{suffix}" in fetched["tags"]

    renamed = await client.patch(
        f"/v1/tags/{tag_id}",
        headers=SERVICE,
        json={"name": f"RAG Renamed {suffix}"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == f"RAG Renamed {suffix}"
    assert renamed.json()["path"] == f"topic/llm/rag-{suffix}"

    target = await client.post(
        "/v1/tags",
        headers=SERVICE,
        json={"path": f"topic/merged-{suffix}"},
    )
    merged = await client.post(
        f"/v1/tags/{tag_id}/merge",
        headers=SERVICE,
        json={"intoTagId": target.json()["id"]},
    )
    assert merged.status_code == 200, merged.text
    fetched = (await client.get(f"/v1/papers/{paper['id']}", headers=SERVICE)).json()
    assert f"topic/merged-{suffix}" in fetched["tags"]
