"""Completeness guard: can the agent reach what a human can reach?

The web UI is a client of the same HTTP API, so "full agent access" means every
API operation is either wrapped by an MCP tool or explicitly excused here with
a reason. This test fails when a new endpoint lands and nobody wraps it — the
exact drift that made the agent unable to read papers or edit tags before.
"""

from __future__ import annotations

import ast
import inspect
import os
import pathlib
import re

from paperdb import mcp_server
from paperdb.main import app

# Operations the agent deliberately does not get, each with the reason.
INTENTIONAL = {
    ("GET", "/v1/tokens"): "token management is human-only (secrets)",
    ("POST", "/v1/tokens"): "token management is human-only (secrets)",
    ("DELETE", "/v1/tokens/{}"): "token management is human-only (secrets)",
    ("GET", "/v1/export-tokens"): "export-token management is human-only (secrets)",
    ("POST", "/v1/export-tokens"): "export-token management is human-only (secrets)",
    ("DELETE", "/v1/export-tokens/{}"): "export-token management is human-only (secrets)",
    ("GET", "/v1/exports/bib/{}"): "public export URLs are human-only (secrets)",
    ("POST", "/v1/citations/sync"): "requires admin scope; agent tokens have none",
    (
        "POST",
        "/v1/totp/verify",
    ): "host adapter updates verify TOTP from the `paperdb adapter` CLI, not a tool",
    (
        "GET",
        "/v1/files/{}/content",
    ): "raw PDF bytes; agents read text via /text and image via /image",
    ("GET", "/v1/files/{}/pages/{}/text"): "superseded by the page-range /text endpoint",
    ("GET", "/v1/files/{}/search"): "superseded by search_text(scope='file:<id>')",
    ("GET", "/v1/files/{}"): "list_files returns the same metadata for all files",
    ("GET", "/v1/papers/{}/state"): "get_paper already includes reading state",
    ("GET", "/v1/papers/{}/citations"): "covered by get_citations(kind='citations')",
    ("GET", "/v1/papers/{}/references"): "covered by get_citations(kind='references')",
    ("GET", "/v1/views/{}"): "list_views returns every view with its count",
    ("GET", "/v1/views/{}/count"): "list_views returns every view with its count",
    ("GET", "/v1/workspaces/{}/papers"): "get_workspace includes the paper rows",
    ("DELETE", "/v1/papers/{}/collections"): "covered by bulk_update_papers(remove_collections)",
    ("GET", "/v1/meta"): "instance diagnostics, not library data",
    ("GET", "/v1/rank-datasets"): "instance diagnostics, not library data",
}


#: Calls whose path is assembled at runtime from a value set, not a literal.
#: ``get_citations`` dispatches to two real endpoints.
DYNAMIC_PATHS = {"/v1/papers/{}/{}": "get_citations(kind=...) -> /citations or /references"}


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path).rstrip("/")


def _mcp_operations() -> set[tuple[str, str]]:
    source = pathlib.Path("src/paperdb/mcp_server.py").read_text()
    flat = re.sub(r"\s+", " ", source)
    calls = re.findall(r'_request\(\s*"([A-Z]+)",\s*f?"(/v1/[^"]+)"', flat)
    return {(method, _norm(path)) for method, path in calls}


def _api_operations() -> set[tuple[str, str]]:
    os.environ.setdefault("PAPERDB_ENV", "test")
    schema = app.openapi()
    ops: set[tuple[str, str]] = set()
    for path, methods in schema["paths"].items():
        if not path.startswith("/v1"):
            continue
        for method in methods:
            ops.add((method.upper(), _norm(path)))
    return ops


def test_every_api_operation_is_reachable_or_excused() -> None:
    api = _api_operations()
    reached = _mcp_operations()
    uncovered = {
        op
        for op in api - reached
        # get_citations dispatches to /papers/{ref}/{kind} at runtime.
        if not (
            op[0] == "GET"
            and op[1].startswith("/v1/papers/{}/")
            and op[1].endswith(("citations", "references"))
        )
    }
    unexplained = uncovered - set(INTENTIONAL)
    assert not unexplained, (
        "These API operations have no MCP tool and no documented reason:\n"
        + "\n".join(f"  {method:6} {path}" for method, path in sorted(unexplained))
    )


def test_stale_excuses_are_removed() -> None:
    """An excuse for something that is actually covered is dead weight."""
    covered = _mcp_operations()
    stale = sorted(op for op in INTENTIONAL if op in covered)
    assert not stale, f"covered operations still listed as intentional: {stale}"


def _literal_path(node: ast.expr) -> str | None:
    """The path of a ``_request`` call, with f-string holes normalized to ``{}``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _norm(node.value)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return _norm("".join(parts))
    return None


def _file_operations() -> dict[str, set[tuple[str, str]]]:
    """Every API call in the server module, attributed to its function."""
    tree = ast.parse(pathlib.Path("src/paperdb/mcp_server.py").read_text())
    result: dict[str, set[tuple[str, str]]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls: set[tuple[str, str]] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            if not isinstance(inner.func, ast.Name):
                continue
            if inner.func.id not in {"_request", "_raw_request"} or len(inner.args) < 2:
                continue
            method = inner.args[0]
            if not (isinstance(method, ast.Constant) and isinstance(method.value, str)):
                continue
            path = _literal_path(inner.args[1])
            if path is not None:
                calls.add((method.value.upper(), path))
        if calls:
            result[node.name] = calls
    return result


def test_no_call_targets_an_endpoint_that_does_not_exist() -> None:
    """The other half of the contract: a tool may not invent a path.

    Without this, a typo in a manually mapped endpoint only shows up at
    runtime; with it, the OpenAPI schema is the single authority both ways.
    """
    api = _api_operations()
    unknown: list[str] = []
    for function, operations in sorted(_file_operations().items()):
        for method, path in sorted(operations):
            if (method, path) in api or (method, path) in {
                (m, p) for p, _ in DYNAMIC_PATHS.items() for m in ("GET", "POST")
            }:
                continue
            unknown.append(f"  {function}: {method} {path}")
    assert not unknown, "These calls target paths missing from the OpenAPI schema:\n" + "\n".join(
        unknown
    )


def _reaches_api(fn: object, seen: set[str] | None = None) -> bool:
    """True when a tool issues an API request, directly or via a module helper."""
    seen = seen or set()
    name = getattr(fn, "__name__", "")
    if name in seen:
        return False
    seen.add(name)
    try:
        source = inspect.getsource(fn)  # type: ignore[arg-type]
    except (OSError, TypeError):  # pragma: no cover - introspection failure
        return False
    if "_request(" in source or "_raw_request(" in source:
        return True
    tree = ast.parse(source.lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            helper = getattr(mcp_server, node.func.id, None)
            if callable(helper) and node.func.id.startswith("_") and _reaches_api(helper, seen):
                return True
    return False


def test_every_capability_tool_is_backed_by_the_api() -> None:
    """Host-local actions do not belong on the tool surface.

    An adapter installs this server; it is not a tool. A capability tool that
    never talks to paperdb would be exactly such a host concern leaking back
    in. ``toolset`` is the one deliberate exception: it is the always-on
    control tool that loads groups, and it is not part of ``_specs``.
    """
    local = [name for name, spec in mcp_server.toolsets._specs.items() if not _reaches_api(spec.fn)]
    assert not local, f"these tools never call the API: {local}"
