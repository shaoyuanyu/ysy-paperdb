from __future__ import annotations

import time

import httpx
import pytest

from paperdb.auth.totp import current_totp, reset_failures, totp_at, verify_totp

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 test vector secret


def test_totp_accepts_current_and_window() -> None:
    reset_failures()
    code = totp_at(SECRET, 1000)
    assert verify_totp(SECRET, code, now=1000 * 30)
    assert verify_totp(SECRET, code, now=1000 * 30 + 29)  # same step
    assert verify_totp(SECRET, code, now=1001 * 30)  # next step, window=1
    assert verify_totp(SECRET, code, now=1002 * 30) is False  # beyond window
    reset_failures()


def test_totp_rejects_garbage_and_locks_after_failures() -> None:
    reset_failures()
    now = 2000 * 30
    assert verify_totp(SECRET, "not-a-code", now=now) is False
    for _ in range(4):
        assert verify_totp(SECRET, "000000", now=now) is False
    # 5 failures recorded -> locked even for a valid code
    assert verify_totp(SECRET, totp_at(SECRET, 2000), now=now) is False
    # lock expires after the window (code for the *current* counter then)
    later = now + 16 * 60
    assert verify_totp(SECRET, totp_at(SECRET, int(later // 30)), now=later) is True
    reset_failures()


async def _personal_token(client: httpx.AsyncClient) -> dict[str, str]:
    created = await client.post(
        "/v1/tokens",
        headers=SERVICE,
        json={
            "name": f"totp-test-{int(time.time())}",
            "scopes": ["papers:read", "papers:write", "organize:write"],
        },
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    return {"Authorization": f"Bearer {token}"}


async def _paper(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    import uuid

    created = await client.post(
        "/v1/papers",
        headers=headers,
        json={
            "kind": "preprint",
            "title": f"TOTP Gate Paper {uuid.uuid4().hex[:8]}",
            "year": 2026,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_totp_gate_personal_token_needs_code(client: httpx.AsyncClient) -> None:
    reset_failures()
    personal = await _personal_token(client)
    paper_id = await _paper(client, SERVICE)

    missing = await client.delete(f"/v1/papers/{paper_id}", headers=personal)
    assert missing.status_code == 428, missing.text
    assert missing.json()["code"] == "totp_required"

    wrong = await client.delete(
        f"/v1/papers/{paper_id}", headers={**personal, "X-TOTP-Code": "000000"}
    )
    assert wrong.status_code == 403, wrong.text
    assert wrong.json()["code"] == "totp_invalid"

    code = current_totp(SECRET)
    ok = await client.delete(f"/v1/papers/{paper_id}", headers={**personal, "X-TOTP-Code": code})
    assert ok.status_code == 204, ok.text
    reset_failures()


async def test_totp_gate_service_token_bypasses(client: httpx.AsyncClient) -> None:
    reset_failures()
    paper_id = await _paper(client, SERVICE)
    response = await client.delete(f"/v1/papers/{paper_id}", headers=SERVICE)
    assert response.status_code == 204, response.text
    reset_failures()


async def test_totp_gate_merge(client: httpx.AsyncClient) -> None:
    reset_failures()
    personal = await _personal_token(client)
    source = await _paper(client, SERVICE)
    target = await _paper(client, SERVICE)

    blocked = await client.post(
        f"/v1/papers/{source}/merge", headers=personal, json={"into": target}
    )
    assert blocked.status_code == 428, blocked.text

    code = current_totp(SECRET)
    merged = await client.post(
        f"/v1/papers/{source}/merge",
        headers={**personal, "X-TOTP-Code": code},
        json={"into": target},
    )
    assert merged.status_code == 200, merged.text
    reset_failures()


@pytest.mark.parametrize(
    "path_factory",
    [
        lambda paper_id: f"/v1/papers/{paper_id}/tags",  # association removal: NOT gated
    ],
)
async def test_association_removal_not_gated(client: httpx.AsyncClient, path_factory) -> None:
    reset_failures()
    personal = await _personal_token(client)
    paper_id = await _paper(client, SERVICE)
    response = await client.request(
        "DELETE", path_factory(paper_id), headers=personal, json={"paths": ["status/x"]}
    )
    # ungated: any failure is scope/validation semantics, never a TOTP code
    assert "totp" not in str(response.json().get("code", "")), response.text
    reset_failures()
