from __future__ import annotations

import uuid

import httpx

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


async def _venue_id(client: httpx.AsyncClient, q: str) -> str:
    response = await client.get("/v1/venues", params={"q": q, "limit": 5}, headers=SERVICE)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items, f"no venue for {q}"
    return items[0]["id"]


async def test_create_conference_paper(client: httpx.AsyncClient) -> None:
    venue_id = await _venue_id(client, "AAAI")
    doi = f"10.9999/test-{uuid.uuid4().hex}"
    payload = {
        "kind": "conference",
        "title": "A Test Paper About Interpretability",
        "year": 2026,
        "venueId": venue_id,
        "abstract": "Abstract text.",
        "authors": [
            {"displayName": "Shaoyuan Yu", "familyName": "Yu", "givenName": "Shaoyuan"},
            {"displayName": "Jane Doe"},
        ],
        "identifiers": [{"scheme": "doi", "value": f"https://doi.org/{doi}", "isPrimary": True}],
        "state": {"status": "queued", "priority": 2, "starred": True},
    }
    response = await client.post("/v1/papers", json=payload, headers=SERVICE)
    assert response.status_code == 201, response.text
    body = response.json()
    paper_id = body["id"]
    assert body["citekey"].startswith("yu2026")
    assert body["evaluation"]["ccf"]["level"] == "A"
    assert body["evaluation"]["cas"] is None
    assert body["state"]["starred"] is True
    assert body["identifiers"][0]["value"] == doi.lower()

    listing = await client.get(
        "/v1/papers", params={"q": "Interpretability", "kind": "conference"}, headers=SERVICE
    )
    assert listing.status_code == 200
    assert any(item["id"] == paper_id for item in listing.json()["items"])

    fetched = await client.get(f"/v1/papers/{paper_id}", headers=SERVICE)
    assert fetched.status_code == 200
    assert fetched.json()["authorCount"] == 2

    patched = await client.patch(
        f"/v1/papers/{paper_id}",
        headers=SERVICE,
        json={"title": "Updated Test Paper", "year": 2025},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "Updated Test Paper"
    assert patched.json()["version"] == 2
    assert "title" in patched.json()["userLockedFields"]

    state = await client.patch(
        f"/v1/papers/{paper_id}/state",
        headers=SERVICE,
        json={"status": "read", "rating": 5},
    )
    assert state.status_code == 200
    assert state.json()["status"] == "read"
    assert state.json()["rating"] == 5

    deleted = await client.delete(f"/v1/papers/{paper_id}", headers=SERVICE)
    assert deleted.status_code == 204
    assert (await client.get(f"/v1/papers/{paper_id}", headers=SERVICE)).status_code == 404
    restored = await client.post(f"/v1/papers/{paper_id}/restore", headers=SERVICE)
    assert restored.status_code == 200
    assert restored.json()["id"] == paper_id


async def test_create_journal_paper_with_cas(client: httpx.AsyncClient) -> None:
    venue_id = await _venue_id(client, "IEEE Communications Surveys and Tutorials")
    response = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "journal",
            "title": "A Survey of Surveys",
            "year": 2025,
            "venueId": venue_id,
            "volume": "17",
            "issue": "1",
            "pages": "1--35",
            "authors": [{"displayName": "Test Author"}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["evaluation"]["cas"]["level"] == "1"
    assert body["evaluation"]["cas"]["isTop"] is True
    assert body["evaluation"]["cas"]["minorFields"]
    assert body["evaluation"]["ccf"] is None


async def test_duplicate_identifier_and_validation(client: httpx.AsyncClient) -> None:
    venue_id = await _venue_id(client, "AAAI")
    doi = f"10.9999/dup-{uuid.uuid4().hex}"
    base = {
        "kind": "conference",
        "title": "Duplicate Identifier Test",
        "year": 2026,
        "venueId": venue_id,
        "identifiers": [{"scheme": "doi", "value": doi}],
    }
    first = await client.post("/v1/papers", json=base, headers=SERVICE)
    assert first.status_code == 201, first.text
    second = await client.post("/v1/papers", json=base, headers=SERVICE)
    assert second.status_code == 409
    assert second.json()["code"] == "duplicate"

    missing_venue = await client.post(
        "/v1/papers",
        json={"kind": "journal", "title": "No Venue", "year": 2026},
        headers=SERVICE,
    )
    assert missing_venue.status_code == 422

    preprint = await client.post(
        "/v1/papers",
        json={
            "kind": "preprint",
            "title": "No Venue Preprint",
            "year": 2026,
            "identifiers": [{"scheme": "arxiv", "value": "arXiv:2601.99999v1"}],
        },
        headers=SERVICE,
    )
    assert preprint.status_code == 201, preprint.text
    assert preprint.json()["venue"] is None
    assert preprint.json()["evaluation"] == {"ccf": None, "cas": None}


async def test_cursor_pagination(client: httpx.AsyncClient) -> None:
    for index in range(3):
        response = await client.post(
            "/v1/papers",
            headers=SERVICE,
            json={
                "kind": "preprint",
                "title": f"Cursor Test {index} {uuid.uuid4().hex}",
                "year": 2024 + index,
            },
        )
        assert response.status_code == 201, response.text

    first = await client.get("/v1/papers", params={"limit": 2, "kind": "preprint"}, headers=SERVICE)
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) == 2
    assert body["hasMore"] is True
    assert body["nextCursor"]

    second = await client.get(
        "/v1/papers",
        params={"limit": 2, "kind": "preprint", "cursor": body["nextCursor"]},
        headers=SERVICE,
    )
    assert second.status_code == 200
    assert second.json()["items"]


async def test_bad_cursor(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/papers", params={"cursor": "not-a-cursor"}, headers=SERVICE)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


async def test_sort_modes_and_q_filter(client: httpx.AsyncClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    for title, year in [
        (f"Zeta Sort {suffix}", 2021),
        (f"Alpha Sort {suffix}", 2023),
        (f"Mu Sort {suffix}", 2022),
    ]:
        response = await client.post(
            "/v1/papers",
            headers=SERVICE,
            json={"kind": "preprint", "title": title, "year": year},
        )
        assert response.status_code == 201, response.text

    by_title = await client.get(
        "/v1/papers",
        params={"q": suffix, "sort": "title", "limit": 10},
        headers=SERVICE,
    )
    assert by_title.status_code == 200, by_title.text
    titles = [item["title"] for item in by_title.json()["items"]]
    assert titles == sorted(titles, key=str.lower)

    by_year = await client.get(
        "/v1/papers",
        params={"q": suffix, "sort": "year", "limit": 10},
        headers=SERVICE,
    )
    years = [item["year"] for item in by_year.json()["items"]]
    assert years == sorted(years, reverse=True)

    q_filter = await client.get(
        "/v1/papers",
        params={"q": f"Alpha Sort {suffix}", "limit": 10},
        headers=SERVICE,
    )
    assert [item["year"] for item in q_filter.json()["items"]] == [2023]

    invalid = await client.get("/v1/papers", params={"sort": "nope"}, headers=SERVICE)
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_sort"
