from __future__ import annotations

import httpx
import pytest

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_ready(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200, response.text


async def test_meta(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/meta", headers=SERVICE)
    assert response.status_code == 200
    body = response.json()
    assert body["counts"]["venues"] == 677 + 547
    assert {item["scheme"] for item in body["datasets"]} == {"ccf", "cas"}
    assert body["venues"]["missingIssn"] > 0


async def test_requires_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/meta")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "unauthorized"


async def test_service_me(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/me", headers=SERVICE)
    assert response.status_code == 200
    assert response.json()["kind"] == "service"


async def test_personal_token_lifecycle(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/v1/tokens",
        headers=SERVICE,
        json={"name": "pytest", "scopes": ["papers:read"]},
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    token_id = created.json()["id"]
    assert token.startswith("pdb_pat_")

    personal = {"Authorization": f"Bearer {token}"}
    me = await client.get("/v1/me", headers=personal)
    assert me.status_code == 200
    assert me.json()["kind"] == "personal"
    assert me.json()["scopes"] == ["papers:read"]

    # A read-only token cannot write.
    forbidden = await client.post(
        "/v1/papers", headers=personal, json={"kind": "preprint", "title": "x", "year": 2026}
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "insufficient_scope"

    revoked = await client.delete(f"/v1/tokens/{token_id}", headers=SERVICE)
    assert revoked.status_code == 204
    assert (await client.get("/v1/me", headers=personal)).status_code == 401


async def test_unknown_scope(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/tokens",
        headers=SERVICE,
        json={"name": "bad", "scopes": ["nope"]},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_scope"


@pytest.mark.parametrize("token", ["garbage", "pdb_pat_deadbeef_missing"])
async def test_invalid_tokens(client: httpx.AsyncClient, token: str) -> None:
    response = await client.get("/v1/meta", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_scoped_rejects_unknown_scope() -> None:
    """A typo'd scope must fail at route registration, not silently 403 forever."""
    from paperdb.api.deps import scoped

    with pytest.raises(ValueError, match="unknown scope"):
        scoped("organize:read")
