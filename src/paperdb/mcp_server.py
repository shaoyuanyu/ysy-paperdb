"""paperdb MCP server: expose the literature-library backend to agents.

Transport: stdio (the standard for local agent tool servers). The server is a
thin, typed wrapper around the paperdb HTTP API — every tool maps to one (or a
short poll around one) API endpoint, so the backend remains the single source
of truth for validation, dedupe, venue normalization, and job logs.

Configuration (environment):
    PAPERDB_MCP_BASE_URL  base URL of the paperdb API (default http://127.0.0.1:9720)
    PAPERDB_MCP_TOKEN     Bearer token (service or personal token)

Run:  uv run paperdb-mcp        (stdio; launch from the agent harness)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

_BASE_URL = os.environ.get("PAPERDB_MCP_BASE_URL", "http://127.0.0.1:9720").rstrip("/")
_TOKEN = os.environ.get("PAPERDB_MCP_TOKEN", "")

mcp = FastMCP(
    "paperdb",
    instructions=(
        "Literature library tools (paperdb). Resolve is read-only and safe; "
        "imports write to the library and are deduplicated automatically. "
        "When unsure whether a query matches the intended paper, resolve "
        "first and inspect the candidate (title/venue/authors/duplicateOf)."
    ),
)


def _client() -> httpx.AsyncClient:
    if not _TOKEN:
        raise RuntimeError(
            "PAPERDB_MCP_TOKEN is not set; configure the token for the paperdb API"
        )
    return httpx.AsyncClient(
        base_url=_BASE_URL,
        headers={"Authorization": f"Bearer {_TOKEN}"},
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=True,
    )


def _summarize_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail = body.get("detail") or body.get("title") or body
        return f"HTTP {response.status_code}: {detail}"
    except Exception:
        return f"HTTP {response.status_code}"


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    async with _client() as client:
        response = await client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(_summarize_error(response))
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _brief_paper(paper: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "citekey", "title", "kind", "year", "venue", "readingStatus", "starred")
    brief = {key: paper.get(key) for key in keep if key in paper}
    if "venue" in paper and isinstance(paper["venue"], dict):
        brief["venue"] = {
            key: paper["venue"].get(key) for key in ("abbrev", "fullName", "ccf")
        }
    return brief


_TERMINAL = {"succeeded", "failed", "cancelled", "dead"}


async def _wait_for_import(import_id: str, timeout_s: float = 90.0) -> dict[str, Any]:
    """Poll an import job until terminal, then return job + items."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    job: dict[str, Any] = {}
    while True:
        job = await _request("GET", f"/v1/imports/{import_id}")
        if job.get("status") in _TERMINAL:
            break
        if asyncio.get_event_loop().time() > deadline:
            job["note"] = f"still {job.get('status')} after {timeout_s:.0f}s; poll get_import"
            return job
        await asyncio.sleep(0.8)
    items = await _request("GET", f"/v1/imports/{import_id}/items")
    out: dict[str, Any] = dict(job)
    out["items"] = [
        {
            "rawInput": item.get("rawInput"),
            "status": item.get("status"),
            "paperId": item.get("paperId"),
            "error": item.get("error"),
        }
        for item in items
    ]
    if out.get("status") == "succeeded" and len(items) == 1 and items[0].get("paperId"):
        paper = await _request("GET", f"/v1/papers/{items[0]['paperId']}")
        out["paper"] = _brief_paper(paper)
    return out


@mcp.tool()
async def resolve(query: str) -> str:
    """Resolve a paper reference without writing anything.

    Accepts a DOI, arXiv ID, DBLP/ACL/OpenReview URL, a plain paper title, or
    any paper landing-page URL (conference/journal/repository page; the page
    is fetched and mined for identifiers/title/PDF links).

    Returns the best candidate: title, authors, year, venue (with CCF/CAS
    evaluation when the venue is known), identifiers, open-access pdfUrls,
    duplicateOf (when the paper is already in the library), and warnings.
    """
    result = await _request("POST", "/v1/resolve", json={"input": query})
    candidates = result.get("candidates") or []
    if not candidates:
        return json.dumps(
            {"input": result.get("input"), "warnings": result.get("warnings")},
            ensure_ascii=False,
        )
    candidate = candidates[0]
    candidate["warnings"] = result.get("warnings") or candidate.get("warnings")
    return json.dumps(candidate, ensure_ascii=False)


