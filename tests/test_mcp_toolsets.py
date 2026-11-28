"""Tool groups: the tool table is loaded by workflow, not as one flat list.

Two layers are tested here:

* the registry itself — every declared tool belongs to exactly one group, the
  default groups are the per-paper management loop, and unknown groups fail
  loudly instead of silently loading everything;
* the real MCP handshake over stdio — a narrow session starts small, and
  ``toolset(op="enable")`` grows the tool table and notifies the client.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ToolListChangedNotification

from paperdb import mcp_server
from paperdb import mcp_toolsets as toolsets

REPO_ROOT = Path(__file__).resolve().parents[1]


def _json(raw: str) -> dict:
    return json.loads(raw)


def _text(result: object) -> str:
    """The text content of a CallToolResult (schema-typed as a union)."""
    for item in result.content:  # type: ignore[attr-defined]
        value = getattr(item, "text", None)
        if isinstance(value, str):
            return value
    raise AssertionError("tool result carried no text content")


def test_every_declared_tool_belongs_to_exactly_one_group() -> None:
    report = toolsets.report()
    listed = [(group["name"], name) for group in report["groups"] for name in group["tools"]]
    names = [name for _, name in listed]
    assert len(names) == len(set(names)) == 93
    assert report["total"] == 93
    assert set(report["groups"][0]) == {"name", "description", "enabled", "tools"}


def test_default_groups_are_the_management_loop() -> None:
    assert toolsets.resolve_groups(None) == ("import", "read", "organize")
    assert toolsets.resolve_groups("") == ("import", "read", "organize")
    assert toolsets.resolve_groups(" all ") == tuple(toolsets.GROUPS)
    assert toolsets.resolve_groups("read, import ,read") == ("read", "import")


def test_unknown_group_is_an_error_not_a_silent_full_load() -> None:
    with pytest.raises(toolsets.ToolsetError, match="unknown toolset"):
        toolsets.resolve_groups("read,everything")
    with pytest.raises(toolsets.ToolsetError, match="unknown toolset"):
        toolsets.enable(["everything"])
    with pytest.raises(toolsets.ToolsetError, match="no group"):
        toolsets.enable([])
    with pytest.raises(toolsets.ToolsetError, match="unknown toolset"):
        toolsets.tool(group="nope")


def test_session_starts_with_the_default_groups() -> None:
    registered = set(mcp_server.mcp._tool_manager._tools)
    assert len(registered) == 62  # import 23 + read 13 + organize 25 + toolset
    assert "toolset" in registered
    assert "resolve" in registered and "get_paper_text" in registered
    # Not loaded by default: workflow and library.
    assert "create_view" not in registered
    assert "list_authors" not in registered
    assert "create_venue" not in registered
    # Venue lookup stays with import: picking a venue_id is part of repair.
    assert "list_venues" in registered


async def test_toolset_lists_groups_without_side_effects() -> None:
    before = set(mcp_server.mcp._tool_manager._tools)
    report = _json(await mcp_server.toolset(op="list"))
    assert report["ok"] is True
    # "registered" counts group tools; the always-on toolset tool is extra.
    assert report["registered"] == len(toolsets.registered_tools())
    assert len(before) == report["registered"] + 1
    assert {group["name"] for group in report["groups"]} == set(toolsets.GROUPS)
    assert [group["name"] for group in report["groups"] if group["enabled"]] == [
        "import",
        "read",
        "organize",
    ]
    assert set(mcp_server.mcp._tool_manager._tools) == before

    bad = _json(await mcp_server.toolset(op="drop"))
    assert bad["ok"] is False and "unknown op" in bad["error"]


async def test_stdio_session_can_load_more_groups() -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PAPERDB_MCP_TOKEN": "test-token",
        "PAPERDB_MCP_TOOLSETS": "import,read",
    }
    notifications: list[object] = []

    async def handler(message: object) -> None:
        notifications.append(getattr(message, "root", message))

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "paperdb.mcp_server"],
        env=env,
        cwd=str(REPO_ROOT),
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write, message_handler=handler) as session,
    ):
        await session.initialize()
        initial = await session.list_tools()
        names = {tool.name for tool in initial.tools}
        assert len(names) == 37  # import 23 + read 13 + toolset
        assert "create_view" not in names

        loaded = _json(
            _text(await session.call_tool("toolset", {"op": "enable", "groups": ["workflow"]}))
        )
        assert loaded["ok"] is True
        assert loaded["addedGroups"] == ["workflow"]
        assert "create_view" in loaded["addedTools"]

        grown = {tool.name for tool in (await session.list_tools()).tools}
        assert len(grown) == 63  # +26 workflow
        assert {"create_view", "list_workspaces", "apply_rule"} <= grown

    assert any(isinstance(item, ToolListChangedNotification) for item in notifications)
