from __future__ import annotations

import uuid

import httpx
import pytest

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


async def _paper(client: httpx.AsyncClient, title: str, openalex: str) -> dict:
    response = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "preprint",
            "title": title,
            "year": 2026,
            "identifiers": [{"scheme": "openalex", "value": openalex, "isPrimary": True}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_citation_sync_and_queries(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    suffix = uuid.uuid4().hex[:8]
    citing = await _paper(client, f"Citing Paper {suffix}", f"W{int(uuid.uuid4().int % 10**10)}")
    cited = await _paper(client, f"Cited Paper {suffix}", f"W{int(uuid.uuid4().int % 10**10)}")
    # Use the actual identifiers generated in this test.
    citing_id = citing["identifiers"][0]["value"]
    cited_id = cited["identifiers"][0]["value"]
    external_id = f"W{int(uuid.uuid4().int % 10**10)}"

    async def fake_get_json(client, url, *, params=None, headers=None, attempts=3):
        params = params or {}
        if "filter" in params and str(params["filter"]).startswith("cites:"):
            return {"results": []}
        if url.rstrip("/").endswith(citing_id):
            return {
                "id": f"https://openalex.org/{citing_id}",
                "referenced_works": [
                    f"https://openalex.org/{cited_id}",
                    f"https://openalex.org/{external_id}",
                ],
                "cited_by_count": 7,
            }
        if "filter" in params and str(params["filter"]).startswith("doi:"):
            return {"results": []}
        return None

    monkeypatch.setattr("paperdb.citations.service.get_json", fake_get_json)

    response = await client.post(
        "/v1/citations/sync",
        headers=SERVICE,
        json={"paperIds": [citing["id"]]},
    )
    assert response.status_code == 202, response.text
    from paperdb.jobs.worker import run_once

    assert await run_once() is True
    job = (await client.get(f"/v1/jobs/{response.json()['jobId']}", headers=SERVICE)).json()
    assert job["status"] == "succeeded", job

    refs = (await client.get(f"/v1/papers/{citing['id']}/references", headers=SERVICE)).json()
    assert refs["items"][0]["inLibrary"] is True
    assert refs["items"][0]["paper"]["id"] == cited["id"]
    assert any(
        item["external"] and item["external"]["openalexId"] == external_id for item in refs["items"]
    )

    citations = (await client.get(f"/v1/papers/{cited['id']}/citations", headers=SERVICE)).json()
    assert citations["items"][0]["paper"]["id"] == citing["id"]
