from __future__ import annotations

import httpx

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


async def test_bibtex_import_rejects_malformed_options(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/imports/bibtex",
        headers=SERVICE,
        files={"file": ("x.bib", b"@article{x, title={X}, year={2024}}", "application/x-bibtex")},
        data={"options": "not-json"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_options"
