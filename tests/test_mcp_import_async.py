"""Import jobs must not hold a tool call hostage.

The wait is a budget, not a promise: import tools poll for a bounded time
(15s by default, 60s at most, 0 = return now) and hand back the per-item state
plus a `next` hint when the job is still running. `get_import` is the
continuation, and never waits.
"""

from __future__ import annotations

import json

import pytest

from paperdb import mcp_server


def test_wait_budget_is_bounded() -> None:
    assert mcp_server._wait_budget(None) == 15.0
    assert mcp_server._wait_budget(0) == 0.0
    assert mcp_server._wait_budget(-5) == 0.0
    assert mcp_server._wait_budget(30) == 30.0
    # The old 180s default was longer than the host's whole tool timeout.
    assert mcp_server._wait_budget(180) == 60.0
    assert mcp_server._wait_budget("nonsense") == 15.0  # type: ignore[arg-type]
    assert mcp_server._wait_budget(float("nan")) == 15.0


async def test_still_running_job_returns_pending_state_and_next_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_request(method: str, path: str, **kwargs: object) -> object:
        calls.append(path)
        if path.endswith("/items"):
            return [
                {"rawInput": "doi:10.1/a", "status": "pending", "paperId": None, "error": None},
                {"rawInput": "doi:10.1/b", "status": "created", "paperId": "p-1", "error": None},
            ]
        return {"id": "imp-1", "status": "running", "stats": {"created": 1}}

    monkeypatch.setattr(mcp_server, "_request", fake_request)
    out = await mcp_server._wait_for_import("imp-1", wait_seconds=0)
    assert calls == ["/v1/imports/imp-1", "/v1/imports/imp-1/items"]
    assert out["status"] == "running"
    assert out["pendingItems"] == 1
    assert len(out["items"]) == 2
    assert "get_import" in out["next"] and "imp-1" in out["next"]
    assert "paper" not in out


async def test_finished_single_import_returns_the_summary_and_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_request(method: str, path: str, **kwargs: object) -> object:
        if path.endswith("/items"):
            return [{"rawInput": "10.1/a", "status": "created", "paperId": "p-9", "error": None}]
        if path == "/v1/papers/p-9":
            return {
                "id": "p-9",
                "citekey": "example2026",
                "title": "An Example Paper",
                "kind": "conference",
                "year": 2026,
                "venue": {"abbrev": "NDSS", "fullName": "NDSS", "ccf": "A"},
            }
        return {"id": "imp-9", "status": "succeeded", "stats": {"created": 1}}

    monkeypatch.setattr(mcp_server, "_request", fake_request)
    out = await mcp_server._wait_for_import("imp-9", wait_seconds=0)
    assert out["pendingItems"] == 0
    assert "next" not in out
    assert out["paper"]["citekey"] == "example2026"
    assert out["paper"]["venue"]["ccf"] == "A"


async def test_import_paper_returns_immediately_when_wait_is_zero(
    mcp_api: None, drain_queue: None
) -> None:
    """A queued job comes straight back, with the id needed to continue."""
    payload = json.loads(await mcp_server.import_paper("10.1145/3600000.3600001", wait_seconds=0))
    assert payload["status"] in {"pending", "queued", "running"}
    assert payload["pendingItems"] == 1
    assert "get_import" in payload["next"]
    import_id = payload["id"]

    # The continuation is immediate and reports the same job, still pending.
    again = json.loads(await mcp_server.get_import(import_id))
    assert again["id"] == import_id
    assert again["status"] in {"pending", "queued", "running"}
    assert again["next"].endswith("to continue.")

    # Leave no job behind for another test's worker.
    cancelled = json.loads(await mcp_server.cancel_import(import_id))
    assert cancelled["status"] in {"cancelled", "dead"}


async def test_batch_import_defaults_to_a_short_wait(mcp_api: None, drain_queue: None) -> None:
    payload = json.loads(
        await mcp_server.import_papers(["10.1145/3600000.3600002"], wait_seconds=0)
    )
    assert payload["status"] in {"pending", "queued", "running"}
    assert payload["pendingItems"] == 1
    await mcp_server.cancel_import(payload["id"])
