"""Tool groups for the paperdb MCP server, and on-demand loading.

A single flat list of every tool is the wrong shape for a host: a session that
only imports and reads should not carry the ~90 tool schemas of the whole API.
Tools are therefore declared with a group, and only the groups a session needs
are registered:

* ``PAPERDB_MCP_TOOLSETS`` chooses the groups at startup (comma-separated, or
  ``all``). Unset means :data:`DEFAULT_GROUPS` — the per-paper management loop.
* the always-registered ``toolset`` tool lists the groups and enables more of
  them at runtime, so a session is never stuck with the set it started with.

Groups are workflows, not HTTP routers: an object's mutations live with the
object (``delete_paper`` is in ``organize``), so "what do I need for this task"
is answerable without reading the API surface.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module host-free
    from mcp.server.fastmcp import FastMCP

TOOLSETS_ENV = "PAPERDB_MCP_TOOLSETS"
ALL = "all"

#: Group -> what belongs in it.  The text is shown to the model by ``toolset``,
#: so it has to say when to reach for the group, not just name it.
GROUPS: dict[str, str] = {
    "import": (
        "入库与元数据: resolve / import* / PDF 挂载 / 导入作业观测 / 查重 / 富化 / "
        "手工建条目 / 单篇查询与修复"
    ),
    "read": (
        "阅读与检索: 正文与页图、章节大纲、全文检索、列表与统计、引用图、读批注、"
        "阅读状态、单篇 BibTeX"
    ),
    "organize": "整理一篇篇论文: 标签、集合、批注写入、批量修改、回收站、删除/合并、主 PDF",
    "workflow": "上层组织: 调研工作区、项目、智能视图、自动规则",
    "library": "字典维护: 作者与场馆记录, 含 CCF/CAS 评级查询与补建",
}

#: The per-paper management loop: enough to import, find, read and tidy up.
DEFAULT_GROUPS: tuple[str, ...] = ("import", "read", "organize")


class ToolsetError(ValueError):
    """An unknown group, or a group list that cannot be applied."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    group: str
    fn: Any


_specs: dict[str, ToolSpec] = {}
_server: FastMCP | None = None
_wrap: Callable[[Any], Any] | None = None
_enabled: set[str] = set()
_registered: set[str] = set()


def resolve_groups(raw: str | None) -> tuple[str, ...]:
    """Parse a ``PAPERDB_MCP_TOOLSETS`` value; unset means the default groups."""
    text = (raw or "").strip().lower()
    if not text:
        return DEFAULT_GROUPS
    if text == ALL:
        return tuple(GROUPS)
    names: list[str] = []
    for part in text.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    unknown = sorted(set(names) - set(GROUPS))
    if unknown:
        raise ToolsetError(f"unknown toolset(s): {', '.join(unknown)}; known: {', '.join(GROUPS)}")
    if not names:
        raise ToolsetError(f"{TOOLSETS_ENV} is empty; use {ALL} or a comma-separated group list")
    return tuple(names)


def bind(
    server: FastMCP,
    groups: Iterable[str] | None = None,
    wrap: Callable[[Any], Any] | None = None,
) -> None:
    """Attach the server and decide which groups start out loaded.

    ``wrap``, when given, is applied to every tool function at registration
    time.  The server uses it to decorate tool results (update notices)
    without the tool declarations — or this module — knowing about it; the
    wrapper preserves each function's name, docstring and signature, so the
    generated schemas are unchanged.
    """
    global _server, _wrap
    _server = server
    _wrap = wrap
    chosen = tuple(groups) if groups is not None else resolve_groups(os.environ.get(TOOLSETS_ENV))
    _enabled.update(chosen)


def tool(*, group: str):
    """Declare a tool in a group; register it now if the group is loaded."""
    if group not in GROUPS:
        raise ToolsetError(f"unknown toolset {group!r}; known: {', '.join(GROUPS)}")

    def decorate(fn: Any) -> Any:
        spec = ToolSpec(name=fn.__name__, group=group, fn=fn)
        _specs[spec.name] = spec
        if group in _enabled:
            _register(spec)
        return fn

    return decorate


def _register(spec: ToolSpec) -> None:
    if _server is None:  # pragma: no cover - bind() runs before any decorator
        raise ToolsetError("toolset.bind() must be called before tools are declared")
    if spec.name in _registered:
        return
    fn = _wrap(spec.fn) if _wrap is not None else spec.fn
    _server.add_tool(fn)
    _registered.add(spec.name)


def enabled_groups() -> tuple[str, ...]:
    return tuple(name for name in GROUPS if name in _enabled)


def registered_tools() -> tuple[str, ...]:
    return tuple(sorted(_registered))


def report() -> dict[str, Any]:
    """What a model needs to decide which group to load next."""
    groups = []
    for name, description in GROUPS.items():
        members = sorted(spec.name for spec in _specs.values() if spec.group == name)
        groups.append(
            {
                "name": name,
                "description": description,
                "enabled": name in _enabled,
                "tools": members,
            }
        )
    return {
        "ok": True,
        "enabled": list(enabled_groups()),
        "registered": len(_registered),
        "total": len(_specs),
        "groups": groups,
    }


def enable(groups: Sequence[str]) -> dict[str, Any]:
    """Load groups now; returns the tool names that became callable."""
    names = [name.strip().lower() for name in groups if name and name.strip()]
    unknown = sorted(set(names) - set(GROUPS))
    if unknown:
        raise ToolsetError(f"unknown toolset(s): {', '.join(unknown)}; known: {', '.join(GROUPS)}")
    if not names:
        raise ToolsetError("no group given")
    added: list[str] = []
    for name in names:
        if name in _enabled:
            continue
        _enabled.add(name)
        for spec in _specs.values():
            if spec.group == name:
                _register(spec)
                added.append(spec.name)
    return {
        "ok": True,
        "addedGroups": sorted(set(names)),
        "addedTools": sorted(added),
        "enabled": list(enabled_groups()),
        "registered": len(_registered),
        "total": len(_specs),
    }
