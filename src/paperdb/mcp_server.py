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
        "first and inspect the candidate (title/venue/authors/duplicateOf). "
        "These tools also serve paper-lookup workflows: after presenting "
        "search results to the user, offer to import some or all of them "
        "(one request for identifier batches; parallel calls for paper-page "
        "URLs). For batches the user may not keep, ask whether to file them "
        "into a collection or into a research workspace (staging); recommend "
        "the workspace for exploratory deep research, and promote/discard at "
        "the end."
    ),
)


def _client() -> httpx.AsyncClient:
    if not _TOKEN:
        raise RuntimeError("PAPERDB_MCP_TOKEN is not set; configure the token for the paperdb API")
    return httpx.AsyncClient(
        base_url=_BASE_URL,
        headers={"Authorization": f"Bearer {_TOKEN}"},
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=True,
    )


def _summarize_error(response: httpx.Response, path: str) -> str:
    try:
        body = response.json()
        detail = body.get("detail") or body.get("title") or body
        return f"HTTP {response.status_code} for {path}: {detail}"
    except Exception:
        text = (response.text or "").strip()[:300]
        return f"HTTP {response.status_code} for {path}: {text or 'empty body'}"


async def _request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    raw: bool = False,
    **kwargs: Any,
) -> Any:
    try:
        async with _client() as client:
            response = await client.request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        # Never let an empty exception message reach the agent: without the
        # method/path/type an import failure is undiagnosable.
        detail = str(exc) or "no detail"
        raise RuntimeError(f"{method} {path} failed: {type(exc).__name__}: {detail}") from exc
    if response.status_code >= 400:
        raise RuntimeError(_summarize_error(response, path))
    if response.status_code == 204 or not response.content:
        return None
    if raw:
        return response.text
    return response.json()


def _brief_paper(paper: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "id",
        "citekey",
        "title",
        "kind",
        "year",
        "venue",
        "evaluation",
        "tags",
        "collections",
        "readingStatus",
        "starred",
    )
    brief = {key: paper.get(key) for key in keep if key in paper}
    if "venue" in paper and isinstance(paper["venue"], dict):
        brief["venue"] = {key: paper["venue"].get(key) for key in ("abbrev", "fullName", "ccf")}
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
    workspace_id: str | None = None,
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
    if workspace_id:
        options["defaultWorkspaceId"] = workspace_id
    created = await _request(
        "POST", "/v1/imports/identifiers", json={"items": [query], "options": options}
    )
    return json.dumps(await _wait_for_import(created["importId"]), ensure_ascii=False)


