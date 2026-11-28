"""Completeness guard: can the agent reach what a human can reach?

The web UI is a client of the same HTTP API, so "full agent access" means every
API operation is either wrapped by an MCP tool or explicitly excused here with
a reason. This test fails when a new endpoint lands and nobody wraps it — the
exact drift that made the agent unable to read papers or edit tags before.
"""

from __future__ import annotations

import os
import pathlib
import re

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
