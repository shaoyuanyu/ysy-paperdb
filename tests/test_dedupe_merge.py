from __future__ import annotations

import uuid

import httpx
import pytest

from paperdb.ingest.types import AuthorRecord, Candidate, VenueSuggestion

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


async def _venue(client: httpx.AsyncClient) -> dict:
    return (
        await client.get("/v1/venues", params={"q": "AAAI", "limit": 1}, headers=SERVICE)
    ).json()["items"][0]


async def _create(client: httpx.AsyncClient, title: str, venue_id: str) -> dict:
    response = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "conference",
            "title": title,
            "year": 2026,
            "venueId": venue_id,
            "authors": [{"displayName": "Same Author", "familyName": "Author"}],
            "identifiers": [{"scheme": "doi", "value": f"10.9999/{uuid.uuid4().hex}"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_duplicates_and_merge(client: httpx.AsyncClient) -> None:
    venue = await _venue(client)
    title = f"Merge Duplicate Paper {uuid.uuid4().hex[:8]}"
    target = await _create(client, title, venue["id"])
    source = await _create(client, title, venue["id"])

    duplicates = await client.get(f"/v1/papers/{source['id']}/duplicates", headers=SERVICE)
    assert duplicates.status_code == 200, duplicates.text
    body = duplicates.json()
    assert any(item["paper"]["id"] == target["id"] for item in body)
    assert body[0]["score"] >= 0.86

    merged = await client.post(
        f"/v1/papers/{source['id']}/merge",
        headers=SERVICE,
        json={"into": target["id"], "fields": {"abstract": "source"}},
    )
    assert merged.status_code == 200, merged.text
    merged_body = merged.json()
    assert merged_body["id"] == target["id"]
    assert len(merged_body["identifiers"]) == 2

    # The old id redirects to the survivor.
    old = await client.get(f"/v1/papers/{source['id']}", headers=SERVICE)
    assert old.status_code == 200
    assert old.json()["id"] == target["id"]


async def test_enrich_job(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    venue = await _venue(client)
    title = f"Enrich Me {uuid.uuid4().hex[:8]}"
    paper = await _create(client, title, venue["id"])
    doi = paper["identifiers"][0]["value"]

    async def fake_resolve(session, raw, settings, *, client=None):
        return (
            Candidate(
                confidence=0.95,
                sources=["crossref"],
                kind="conference",
                title=f"Enriched {title}",
                abstract="Enriched abstract.",
                year=2026,
                publication_date=None,
                authors=[
                    AuthorRecord(
                        name="Same Author",
                        family="Author",
                        given="Same",
                        dblp_pid="A/Same_Author",
                    )
                ],
                identifiers=[{"scheme": "doi", "value": doi, "isPrimary": True}],
                venue=VenueSuggestion(
                    venue_id=venue["id"],
                    full_name=venue["fullName"],
                    abbrev=venue["abbrev"],
                    kind="conference",
                    year=2026,
                    confidence=0.95,
                    evidence="dblp",
                ),
                evaluation={"ccf": None, "cas": None},
                _venue_name=venue["fullName"],
                _dblp_venue_key="conf/aaai",
            ),
            [],
        )

    monkeypatch.setattr("paperdb.jobs.handlers.resolve_input", fake_resolve)
    response = await client.post(f"/v1/papers/{paper['id']}/enrich", headers=SERVICE)
    assert response.status_code == 202, response.text
    job_id = response.json()["jobId"]

    from paperdb.jobs.worker import run_once

    assert await run_once() is True
    job = (await client.get(f"/v1/jobs/{job_id}", headers=SERVICE)).json()
    assert job["status"] == "succeeded", job
    fetched = (await client.get(f"/v1/papers/{paper['id']}", headers=SERVICE)).json()
    assert fetched["title"].startswith("Enriched")
    assert fetched["abstract"] == "Enriched abstract."
    assert fetched["version"] >= 2
