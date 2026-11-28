"""Update notices: cached detection, once-per-process delivery, injection.

The notice mechanism has three layers, each tested here:

* the cached check — TTL windows, 404-as-no-release, failure backoff, and
  the GitHub payload validation (version tag + adapter asset for some host);
* the notice decision — "latest > installed" against the env-provided
  installed version, delivered at most once per process per latest version;
* result injection — JSON objects gain an ``updateNotice`` field, other text
  gains a marked line, non-string results (images) are untouched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from paperdb import mcp_update
from paperdb.adapters import AdapterError


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """A private cache root, a pinned installed version, a fresh in-memory state."""
    monkeypatch.setenv("PAPERDB_MCP_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPERDB_MCP_ADAPTER_VERSION", "0.1.0")
    monkeypatch.setenv("PAPERDB_MCP_UPDATE_TTL", "3600")
    mcp_update.reset()
    yield
    mcp_update.reset()


def _write_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "checkedAt": datetime.now(UTC).isoformat(),
        "repo": "shaoyuanyu/ysy-paperdb",
        "latest": "9.9.9",
        "tag": "v9.9.9",
        "publishedAt": "2026-11-29T00:00:00Z",
        "page": "https://github.com/shaoyuanyu/ysy-paperdb/releases/tag/v9.9.9",
        "notes": "a big release",
    }
    entry.update(overrides)
    mcp_update.cache_path().write_text(json.dumps(entry), encoding="utf-8")
    return entry


async def _json_tool(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# configuration


def test_ttl_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mcp_update.ttl_seconds() == mcp_update.DEFAULT_TTL
    monkeypatch.setenv("PAPERDB_MCP_UPDATE_TTL", "60")
    assert mcp_update.ttl_seconds() == 60
    monkeypatch.setenv("PAPERDB_MCP_UPDATE_TTL", "0")
    assert mcp_update.ttl_seconds() == 0 and mcp_update.enabled() is False
    monkeypatch.setenv("PAPERDB_MCP_UPDATE_TTL", "not-a-number")
    assert mcp_update.ttl_seconds() == mcp_update.DEFAULT_TTL
    monkeypatch.setenv("PAPERDB_MCP_UPDATE_TTL", "-5")
    assert mcp_update.ttl_seconds() == 0


def test_installed_version_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp_update.reset()
    monkeypatch.setenv("PAPERDB_MCP_ADAPTER_VERSION", "1.2.3")
    assert mcp_update.installed_version() == "1.2.3"
    monkeypatch.setenv("PAPERDB_MCP_ADAPTER_VERSION", "")  # empty means unknown
    mcp_update.reset()
    resolved = mcp_update.installed_version()
    assert resolved is None or (isinstance(resolved, str) and resolved == "0.1.0")


# --------------------------------------------------------------------------- #
# notice decision


def test_notice_when_update_available(isolated: None) -> None:
    _write_entry(latest="0.2.0", tag="v0.2.0", page="https://github.com/x/releases/tag/v0.2.0")
    notice = mcp_update.pending_notice()
    assert notice is not None
    assert notice["installed"] == "0.1.0"
    assert notice["latest"] == "0.2.0"
    assert notice["page"].endswith("/v0.2.0")
    assert "60 seconds" in notice["instructions"]


def test_notice_delivered_once_per_process(isolated: None) -> None:
    _write_entry(latest="0.2.0")
    assert mcp_update.pending_notice() is not None
    assert mcp_update.pending_notice() is None


def test_new_release_notifies_again(isolated: None) -> None:
    _write_entry(latest="0.2.0")
    assert mcp_update.pending_notice() is not None
    _write_entry(latest="0.3.0")  # a different latest re-arms the notice
    notice = mcp_update.pending_notice()
    assert notice is not None and notice["latest"] == "0.3.0"


def test_no_notice_when_up_to_date(isolated: None) -> None:
    _write_entry(latest="0.1.0")  # equal, not newer
    assert mcp_update.pending_notice() is None


def test_no_notice_when_installed_is_newer(isolated: None) -> None:
    _write_entry(latest="0.0.9")  # older than the installed 0.1.0
    assert mcp_update.pending_notice() is None


def test_no_notice_without_published_release(isolated: None) -> None:
    _write_entry(latest=None)  # 404: valid, cacheable "no update"
    assert mcp_update.pending_notice() is None


def test_no_notice_on_error_entry(isolated: None) -> None:
    _write_entry(error="ConnectError: offline")
    assert mcp_update.pending_notice() is None


def test_no_notice_when_disabled(isolated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_entry(latest="9.9.9")
    monkeypatch.setenv("PAPERDB_MCP_UPDATE_TTL", "0")
    assert mcp_update.pending_notice() is None


def test_no_notice_with_unknown_installed_version(isolated: None) -> None:
    _write_entry(latest="9.9.9")
    saved = mcp_update._installed
    mcp_update._installed = None  # an unresolvable version: stay silent, don't guess
    try:
        assert mcp_update.pending_notice() is None
    finally:
        mcp_update._installed = saved


# --------------------------------------------------------------------------- #
# staleness and background refresh


async def test_stale_cache_schedules_one_refresh(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_fetch(client: httpx.AsyncClient) -> dict[str, Any]:
        calls.append({"latest": "0.2.0", "tag": "v0.2.0", "page": "https://x/v0.2.0"})
        return calls[-1]

    monkeypatch.setattr(mcp_update, "_fetch_latest", fake_fetch)
    old = (datetime.now(UTC) - timedelta(hours=8)).isoformat()
    _write_entry(latest="0.2.0", checkedAt=old)

    notice = mcp_update.pending_notice()  # stale: schedules a background refresh
    assert notice is not None  # stale data still answers this call
    assert mcp_update._task is not None
    # Two calls while the first refresh is pending schedule nothing extra.
    mcp_update.pending_notice()
    await mcp_update._task
    entry = json.loads(mcp_update.cache_path().read_text(encoding="utf-8"))
    assert entry["latest"] == "0.2.0" and "error" not in entry
    assert len(calls) == 1


def test_fresh_cache_does_not_refresh(isolated: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_entry(latest="0.2.0")
    called = []

    async def boom(client: httpx.AsyncClient) -> dict[str, Any]:
        called.append(1)
        raise AssertionError("must not be called")

    monkeypatch.setattr(mcp_update, "_fetch_latest", boom)
    mcp_update.pending_notice()
    assert called == []


def test_failed_check_backs_off_shorter_than_ttl(isolated: None) -> None:
    ttl = mcp_update.ttl_seconds()
    now = datetime.now(UTC)
    fresh_error = {"checkedAt": now.isoformat(), "error": "x"}
    stale_soon = {
        "checkedAt": (now - timedelta(seconds=mcp_update.FAILURE_TTL + 1)).isoformat(),
        "error": "x",
    }
    assert mcp_update._fresh(fresh_error, ttl) is True
    assert mcp_update._fresh(stale_soon, ttl) is False
    # A success entry from the same moment is still fresh at the full TTL.
    fresh_success = {"checkedAt": (now - timedelta(seconds=mcp_update.FAILURE_TTL + 1)).isoformat()}
    assert mcp_update._fresh(fresh_success, ttl) is True


async def test_refresh_caches_error_on_failure(
    isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(client: httpx.AsyncClient) -> dict[str, Any]:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(mcp_update, "_fetch_latest", boom)
    entry = await mcp_update.refresh()
    assert "ConnectError" in entry["error"]
    cached = json.loads(mcp_update.cache_path().read_text(encoding="utf-8"))
    assert cached["error"] == entry["error"]


# --------------------------------------------------------------------------- #
# the GitHub payload check


def _github_payload(tag: str, assets: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "tag_name": tag,
            "published_at": "2026-11-29T00:00:00Z",
            "html_url": f"https://github.com/x/releases/tag/{tag}",
            "body": "release notes",
            "assets": [
                {"name": name, "browser_download_url": f"https://x/{name}"} for name in assets
            ],
        }
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def _fetch(transport: httpx.MockTransport) -> dict[str, Any]:
    async with httpx.AsyncClient(transport=transport) as client:
        return await mcp_update._fetch_latest(client)


async def test_fetch_latest_accepts_release_with_adapter_asset() -> None:
    fields = await _fetch(
        _github_payload("v0.2.0", ["paperdb-adapter-dsh-0.2.0.tar.gz", "SHA256SUMS"])
    )
    assert fields["latest"] == "0.2.0"
    assert fields["tag"] == "v0.2.0"
    assert fields["page"].endswith("/v0.2.0")
    assert fields["notes"] == "release notes"


async def test_fetch_latest_rejects_release_without_adapter_asset() -> None:
    with pytest.raises(AdapterError, match="no adapter asset"):
        await _fetch(_github_payload("v0.2.0", ["source.tar.gz"]))


async def test_fetch_latest_rejects_non_version_tag() -> None:
    with pytest.raises(AdapterError, match="not a version"):
        await _fetch(_github_payload("release-v2026.11.29-ddf021d", []))


async def test_fetch_latest_maps_404_to_no_release() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    fields = await _fetch(httpx.MockTransport(handler))
    assert fields["latest"] is None


# --------------------------------------------------------------------------- #
# result injection


async def test_notice_injected_into_json_object(isolated: None) -> None:
    _write_entry(latest="0.2.0")

    async def get_paper(ref: str) -> str:
        """Get one paper."""
        return json.dumps({"id": ref, "title": "A Paper"}, ensure_ascii=False)

    wrapped = mcp_update.wrap_tool(get_paper)
    out = await wrapped(ref="p1")
    payload = json.loads(out)  # still valid JSON, same tool contract
    assert payload["id"] == "p1" and payload["title"] == "A Paper"
    assert payload["updateNotice"]["latest"] == "0.2.0"
    assert payload["updateNotice"]["installed"] == "0.1.0"
    # The wrapper preserves the schema surface FastMCP introspects.
    assert wrapped.__name__ == "get_paper" and wrapped.__doc__ == "Get one paper."
    assert await wrapped(ref="p2") == json.dumps({"id": "p2", "title": "A Paper"})


async def test_notice_appended_to_non_object_result(isolated: None) -> None:
    _write_entry(latest="0.2.0")

    async def list_files(paper_ref: str) -> str:
        """List files."""
        return json.dumps([{"id": "f1"}])

    wrapped = mcp_update.wrap_tool(list_files)
    out = await wrapped(paper_ref="p1")
    head, marker_line = out.split("\n[paperdb-update] ", 1)
    assert json.loads(head) == [{"id": "f1"}]
    assert json.loads(marker_line)["updateNotice"]["latest"] == "0.2.0"


async def test_non_string_result_is_untouched_and_notice_not_consumed(isolated: None) -> None:
    _write_entry(latest="0.2.0")

    class FakeImage:
        pass

    async def page_image(file_id: str) -> FakeImage:
        """Render a page."""
        return FakeImage()

    image_tool = mcp_update.wrap_tool(page_image)
    assert isinstance(await image_tool(file_id="f1"), FakeImage)

    # The pending notice survived the image call and rides the next text call.
    async def get_text(paper_ref: str) -> str:
        """Read text."""
        return json.dumps({"ok": True})

    text_tool = mcp_update.wrap_tool(get_text)
    assert "updateNotice" in json.loads(await text_tool(paper_ref="p1"))


async def test_wrapper_handles_sync_tools(isolated: None) -> None:
    _write_entry(latest="0.2.0")

    def whoami_sync() -> str:
        """Sync tool."""
        return json.dumps({"scopes": ["read"]})

    wrapped = mcp_update.wrap_tool(whoami_sync)
    payload = json.loads(await wrapped())
    assert payload["scopes"] == ["read"]
    assert payload["updateNotice"]["latest"] == "0.2.0"


async def test_result_untouched_without_notice(isolated: None) -> None:
    _write_entry(latest="0.1.0")  # up to date

    async def resolve(query: str) -> str:
        """Resolve."""
        return json.dumps({"input": query})

    wrapped = mcp_update.wrap_tool(resolve)
    assert await wrapped(query="q") == json.dumps({"input": "q"})
