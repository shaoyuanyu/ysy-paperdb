from __future__ import annotations

import uuid

import httpx

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


async def test_venue_create_and_update(client: httpx.AsyncClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    created = await client.post(
        "/v1/venues",
        headers=SERVICE,
        json={
            "kind": "journal",
            "fullName": f"Test Journal of Systems {suffix}",
            "abbrev": f"TJS{suffix}",
            "issnPrint": "1234-5678",
            "ccfField": "软件工程/系统软件/程序设计语言",
            "ccfFieldKey": "软件工程",
        },
    )
    assert created.status_code == 201, created.text
    venue = created.json()
    assert venue["kind"] == "journal"
    assert venue["issnPrint"] == "12345678"
    assert venue["aliases"]

    updated = await client.patch(
        f"/v1/venues/{venue['id']}",
        headers=SERVICE,
        json={"fullName": f"Renamed Journal {suffix}", "abbrev": None},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["fullName"] == f"Renamed Journal {suffix}"
    assert updated.json()["abbrev"] is None

    listed = await client.get(
        "/v1/venues", params={"q": f"Renamed Journal {suffix}"}, headers=SERVICE
    )
    assert listed.status_code == 200
    assert any(item["id"] == venue["id"] for item in listed.json()["items"])


async def test_author_create_and_update(client: httpx.AsyncClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    created = await client.post(
        "/v1/authors",
        headers=SERVICE,
        json={
            "displayName": f"Test Author {suffix}",
            "familyName": "Author",
            "givenName": "Test",
            "isMe": True,
        },
    )
    assert created.status_code == 201, created.text
    author = created.json()
    assert author["isMe"] is True
    assert author["paperCount"] == 0

    updated = await client.patch(
        f"/v1/authors/{author['id']}",
        headers=SERVICE,
        json={
            "orcid": f"0000-0002-{suffix[:4]}-{suffix[4:]}",
            "homepageUrl": "https://example.com",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["orcid"].startswith("0000-0002-")
    assert updated.json()["homepageUrl"] == "https://example.com"

    second = await client.post(
        "/v1/authors",
        headers=SERVICE,
        json={"displayName": f"Another Me {suffix}", "isMe": True},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "conflict"