@mcp.tool()
async def import_paper(
    query: str,
    tags: list[str] | None = None,
    collection_id: str | None = None,
    on_duplicate: str = "link",
) -> str:
    """Import one paper into the library (identifier, title, or landing-page URL).

    The query is resolved through the metadata sources; duplicates are
    detected by identifiers (linked, never re-created, and newly resolved
    identifiers are merged into the existing entry) and by strong fuzzy
    matches (exact title + year + first author) when on_duplicate is "link".
    Optional hierarchical tag paths (e.g. "topic/llm/rag") and a collection
    are applied on import. on_duplicate: "link" (default) also links strong
    fuzzy matches; "skip" only links identifier duplicates without merging;
    "create" always creates a new entry (use when the user deliberately
    wants to keep two versions as separate entries). Waits for the import
    job and returns the final per-item status plus the imported paper.
    """
    valid = on_duplicate if on_duplicate in {"link", "skip", "create"} else "link"
    options: dict[str, Any] = {"onDuplicate": valid}
    if tags:
        options["defaultTags"] = tags
    if collection_id:
        options["defaultCollectionId"] = collection_id
    created = await _request(
        "POST", "/v1/imports/identifiers", json={"items": [query], "options": options}
    )
    return json.dumps(
        await _wait_for_import(created["importId"]), ensure_ascii=False
    )


@mcp.tool()
async def import_paper_with_pdf(url: str, filename: str | None = None) -> str:
    """Import a paper AND its PDF from a landing-page or direct-PDF URL.

    Resolves the paper from the page (citation meta tags / title search),
    downloads the first working PDF link on the page, and stores both. When no
    PDF is reachable, the paper is still imported (metadata-only) with a
    warning. Re-running with the same URL links to the existing paper.
    """
    payload: dict[str, Any] = {"url": url}
    if filename:
        payload["filename"] = filename
    result = await _request("POST", "/v1/imports/pdf-url", json=payload)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def import_bibtex(bibtex: str) -> str:
    """Import papers from a BibTeX string (up to 500 entries).

    Entries carrying a DOI/arXiv ID are re-resolved against the metadata
    sources for authoritative fields; citekeys are preserved. Waits for the
    import job and returns per-item statuses.
    """
    files = {"file": ("import.bib", bibtex.encode("utf-8"), "application/x-bibtex")}
    created = await _request("POST", "/v1/imports/bibtex", files=files)
    return json.dumps(await _wait_for_import(created["importId"]), ensure_ascii=False)


@mcp.tool()
async def get_paper(ref: str) -> str:
    """Get one paper by id, citekey, or DOI. Includes venue with CCF/CAS
    evaluation, authors, identifiers, files, and reading state."""
    result = await _request("GET", f"/v1/papers/{ref}")
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def search_papers(q: str, limit: int = 20) -> str:
    """Search the library by title/author/citekey. Returns brief papers."""
    result = await _request("GET", "/v1/papers", params={"q": q, "limit": limit})
    papers = [_brief_paper(item) for item in result.get("items", [])]
    payload = {"papers": papers, "nextCursor": result.get("nextCursor")}
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
async def add_tags(paper_ref: str, paths: list[str]) -> str:
    """Add hierarchical tag paths (e.g. "topic/security", "status/to-read") to
    a paper (by id, citekey, or DOI). Missing tag nodes are created."""
    result = await _request("POST", f"/v1/papers/{paper_ref}/tags", json={"paths": paths})
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def add_to_collection(paper_ref: str, collection_id: str) -> str:
    """Add a paper to a collection (by collection id; see list_collections)."""
    result = await _request(
        "POST", f"/v1/papers/{paper_ref}/collections", json={"collectionIds": [collection_id]}
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def list_collections() -> str:
    """List the collection tree (id, name, path) to pick a collection id."""
    result = await _request("GET", "/v1/collections/tree")
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_import(import_id: str) -> str:
    """Get an import job's status and per-item results (poll while pending)."""
    return json.dumps(await _wait_for_import(import_id, timeout_s=0.1), ensure_ascii=False)


def main() -> None:
    if not _TOKEN:
        print("paperdb-mcp: PAPERDB_MCP_TOKEN is not set", file=sys.stderr)
        raise SystemExit(1)
    mcp.run()


if __name__ == "__main__":
    main()