@mcp.tool()
async def import_paper_with_pdf(
    url: str,
    filename: str | None = None,
    tags: list[str] | None = None,
    collection_id: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """Import a paper AND its PDF from a landing-page or direct-PDF URL.

    Resolves the paper from the page (citation meta tags / title search),
    downloads the first working PDF link on the page, and stores both. When no
    PDF is reachable, the paper is still imported (metadata-only) with a
    warning. Re-running with the same URL links to the existing paper.

    tags/collection_id are applied after the import (the backend import path
    does not take them yet). To add a PDF to a paper that is already in the
    library, use attach_pdf instead — it can never create a duplicate entry.
    """
    payload: dict[str, Any] = {"url": url}
    if filename:
        payload["filename"] = filename
    result = await _request("POST", "/v1/imports/pdf-url", json=payload)
    paper_id = result.get("paperId")
    if paper_id and (tags or collection_id):
        await _apply_defaults(paper_id, tags, collection_id)
        result["tagsApplied"] = tags or []
        result["collectionApplied"] = collection_id
    if paper_id and workspace_id:
        await _request(
            "POST",
            f"/v1/workspaces/{workspace_id}/papers",
            json={"paperRefs": [paper_id]},
        )
        result["workspaceId"] = workspace_id
    return json.dumps(result, ensure_ascii=False)


async def _apply_defaults(paper_id: str, tags: list[str] | None, collection_id: str | None) -> None:
    if tags:
        await _request("POST", f"/v1/papers/{paper_id}/tags", json={"paths": tags})
    if collection_id:
        await _request(
            "POST", f"/v1/papers/{paper_id}/collections", json={"collectionIds": [collection_id]}
        )


@mcp.tool()
async def import_papers(
    items: list[str],
    tags: list[str] | None = None,
    collection_id: str | None = None,
    workspace_id: str | None = None,
    on_duplicate: str = "link",
    timeout_s: float = 180.0,
) -> str:
    """Import several papers in ONE job (identifiers, titles, or page URLs).

    Use this instead of calling import_paper in a loop: the backend resolves
    and dedupes items sequentially inside a single import job, so identifier
    merges cannot race each other. Accepts up to 100 items; waits for the job
    and returns per-item statuses.
    """
    cleaned = [item.strip() for item in items if item and item.strip()]
    if not cleaned:
        return json.dumps({"error": "items is empty"})
    if len(cleaned) > 100:
        return json.dumps({"error": "at most 100 items per call"})
    options: dict[str, Any] = {
        "onDuplicate": on_duplicate if on_duplicate in {"link", "skip", "create"} else "link"
    }
    if tags:
        options["defaultTags"] = tags
    if collection_id:
        options["defaultCollectionId"] = collection_id
    if workspace_id:
        options["defaultWorkspaceId"] = workspace_id
    created = await _request(
        "POST", "/v1/imports/identifiers", json={"items": cleaned, "options": options}
    )
    return json.dumps(
        await _wait_for_import(created["importId"], timeout_s=timeout_s), ensure_ascii=False
    )


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
async def update_paper(
    paper_ref: str,
    venue_id: str | None = None,
    title: str | None = None,
    abstract: str | None = None,
    year: int | None = None,
    kind: str | None = None,
    citekey: str | None = None,
) -> str:
    """Correct one paper's metadata in place — the repair path after import.

    Pass only the fields that should change. venue_id (from list_venues) is how
    a provisional "Proceedings ..." venue is replaced by the canonical
    conference, which also restores the paper's CCF/CAS evaluation. Note that
    kind is one of conference/journal/preprint/arxiv/techreport/thesis/book;
    clearing a field (e.g. abstract) is done by passing an empty string.
    """
    payload: dict[str, Any] = {}
    if venue_id is not None:
        payload["venueId"] = venue_id
    if title is not None:
        payload["title"] = title
    if abstract is not None:
        payload["abstract"] = abstract
    if year is not None:
        payload["year"] = year
    if kind is not None:
        payload["kind"] = kind
    if citekey is not None:
        payload["citekey"] = citekey
    if not payload:
        return json.dumps({"error": "nothing to update"})
    result = await _request("PATCH", f"/v1/papers/{paper_ref}", json=payload)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def attach_pdf(paper_ref: str, url: str, filename: str | None = None) -> str:
    """Attach a PDF to a paper that is already in the library (by direct URL).

    Unlike import_paper_with_pdf, this never creates or resolves a paper, so it
    cannot produce a duplicate entry. Use it to repair metadata-only imports.
    """
    payload: dict[str, Any] = {"url": url}
    if filename:
        payload["filename"] = filename
    result = await _request("POST", f"/v1/papers/{paper_ref}/files/from-url", json=payload)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def list_files(paper_ref: str) -> str:
    """List a paper's stored PDFs (id, filename, size, primary, text status)."""
    result = await _request("GET", f"/v1/papers/{paper_ref}/files")
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def list_venues(q: str, limit: int = 10) -> str:
    """Search venues (conferences/journals) by name or abbreviation.

    Returns id/fullName/abbrev/kind plus CCF/CAS evaluation when available;
    pass a returned id to update_paper(venue_id=...) to fix a wrong venue.
    """
    result = await _request("GET", "/v1/venues", params={"q": q, "limit": limit})
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
async def list_workspaces(include_closed: bool = False) -> str:
    """List research workspaces (staging areas) with paper/pending counts.

    A research workspace is a peer of collections/projects, but its papers are
    NOT in the library yet: they are hidden from list_papers/search until
    promoted. Use it for batch research the user may not keep.
    """
    result = await _request("GET", "/v1/workspaces", params={"include_closed": include_closed})
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def create_workspace(name: str, description: str | None = None) -> str:
    """Create a research workspace (staging area) for a batch of papers.

    Propose a descriptive name such as "2026-11 agent-jailbreak survey"; names
    are unique among open workspaces.
    """
    payload: dict[str, Any] = {"name": name}
    if description:
        payload["description"] = description
    result = await _request("POST", "/v1/workspaces", json=payload)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_workspace(workspace_id: str, pending_only: bool = False) -> str:
    """Get a workspace with its papers (citekey/title/year/promoted)."""
    result = await _request(
        "GET", f"/v1/workspaces/{workspace_id}", params={"pending_only": pending_only}
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def add_to_workspace(workspace_id: str, paper_refs: list[str]) -> str:
    """Add existing papers to a workspace and mark them staged (hidden).

    Prefer importing with import_papers(workspace_id=...) for new papers; this
    is for papers already in the library/staging.
    """
    result = await _request(
        "POST",
        f"/v1/workspaces/{workspace_id}/papers",
        json={"paperRefs": paper_refs},
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def promote_workspace_papers(
    workspace_id: str, paper_refs: list[str], collection_id: str | None = None
) -> str:
    """Move papers from staging into the library (optionally into a collection).

    Use after the user has reviewed a research workspace and chosen what to
    keep. Promoting clears the staged flag so the paper appears in list_papers.
    """
    payload: dict[str, Any] = {"paperRefs": paper_refs}
    if collection_id:
        payload["collectionId"] = collection_id
    result = await _request("POST", f"/v1/workspaces/{workspace_id}/promote", json=payload)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def discard_workspace_papers(workspace_id: str, paper_refs: list[str]) -> str:
    """Discard papers from a research workspace (staged ones are soft-deleted)."""
    result = await _request(
        "POST",
        f"/v1/workspaces/{workspace_id}/discard",
        json={"paperRefs": paper_refs},
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def create_collection(
    name: str, parent_id: str | None = None, description: str | None = None
) -> str:
    """Create a collection (file folder) when the user wants a new one.

    Use after the user chooses "keep in a collection" and no existing
    collection fits (list_collections shows the current tree).
    """
    payload: dict[str, Any] = {"name": name}
    if parent_id:
        payload["parentId"] = parent_id
    if description:
        payload["description"] = description
    result = await _request("POST", "/v1/collections", json=payload)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def close_workspace(workspace_id: str, discard_unpromoted: bool = False) -> str:
    """Close a research workspace once its papers are promoted or discarded.

    With discard_unpromoted=True, papers still pending are dropped (staged
    ones are soft-deleted); promoted papers stay in the library.
    """
    result = await _request(
        "POST",
        f"/v1/workspaces/{workspace_id}/close",
        json={"discardUnpromoted": discard_unpromoted},
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_import(import_id: str) -> str:
    """Get an import job's status and per-item results (poll while pending)."""
    return json.dumps(await _wait_for_import(import_id, timeout_s=0.1), ensure_ascii=False)


@mcp.tool()
async def list_papers(
    q: str | None = None,
    collection_id: str | None = None,
    tag: str | None = None,
    venue_id: str | None = None,
    kind: str | None = None,
    year: int | None = None,
    status: str | None = None,
    starred: bool | None = None,
    unfiled: bool | None = None,
    workspace_id: str | None = None,
    include_staged: bool = False,
    limit: int = 20,
    cursor: str | None = None,
) -> str:
    """Browse the library like the web UI: filter papers by search text,
    collection, tag path, venue, kind (conference/journal/preprint/...), year,
    reading status, starred, or unfiled. Returns brief papers + nextCursor
    for pagination."""
    params: dict[str, Any] = {"limit": limit}
    if q:
        params["q"] = q
    if collection_id:
        params["collection"] = collection_id
    if tag:
        params["tag"] = tag
    if venue_id:
        params["venueId"] = venue_id
    if kind:
        params["kind"] = kind
    if year:
        params["year"] = year
    if status:
        params["status"] = status
    if starred is not None:
        params["starred"] = starred
    if unfiled is not None:
        params["unfiled"] = unfiled
    if workspace_id:
        params["workspace"] = workspace_id
    if include_staged:
        params["includeStaged"] = True
    if cursor:
        params["cursor"] = cursor
    result = await _request("GET", "/v1/papers", params=params)
    papers = [_brief_paper(item) for item in result.get("items", [])]
    payload = {"papers": papers, "nextCursor": result.get("nextCursor")}
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
async def list_tags() -> str:
    """List the full hierarchical tag tree (namespace paths + paper counts)."""
    result = await _request("GET", "/v1/tags/tree")
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def list_projects() -> str:
    """List project workspaces (name, description, collections, paper count)."""
    result = await _request("GET", "/v1/projects")
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_project(project_id: str) -> str:
    """Get one project with its papers (project detail + paper list)."""
    detail = await _request("GET", f"/v1/projects/{project_id}")
    papers = await _request("GET", f"/v1/projects/{project_id}/papers", params={"limit": 100})
    brief = [_brief_paper(item) for item in papers.get("items", [])]
    payload = dict(detail)
    payload["papers"] = brief
    payload["papersNextCursor"] = papers.get("nextCursor")
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
async def get_citations(paper_ref: str, kind: str = "references") -> str:
    """Get a paper's citation graph: 'references' (papers it cites) or
    'citations' (papers citing it). Library entries are resolved; external
    works are returned as stubs."""
    if kind not in {"references", "citations"}:
        return json.dumps({"error": "kind must be 'references' or 'citations'"})
    result = await _request("GET", f"/v1/papers/{paper_ref}/{kind}")
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def search_fulltext(q: str, limit: int = 20) -> str:
    """Full-text search inside stored PDFs: returns paper, file, page number,
    and a text snippet per hit."""
    result = await _request("GET", "/v1/search/fulltext", params={"q": q, "limit": limit})
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def set_reading_state(
    paper_ref: str,
    status: str | None = None,
    starred: bool | None = None,
    priority: int | None = None,
    rating: int | None = None,
    progress_page: int | None = None,
) -> str:
    """Update a paper's reading state: status
    (unread/queued/reading/read/skimmed), starred, priority 0-3, rating 1-5,
    progress page."""
    payload: dict[str, Any] = {}
    if status is not None:
        payload["status"] = status
    if starred is not None:
        payload["starred"] = starred
    if priority is not None:
        payload["priority"] = priority
    if rating is not None:
        payload["rating"] = rating
    if progress_page is not None:
        payload["progressPage"] = progress_page
    result = await _request("PATCH", f"/v1/papers/{paper_ref}/state", json=payload)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_paper_bibtex(ref: str) -> str:
    """Get a paper's BibTeX entry (the same string used by .bib exports)."""
    result = await _request("GET", f"/v1/papers/{ref}/bibtex", raw=True)
    return json.dumps({"bibtex": result}, ensure_ascii=False)


@mcp.tool()
async def delete_paper(paper_ref: str, totp_code: str) -> str:
    """Delete a paper (soft delete, restorable by the owner).

    DANGEROUS. Requires a current 6-digit TOTP code — the same code your
    authenticator shows for the site login. Ask the user for the code right
    before calling; codes expire after ~30 seconds. The call fails with
    'totp_required'/'totp_invalid' otherwise.
    """
    result = await _request(
        "DELETE",
        f"/v1/papers/{paper_ref}",
        headers={"X-TOTP-Code": totp_code.strip()},
    )
    return json.dumps({"deleted": paper_ref, "result": result or "204 no content"})


@mcp.tool()
async def merge_papers(source_ref: str, into_ref: str, totp_code: str) -> str:
    """Merge one paper into another (identifiers/authors/state are merged;
    the old id redirects; the source is soft-deleted).

    DANGEROUS. Requires a current 6-digit TOTP code from the user's
    authenticator (same as site login); ask right before calling.
    """
    result = await _request(
        "POST",
        f"/v1/papers/{source_ref}/merge",
        json={"into": into_ref, "delete_source": True},
        headers={"X-TOTP-Code": totp_code.strip()},
    )
    return json.dumps(
        {"merged": source_ref, "into": into_ref, "paper": _brief_paper(result)},
        ensure_ascii=False,
    )


def main() -> None:
    if not _TOKEN:
        print("paperdb-mcp: PAPERDB_MCP_TOKEN is not set", file=sys.stderr)
        raise SystemExit(1)
    mcp.run()


if __name__ == "__main__":
    main()
