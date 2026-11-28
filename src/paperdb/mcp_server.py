"""paperdb MCP server: expose the literature-library backend to agents.

Transport: stdio (the standard for local agent tool servers). The server is a
thin, typed wrapper around the paperdb HTTP API — every tool maps to one (or a
short poll around one) API endpoint, so the backend remains the single source
of truth for validation, dedupe, venue normalization, and job logs.

The server is harness-neutral: it speaks plain MCP, reads only ``PAPERDB_MCP_*``
configuration, and knows nothing about which agent host launched it. Installing
this server into a particular host is the job of ``paperdb adapter`` (see
``paperdb.adapters``), not of a tool.

Configuration (environment):
    PAPERDB_MCP_BASE_URL  base URL of the paperdb API (default http://127.0.0.1:9720)
    PAPERDB_MCP_TOKEN     Bearer token (service or personal token)
    PAPERDB_MCP_TOKEN_FILE where to read the token from when the above is unset

Run:  uv run paperdb-mcp        (stdio; launch from any MCP-capable host)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sys
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import Context, FastMCP, Image

from paperdb import mcp_config, mcp_local, page_cache
from paperdb import mcp_toolsets as toolsets

_BASE_URL = mcp_config.base_url()
_TOKEN = mcp_config.load_token()

mcp = FastMCP(
    "paperdb",
    instructions=(
        "Literature library (paperdb). The owner uses this both from the web "
        "UI and through you, so prefer these tools over guessing metadata.\n"
        "\n"
        "Look up and import: resolve is read-only — check the candidate "
        "(title/venue/CCF/duplicateOf) before importing. Batch identifiers in "
        "one import_papers call; for exploratory batches ask whether results go "
        "into a collection or a research workspace (staging).\n"
        "\n"
        "Read: search_text locates (scope=library|workspace:<id>|paper:<ref>|"
        "file:<id>), get_paper_outline gives sections when the PDF has "
        "bookmarks, get_paper_text reads whole pages (100 by default), and "
        "get_page_image shows figures/tables/equations that text loses. Staged "
        "papers in a research workspace are readable too.\n"
        "\n"
        "Organize: tags, collections, projects, views (smart filters), "
        "auto-rules and annotations are all writable; bulk_update_papers edits "
        "many papers at once. Deletes (paper/tag/collection/project/view/file) "
        "and merges require the user's current 6-digit TOTP code — ask right "
        "before calling, never invent or reuse one.\n"
        "\n"
        "Tool groups: the tool table is loaded by workflow, not flat. This "
        "session starts with import + read + organize (per-paper management); "
        "the further groups workflow (research workspaces, projects, smart "
        "views, auto-rules) and library (authors, venues) are one call away — "
        'call toolset(op="enable", groups=[...]) and the tools it returns '
        'are immediately callable. toolset(op="list") shows every group, '
        "its description and its tools.\n"
        "\n"
        "Boundaries: local reads are confined to PDFs under ~/Downloads, "
        "~/Desktop, ~/Documents and the OS temp dir (extend with "
        "PAPERDB_MCP_LOCAL_ROOTS); "
        "the only local writes are a page-image cache under "
        "~/.cache/paperdb/mcp. Every library change goes through the API as the "
        "token's owner; deletes, merges and adapter writes need the user's "
        "current TOTP, so confirm intent before bulk or destructive edits.\n"
        "\n"
        "Installation and upgrades happen outside the conversation: the "
        "`paperdb adapter` CLI installs this server into a host, and no tool "
        "here writes that host's configuration.\n"
        "\n"
        "When unsure what the library holds, library_stats gives the overview "
        "(totals, unread spread, how much has an extracted PDF)."
    ),
)

toolsets.bind(mcp)


@mcp.tool()
async def toolset(
    op: str = "list",
    groups: list[str] | None = None,
    ctx: Context | None = None,
) -> str:
    """Discover tool groups, or load more of them into this session.

    The server exposes the whole library, but the tool table is loaded by
    workflow: a session starts with the groups it needs and adds others on
    demand, so a narrow task does not carry every schema.

    op="list"     -> every group with its description, enabled flag and tools
    op="enable"   -> load groups (e.g. ["workflow"]); the returned addedTools
                     are callable immediately, even if the host has not
                     refreshed its tool list yet.

    Groups: import (入出库与元数据), read (阅读/检索), organize (标签/集合/批注/
    批量/删除/合并), workflow (调研工作区/项目/视图/规则), library (作者/场馆).
    """
    try:
        if op == "enable":
            report = toolsets.enable(groups or [])
            if ctx is not None:
                # Best effort: a host is allowed not to support tool-list updates.
                with contextlib.suppress(Exception):
                    await ctx.session.send_tool_list_changed()
            return json.dumps(report, ensure_ascii=False)
        if op != "list":
            return json.dumps(
                {"ok": False, "error": f"unknown op {op!r}; use 'list' or 'enable'"},
                ensure_ascii=False,
            )
        return json.dumps(toolsets.report(), ensure_ascii=False)
    except toolsets.ToolsetError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def _client() -> httpx.AsyncClient:
    if not _TOKEN:
        raise RuntimeError(
            "paperdb MCP token is not configured: set PAPERDB_MCP_TOKEN or "
            "point PAPERDB_MCP_TOKEN_FILE at a file containing it"
        )
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
        # The machine-readable code (totp_required, ambiguous_scope, ...) is what
        # lets an agent decide what to do next; keep it in the message.
        code = body.get("code")
        prefix = f"HTTP {response.status_code}" + (f" {code}" if code else "")
        return f"{prefix} for {path}: {detail}"
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


async def _raw_request(
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    """Like ``_request`` but returns the response bytes (images, PDFs)."""
    try:
        async with _client() as client:
            response = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        detail = str(exc) or "no detail"
        raise RuntimeError(f"{method} {path} failed: {type(exc).__name__}: {detail}") from exc
    if response.status_code >= 400:
        raise RuntimeError(_summarize_error(response, path))
    return response


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


_IMPORT_WAIT_DEFAULT = 15.0
_IMPORT_WAIT_MAX = 60.0


def _wait_budget(wait_seconds: float | None) -> float:
    """Clamp a caller's wait budget; ``None`` means the tool default."""
    if wait_seconds is None:
        return _IMPORT_WAIT_DEFAULT
    try:
        value = float(wait_seconds)
    except (TypeError, ValueError):
        return _IMPORT_WAIT_DEFAULT
    if value != value:  # NaN
        return _IMPORT_WAIT_DEFAULT
    return max(0.0, min(value, _IMPORT_WAIT_MAX))


async def _wait_for_import(import_id: str, wait_seconds: float | None = None) -> dict[str, Any]:
    """Poll an import job for up to ``wait_seconds``, then return a usable snapshot.

    Never blocks until the host's tool timeout: the default budget is 15s and
    the ceiling is 60s, so a batch that is still running comes back with its
    per-item state and a ``next`` hint (``get_import``) instead of a killed
    tool call. ``wait_seconds=0`` returns immediately after one status read.
    """
    budget = _wait_budget(wait_seconds)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while True:
        job = await _request("GET", f"/v1/imports/{import_id}")
        if job.get("status") in _TERMINAL:
            break
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(0.5, remaining))

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
    out["pendingItems"] = sum(1 for item in items if item.get("status") == "pending")
    if out.get("status") not in _TERMINAL:
        out["next"] = (
            f"{out.get('status')} after a {budget:g}s wait; "
            f"{out['pendingItems']} item(s) still pending. "
            f'Call get_import(import_id="{import_id}") to continue.'
        )
    elif out.get("status") == "succeeded" and len(items) == 1 and items[0].get("paperId"):
        paper = await _request("GET", f"/v1/papers/{items[0]['paperId']}")
        out["paper"] = _brief_paper(paper)
    return out


@toolsets.tool(group="import")
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


@toolsets.tool(group="import")
async def import_paper(
    query: str,
    tags: list[str] | None = None,
    collection_id: str | None = None,
    workspace_id: str | None = None,
    on_duplicate: str = "link",
    wait_seconds: float | None = None,
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
    wants to keep two versions as separate entries).

    Waits up to ``wait_seconds`` (default 15, max 60; 0 returns immediately)
    for the job; a single successful import also returns the paper. If the job
    is still running, the result carries `next` — continue with get_import.
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
    return json.dumps(await _wait_for_import(created["importId"], wait_seconds), ensure_ascii=False)


@toolsets.tool(group="import")
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


@toolsets.tool(group="import")
async def import_papers(
    items: list[str],
    tags: list[str] | None = None,
    collection_id: str | None = None,
    workspace_id: str | None = None,
    on_duplicate: str = "link",
    wait_seconds: float | None = None,
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
    return json.dumps(await _wait_for_import(created["importId"], wait_seconds), ensure_ascii=False)


@toolsets.tool(group="import")
async def import_bibtex(bibtex: str, wait_seconds: float | None = None) -> str:
    """Import papers from a BibTeX string (up to 500 entries).

    Entries carrying a DOI/arXiv ID are re-resolved against the metadata
    sources for authoritative fields; citekeys are preserved. Waits up to
    ``wait_seconds`` (default 15, max 60); a job that is still running returns
    `next` for get_import.
    """
    files = {"file": ("import.bib", bibtex.encode("utf-8"), "application/x-bibtex")}
    created = await _request("POST", "/v1/imports/bibtex", files=files)
    return json.dumps(await _wait_for_import(created["importId"], wait_seconds), ensure_ascii=False)


@toolsets.tool(group="import")
async def get_paper(ref: str) -> str:
    """Get one paper by id, citekey, or DOI: venue with CCF/CAS evaluation,
    authors, identifiers, abstract, and reading state. PDFs are NOT included —
    call list_files for their ids and textStatus."""
    result = await _request("GET", f"/v1/papers/{ref}")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
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


@toolsets.tool(group="import")
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


@toolsets.tool(group="import")
async def upload_pdf(paper_ref: str, path: str) -> str:
    """Attach a PDF from the local filesystem to an existing paper.

    This is the web UI's drag-and-drop path: use it when the paper is already
    in the library and the PDF is a local file (publisher download, email
    attachment) with no reachable URL. ``path`` must be absolute on the machine
    running this MCP server; extraction is queued and returns a job id.
    """
    data, filename = mcp_local.read_pdf(path)
    result = await _request(
        "POST",
        f"/v1/papers/{paper_ref}/files",
        files={"file": (filename, data, "application/pdf")},
    )
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def import_pdf_file(path: str) -> str:
    """Import a paper straight from a local PDF file (the web UI's "drop a PDF").

    The paper is created from the PDF's own metadata, the file is stored, and
    text extraction is queued. Identical bytes link to the existing paper, so
    re-importing the same file cannot duplicate. ``path`` must be absolute.
    """
    data, filename = mcp_local.read_pdf(path)
    result = await _request(
        "POST",
        "/v1/imports/pdf",
        files={"file": (filename, data, "application/pdf")},
    )
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def list_files(paper_ref: str) -> str:
    """List a paper's stored PDFs: id, filename, size, primary, and
    ``textStatus`` (extracted / pending / failed / ocr_needed)."""
    result = await _request("GET", f"/v1/papers/{paper_ref}/files")
    return json.dumps(result, ensure_ascii=False)


def _text_hint(status: str | None) -> str | None:
    """Turn an extraction status into something a caller can act on."""
    if status == "ocr_needed":
        return (
            "No extractable characters: this PDF is a scan or an image-only "
            "export and paperdb does not run OCR. The PDF itself is fine."
        )
    if status == "failed":
        return "Text extraction failed; retry with reindex_file(file_id)."
    if status == "pending":
        return "Text extraction is still queued; retry in a few seconds."
    return None


async def _primary_file(paper_ref: str, file_id: str | None) -> dict[str, Any] | None:
    """The PDF to read: an explicit file id, else the paper's primary file."""
    if file_id is not None:
        return {"id": file_id}
    ref = quote(paper_ref, safe="")
    files = await _request("GET", f"/v1/papers/{ref}/files")
    if not files:
        return None
    return next((item for item in files if item.get("isPrimary")), files[0])


async def _paper_text(
    paper_ref: str,
    page_from: int,
    page_to: int | None,
    max_pages: int,
    file_id: str | None,
) -> str:
    # Clamp instead of erroring: an out-of-range window should still return
    # text, with truncated/nextPage telling the caller where to continue.
    page_from = max(1, page_from)
    max_pages = max(1, min(max_pages, 500))
    primary = await _primary_file(paper_ref, file_id)
    if primary is None:
        return json.dumps(
            {
                "error": "no_pdf",
                "paperRef": paper_ref,
                "hint": "No PDF is attached yet; use attach_pdf(paper_ref, url) first.",
            },
            ensure_ascii=False,
        )
    params: dict[str, Any] = {"from": page_from, "maxPages": max_pages}
    if page_to is not None:
        params["to"] = page_to
    result = await _request("GET", f"/v1/files/{primary['id']}/text", params=params)
    pages = result.get("pages", [])
    status = result.get("textStatus")
    payload: dict[str, Any] = {
        "paperRef": paper_ref,
        "fileId": primary["id"],
        "textStatus": status,
        "pageCount": result.get("pageCount"),
        "pagesReturned": [page["page"] for page in pages],
        "truncated": result.get("truncated", False),
        "nextPage": result.get("nextPage"),
        "text": "\n\n".join(f"[p.{page['page']}]\n{page['text'].strip()}" for page in pages),
    }
    hint = _text_hint(status)
    if hint and not pages:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False)


@toolsets.tool(group="read")
async def get_paper_text(
    paper_ref: str,
    page_from: int = 1,
    page_to: int | None = None,
    max_pages: int = 100,
    file_id: str | None = None,
) -> str:
    """Read a paper's body text — the read step after a search.

    The window is measured in pages: by default one call reads the whole paper
    (up to 100 pages). Pass ``page_to`` to stop at a page, or lower
    ``max_pages`` to keep the answer small — a 100-page read is a lot of
    tokens, so read a section's range when you only need that part. When the
    paper is longer than the window, ``truncated`` is set and ``nextPage`` says
    where to continue. Pages come back as ``[p.N]``-marked text from the
    primary PDF. If the result is empty, ``textStatus`` explains why:
    ``ocr_needed`` (a scan, no OCR in paperdb), ``failed`` (retry with
    reindex_file), or ``pending`` (extraction still running). ``error:
    no_pdf`` means no PDF is attached at all.

    Pass ``file_id`` (from a search hit's ``fileId``) to read a specific PDF
    when a paper has more than one; otherwise the primary PDF is used.
    """
    return await _paper_text(paper_ref, page_from, page_to, max_pages, file_id)


@toolsets.tool(group="read")
async def get_paper_outline(paper_ref: str, file_id: str | None = None) -> str:
    """The paper's section structure (title + page), when the PDF has bookmarks.

    Use it to read selectively: find "4 Method" at page 6, then call
    get_paper_text(page_from=6, page_to=9) instead of pulling the whole paper.
    An empty ``sections`` list is a normal answer (most papers ship without a
    table of contents) — fall back to search_text to locate sections.
    """
    primary = await _primary_file(paper_ref, file_id)
    if primary is None:
        return json.dumps({"error": "no_pdf", "paperRef": paper_ref}, ensure_ascii=False)
    result = await _request("GET", f"/v1/files/{primary['id']}/outline")
    if not result.get("sections"):
        result["hint"] = (
            "No bookmarks in this PDF; use search_text(scope='paper:...') to find "
            "a section, or read pages sequentially."
        )
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="read")
async def library_stats() -> str:
    """One-call overview: how many papers, how many readable, reading-status spread.

    Use it before browsing to size the task ("80 unread, only 40 with text"),
    and to spot gaps: papers without PDFs or without extracted text.
    """
    result = await _request("GET", "/v1/library/stats")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="read")
async def get_page_image(file_id: str, page: int, dpi: int = 120) -> Image:
    """Render one PDF page as an image.

    Text cannot carry figures, tables, equations or two-column layout — this is
    the half of a paper a human sees rather than reads. Use it after
    get_paper_text when the text says "see Figure 3" or the numbers do not
    line up.

    An image costs context by area, so 120 dpi (a whole A4 page well under the
    1568 px long edge most providers downscale to) is the default: read the
    page at 120 first, and raise ``dpi`` only to zoom into one small figure.
    Pages are content-addressed and cached locally, so re-reading a page is
    cheap.
    """
    png = page_cache.read(file_id, page, dpi)
    if png is None:
        response = await _raw_request(
            "GET", f"/v1/files/{file_id}/pages/{page}/image", params={"dpi": dpi}
        )
        png = response.content
        page_cache.write(file_id, page, dpi, png)
    return Image(data=png, format="png")


@toolsets.tool(group="read")
async def get_annotations(paper_ref: str, page: int | None = None) -> str:
    """Read the owner's highlights and notes on a paper (quote, comment, page).

    Use this when the user asks what they marked, or when you want to anchor a
    summary on what they already found worth highlighting rather than re-reading
    the whole PDF.
    """
    params = {"page": page} if page is not None else None
    ref = quote(paper_ref, safe="")
    result = await _request("GET", f"/v1/papers/{ref}/annotations", params=params)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="read")
async def reindex_file(file_id: str) -> str:
    """Re-run text extraction for one PDF (repair after textStatus failed).

    Queues a job for the worker and returns its id; extraction is asynchronous,
    so re-check list_files or get_paper_text a few seconds later.
    """
    result = await _request("POST", f"/v1/files/{file_id}/reindex")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def list_venues(q: str = "", limit: int = 10, cursor: str | None = None) -> str:
    """Search venues (conferences/journals) by name or abbreviation.

    Returns id/fullName/abbrev/kind plus CCF/CAS evaluation when available;
    pass a returned id to update_paper(venue_id=...) to fix a wrong venue.
    Paginate with ``cursor`` from the previous page.
    """
    params: dict[str, Any] = {"limit": limit}
    if q:
        params["q"] = q
    if cursor:
        params["cursor"] = cursor
    result = await _request("GET", "/v1/venues", params=params)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def search_papers(q: str, limit: int = 20) -> str:
    """Search the library by title/author/citekey. Returns brief papers."""
    result = await _request("GET", "/v1/papers", params={"q": q, "limit": limit})
    papers = [_brief_paper(item) for item in result.get("items", [])]
    payload = {"papers": papers, "nextCursor": result.get("nextCursor")}
    return json.dumps(payload, ensure_ascii=False)


@toolsets.tool(group="organize")
async def add_tags(paper_ref: str, paths: list[str]) -> str:
    """Add hierarchical tag paths (e.g. "topic/security", "status/to-read") to
    a paper (by id, citekey, or DOI). Missing tag nodes are created."""
    result = await _request("POST", f"/v1/papers/{paper_ref}/tags", json={"paths": paths})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def add_to_collection(paper_ref: str, collection_id: str) -> str:
    """Add a paper to a collection (by collection id; see list_collections)."""
    result = await _request(
        "POST", f"/v1/papers/{paper_ref}/collections", json={"collectionIds": [collection_id]}
    )
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def list_collections() -> str:
    """List the collection tree (id, name, path) to pick a collection id."""
    result = await _request("GET", "/v1/collections/tree")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def list_workspaces(include_closed: bool = False) -> str:
    """List research workspaces (staging areas) with paper/pending counts.

    A research workspace is a peer of collections/projects, but its papers are
    NOT in the library yet: they are hidden from list_papers/search until
    promoted. Use it for batch research the user may not keep.
    """
    result = await _request("GET", "/v1/workspaces", params={"include_closed": include_closed})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
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


@toolsets.tool(group="workflow")
async def get_workspace(workspace_id: str, pending_only: bool = False) -> str:
    """Get a workspace with its papers (citekey/title/year/promoted)."""
    result = await _request(
        "GET", f"/v1/workspaces/{workspace_id}", params={"pending_only": pending_only}
    )
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def update_workspace(
    workspace_id: str,
    name: str | None = None,
    description: str | None = None,
) -> str:
    """Rename a research workspace or rewrite its description."""
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if not payload:
        return json.dumps({"error": "nothing to update"})
    result = await _request("PATCH", f"/v1/workspaces/{workspace_id}", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
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


@toolsets.tool(group="workflow")
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


@toolsets.tool(group="workflow")
async def discard_workspace_papers(workspace_id: str, paper_refs: list[str]) -> str:
    """Discard papers from a research workspace (staged ones are soft-deleted)."""
    result = await _request(
        "POST",
        f"/v1/workspaces/{workspace_id}/discard",
        json={"paperRefs": paper_refs},
    )
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
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


@toolsets.tool(group="workflow")
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


@toolsets.tool(group="import")
async def get_import(import_id: str) -> str:
    """Get an import job's status and per-item results (the continuation call).

    Returns immediately: status, per-item state, `pendingItems`, and — while
    the job is still running — a `next` hint. Import tools that timed out
    their wait point here.
    """
    return json.dumps(await _wait_for_import(import_id, wait_seconds=0), ensure_ascii=False)


@toolsets.tool(group="read")
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
    view_id: str | None = None,
    filter: dict[str, Any] | None = None,
    sort: str | None = None,
    include_staged: bool = False,
    limit: int = 20,
    cursor: str | None = None,
) -> str:
    """Browse the library like the web UI: filter papers by search text,
    collection, tag path, venue, kind (conference/journal/preprint/...), year,
    reading status, starred, or unfiled.

    Also runs the web UI's saved views and advanced filters: ``view_id`` runs a
    saved view (list_views shows them), ``filter`` is the filter-DSL object, and
    ``sort`` is one of added/updated/year/title/citations. Returns brief papers
    + nextCursor for pagination."""
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
    if view_id:
        params["view"] = view_id
    if filter:
        # The API takes the filter DSL as base64url JSON (it travels as a query
        # string in the web UI too); raw JSON would be a 400.
        encoded = base64.urlsafe_b64encode(json.dumps(filter).encode()).decode()
        params["filter"] = encoded.rstrip("=")
    if sort:
        params["sort"] = sort
    if include_staged:
        params["includeStaged"] = True
    if cursor:
        params["cursor"] = cursor
    result = await _request("GET", "/v1/papers", params=params)
    papers = [_brief_paper(item) for item in result.get("items", [])]
    payload = {"papers": papers, "nextCursor": result.get("nextCursor")}
    return json.dumps(payload, ensure_ascii=False)


@toolsets.tool(group="organize")
async def list_tags() -> str:
    """List the full hierarchical tag tree (namespace paths + paper counts)."""
    result = await _request("GET", "/v1/tags/tree")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def list_projects() -> str:
    """List project workspaces (name, description, collections, paper count)."""
    result = await _request("GET", "/v1/projects")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def get_project(project_id: str) -> str:
    """Get one project with its papers (project detail + paper list)."""
    detail = await _request("GET", f"/v1/projects/{project_id}")
    papers = await _request("GET", f"/v1/projects/{project_id}/papers", params={"limit": 100})
    brief = [_brief_paper(item) for item in papers.get("items", [])]
    payload = dict(detail)
    payload["papers"] = brief
    payload["papersNextCursor"] = papers.get("nextCursor")
    return json.dumps(payload, ensure_ascii=False)


@toolsets.tool(group="read")
async def get_citations(paper_ref: str, kind: str = "references") -> str:
    """Get a paper's citation graph: 'references' (papers it cites) or
    'citations' (papers citing it). Library entries are resolved; external
    works are returned as stubs."""
    if kind not in {"references", "citations"}:
        return json.dumps({"error": "kind must be 'references' or 'citations'"})
    result = await _request("GET", f"/v1/papers/{paper_ref}/{kind}")
    return json.dumps(result, ensure_ascii=False)


async def _search_text(
    q: str,
    scope: str,
    limit: int,
    context_chars: int,
    max_per_paper: int | None,
) -> str:
    params: dict[str, Any] = {"q": q, "limit": limit, "contextChars": context_chars}
    if max_per_paper is not None:
        params["maxPerPaper"] = max_per_paper
    if scope in {"", "library"}:
        pass
    elif scope.startswith("workspace:"):
        params["workspaceId"] = scope.split(":", 1)[1]
    elif scope.startswith("paper:"):
        ref = quote(scope.split(":", 1)[1], safe="")
        paper = await _request("GET", f"/v1/papers/{ref}")
        params["paperId"] = paper["id"]
    elif scope.startswith("file:"):
        params["fileId"] = scope.split(":", 1)[1]
    else:
        return json.dumps(
            {
                "error": "invalid_scope",
                "scope": scope,
                "hint": 'Use "library", "workspace:<id>", "paper:<ref>" or "file:<id>".',
            },
            ensure_ascii=False,
        )
    result = await _request("GET", "/v1/search/fulltext", params=params)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="read")
async def search_text(
    q: str,
    scope: str = "library",
    limit: int = 20,
    context_chars: int = 400,
    max_per_paper: int | None = None,
) -> str:
    """Search the text of stored PDFs, with an explicit scope.

    ``scope``:
      - ``library`` — everything promoted into the library (default)
      - ``workspace:<id>`` — one research workspace, staged papers included
      - ``paper:<ref>`` — one paper by citekey / id / DOI
      - ``file:<id>`` — one PDF

    Scoped search reaches research-workspace (staged) papers; the library scope
    does not. Hits carry ``paperId``, ``fileId`` and ``page`` — chain into
    get_paper_text to read the page instead of the snippet.
    """
    return await _search_text(q, scope, limit, context_chars, max_per_paper)


@toolsets.tool(group="read")
async def search_fulltext(q: str, limit: int = 20) -> str:
    """Library-wide full-text search: shorthand for search_text(scope="library")."""
    return await _search_text(q, "library", limit, 240, None)


@toolsets.tool(group="read")
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


@toolsets.tool(group="read")
async def get_paper_bibtex(ref: str) -> str:
    """Get a paper's BibTeX entry (the same string used by .bib exports)."""
    result = await _request("GET", f"/v1/papers/{ref}/bibtex", raw=True)
    return json.dumps({"bibtex": result}, ensure_ascii=False)


@toolsets.tool(group="organize")
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


@toolsets.tool(group="organize")
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


# ---------------------------------------------------------------------------
# Organizing: tags, collections, projects
#
# The web UI's organizing surface. Deletes keep the same human gate as
# delete_paper: a current TOTP code, asked for right before the call.
# ---------------------------------------------------------------------------


@toolsets.tool(group="organize")
async def create_tag(
    path: str,
    color: str | None = None,
    description: str | None = None,
) -> str:
    """Create a tag by hierarchical path (e.g. "topic/llm/rag").

    Missing parents are created; an existing tag is returned as-is.
    """
    payload: dict[str, Any] = {"path": path}
    if color is not None:
        payload["color"] = color
    if description is not None:
        payload["description"] = description
    result = await _request("POST", "/v1/tags", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def update_tag(
    tag_id: str,
    name: str | None = None,
    color: str | None = None,
    description: str | None = None,
) -> str:
    """Rename the tag's display name, or change its colour/description.

    ``name`` is the display name only — the path (the stable key) does not
    change. Use merge_tags to fold one path into another.
    """
    payload: dict[str, Any] = {}
    for key, value in (("name", name), ("color", color), ("description", description)):
        if value is not None:
            payload[key] = value
    if not payload:
        return json.dumps({"error": "nothing to update"})
    result = await _request("PATCH", f"/v1/tags/{tag_id}", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def merge_tags(tag_id: str, into_tag_id: str) -> str:
    """Move every paper from ``tag_id`` onto ``into_tag_id`` and delete the
    source tag. Use it to fold duplicates such as "llm" and "topic/llm"."""
    result = await _request("POST", f"/v1/tags/{tag_id}/merge", json={"intoTagId": into_tag_id})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def delete_tag(tag_id: str, totp_code: str) -> str:
    """Delete a tag and detach it from every paper. DANGEROUS.

    Requires a current 6-digit TOTP code; ask the user right before calling.
    """
    await _request(
        "DELETE",
        f"/v1/tags/{tag_id}",
        headers={"X-TOTP-Code": totp_code.strip()},
    )
    return json.dumps({"deleted": tag_id, "result": "204 no content"})


@toolsets.tool(group="organize")
async def remove_tags(paper_ref: str, paths: list[str]) -> str:
    """Remove tag paths from one paper (the inverse of add_tags)."""
    result = await _request("DELETE", f"/v1/papers/{paper_ref}/tags", json={"paths": paths})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def update_collection(
    collection_id: str,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
) -> str:
    """Rename or redocument a collection."""
    payload: dict[str, Any] = {}
    for key, value in (("name", name), ("slug", slug), ("description", description)):
        if value is not None:
            payload[key] = value
    if not payload:
        return json.dumps({"error": "nothing to update"})
    result = await _request("PATCH", f"/v1/collections/{collection_id}", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def move_collection(
    collection_id: str,
    parent_id: str | None = None,
    position: int = 0,
) -> str:
    """Reparent/reorder a collection. ``parent_id=None`` moves it to the root."""
    result = await _request(
        "POST",
        f"/v1/collections/{collection_id}/move",
        json={"parentId": parent_id, "position": position},
    )
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def delete_collection(collection_id: str, totp_code: str) -> str:
    """Delete a collection. Papers survive — only the shelf goes away.

    DANGEROUS, TOTP-gated: ask the user for a current 6-digit code first.
    """
    await _request(
        "DELETE",
        f"/v1/collections/{collection_id}",
        headers={"X-TOTP-Code": totp_code.strip()},
    )
    return json.dumps({"deleted": collection_id, "result": "204 no content"})


@toolsets.tool(group="organize")
async def collection_papers(
    collection_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    reorder: list[str] | None = None,
) -> str:
    """Add, remove, or reorder papers inside one collection in a single call.

    ``add``/``remove`` take paper refs (id, citekey or DOI); ``reorder`` takes
    the full ordered list of paper ids.
    """
    result: dict[str, Any] = {}
    if add:
        result["added"] = await _request(
            "POST",
            f"/v1/collections/{collection_id}/papers",
            json={"paperIds": add},
        )
    if remove:
        result["removed"] = await _request(
            "DELETE",
            f"/v1/collections/{collection_id}/papers",
            json={"paperIds": remove},
        )
    if reorder:
        result["reordered"] = await _request(
            "POST",
            f"/v1/collections/{collection_id}/reorder",
            json={"paperIds": reorder},
        )
    if not result:
        return json.dumps({"error": "nothing to do"})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def get_collection_bibtex(collection_id: str) -> str:
    """BibTeX for a collection (and its sub-collections), same text as the
    web UI's .bib export."""
    text = await _request("GET", f"/v1/collections/{collection_id}/bibtex", raw=True)
    return json.dumps({"bibtex": text}, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def create_project(
    name: str,
    description: str | None = None,
) -> str:
    """Create a project (a workspace grouping collections and papers)."""
    payload: dict[str, Any] = {"name": name}
    if description is not None:
        payload["description"] = description
    result = await _request("POST", "/v1/projects", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def update_project(
    project_id: str,
    name: str | None = None,
    description: str | None = None,
) -> str:
    """Rename or redocument a project."""
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if not payload:
        return json.dumps({"error": "nothing to update"})
    result = await _request("PATCH", f"/v1/projects/{project_id}", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def delete_project(project_id: str, totp_code: str) -> str:
    """Delete a project (its collections and papers stay). DANGEROUS, TOTP-gated."""
    await _request(
        "DELETE",
        f"/v1/projects/{project_id}",
        headers={"X-TOTP-Code": totp_code.strip()},
    )
    return json.dumps({"deleted": project_id, "result": "204 no content"})


@toolsets.tool(group="workflow")
async def project_papers(
    project_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> str:
    """Add or remove papers in a project in one call."""
    result: dict[str, Any] = {}
    if add:
        result["added"] = await _request(
            "POST", f"/v1/projects/{project_id}/papers", json={"paperIds": add}
        )
    if remove:
        result["removed"] = await _request(
            "DELETE", f"/v1/projects/{project_id}/papers", json={"paperIds": remove}
        )
    if not result:
        return json.dumps({"error": "nothing to do"})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def project_collections(
    project_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> str:
    """Attach or detach collections from a project in one call."""
    result: dict[str, Any] = {}
    if add:
        result["added"] = await _request(
            "POST", f"/v1/projects/{project_id}/collections", json={"collectionIds": add}
        )
    if remove:
        result["removed"] = await _request(
            "DELETE", f"/v1/projects/{project_id}/collections", json={"collectionIds": remove}
        )
    if not result:
        return json.dumps({"error": "nothing to do"})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def get_project_bibtex(project_id: str) -> str:
    """BibTeX for everything in a project, same text as the web UI's export."""
    text = await _request("GET", f"/v1/projects/{project_id}/bibtex", raw=True)
    return json.dumps({"bibtex": text}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Views, auto-rules, and the paper lifecycle (create / bulk / trash / restore)
# ---------------------------------------------------------------------------


@toolsets.tool(group="workflow")
async def list_views() -> str:
    """Saved "smart views": filter DSL + sort + count, as the web UI sidebar
    shows them. Use a view's id with list_papers(view_id=...) to run it."""
    result = await _request("GET", "/v1/views")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def validate_filter(filter: dict[str, Any]) -> str:
    """Check a filter DSL payload before saving it into a view or an auto-rule.
    Returns the number of rules it parsed."""
    result = await _request("POST", "/v1/views/validate", json={"filter": filter})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def create_view(
    name: str,
    filter: dict[str, Any] | None = None,
    sort: list[dict[str, Any]] | None = None,
    columns: list[str] | None = None,
    view_mode: str = "table",
    is_pinned: bool = False,
) -> str:
    """Save a smart view (filter DSL + sort). Validate the filter first."""
    payload: dict[str, Any] = {
        "name": name,
        "filter": filter or {},
        "viewMode": view_mode,
        "isPinned": is_pinned,
    }
    if sort is not None:
        payload["sort"] = sort
    if columns is not None:
        payload["columns"] = columns
    result = await _request("POST", "/v1/views", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def update_view(
    view_id: str,
    name: str | None = None,
    filter: dict[str, Any] | None = None,
    sort: list[dict[str, Any]] | None = None,
    is_pinned: bool | None = None,
) -> str:
    """Change a saved view in place (name / filter / sort / pin)."""
    payload: dict[str, Any] = {}
    for key, value in (
        ("name", name),
        ("filter", filter),
        ("sort", sort),
        ("isPinned", is_pinned),
    ):
        if value is not None:
            payload[key] = value
    if not payload:
        return json.dumps({"error": "nothing to update"})
    result = await _request("PATCH", f"/v1/views/{view_id}", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def delete_view(view_id: str, totp_code: str) -> str:
    """Delete a saved view. DANGEROUS, TOTP-gated."""
    await _request("DELETE", f"/v1/views/{view_id}", headers={"X-TOTP-Code": totp_code.strip()})
    return json.dumps({"deleted": view_id, "result": "204 no content"})


@toolsets.tool(group="workflow")
async def list_rules() -> str:
    """Automatic tagging rules and when they run (run_on / enabled / last run)."""
    result = await _request("GET", "/v1/auto-rules")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def create_rule(
    name: str,
    filter: dict[str, Any],
    actions: list[dict[str, Any]],
    run_on: list[str] | None = None,
    enabled: bool = True,
) -> str:
    """Create an auto-rule: when a paper matches ``filter``, apply ``actions``
    (e.g. add tags / add to a collection). Validate the filter first."""
    payload: dict[str, Any] = {
        "name": name,
        "filter": filter,
        "actions": actions,
        "runOn": run_on or ["manual"],
        "enabled": enabled,
    }
    result = await _request("POST", "/v1/auto-rules", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def update_rule(
    rule_id: str,
    name: str | None = None,
    filter: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    run_on: list[str] | None = None,
    enabled: bool | None = None,
) -> str:
    """Change an auto-rule in place."""
    payload: dict[str, Any] = {}
    for key, value in (
        ("name", name),
        ("filter", filter),
        ("actions", actions),
        ("runOn", run_on),
        ("enabled", enabled),
    ):
        if value is not None:
            payload[key] = value
    if not payload:
        return json.dumps({"error": "nothing to update"})
    result = await _request("PATCH", f"/v1/auto-rules/{rule_id}", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="workflow")
async def delete_rule(rule_id: str) -> str:
    """Delete an auto-rule (papers it already changed keep their tags)."""
    await _request("DELETE", f"/v1/auto-rules/{rule_id}")
    return json.dumps({"deleted": rule_id, "result": "204 no content"})


@toolsets.tool(group="workflow")
async def apply_rule(rule_id: str) -> str:
    """Run an auto-rule over the current library; returns {"applied": N}."""
    result = await _request("POST", f"/v1/auto-rules/{rule_id}/apply")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def create_paper(
    kind: str,
    title: str,
    year: int,
    abstract: str | None = None,
    venue_id: str | None = None,
    authors: list[str] | None = None,
    identifiers: list[dict[str, Any]] | None = None,
    citekey: str | None = None,
    language: str = "en",
) -> str:
    """Create a paper by hand when no aggregator has it.

    ``kind`` is conference/journal/preprint/arxiv/techreport/thesis/book;
    ``authors`` are display names; ``identifiers`` are
    [{"scheme": "doi"|"arxiv"|..., "value": "..."}]. Prefer import_paper —
    this path skips dedupe and venue normalization.
    """
    payload: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "year": year,
        "language": language,
    }
    if abstract is not None:
        payload["abstract"] = abstract
    if venue_id is not None:
        payload["venueId"] = venue_id
    if authors:
        payload["authors"] = [{"displayName": name} for name in authors]
    if identifiers:
        payload["identifiers"] = identifiers
    if citekey is not None:
        payload["citekey"] = citekey
    result = await _request("POST", "/v1/papers", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def bulk_update_papers(
    paper_ids: list[str],
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    add_collections: list[str] | None = None,
    remove_collections: list[str] | None = None,
    status: str | None = None,
    starred: bool | None = None,
) -> str:
    """Batch edit up to 500 papers in one call — tags, collections, and state.

    ``paper_ids`` are paper ids. This is the multi-select action of the web UI:
    organizing a survey set should not be one request per paper.
    """
    patch: dict[str, Any] = {}
    if add_tags:
        patch["addTags"] = add_tags
    if remove_tags:
        patch["removeTags"] = remove_tags
    if add_collections:
        patch["addCollections"] = add_collections
    if remove_collections:
        patch["removeCollections"] = remove_collections
    state: dict[str, Any] = {}
    if status is not None:
        state["status"] = status
    if starred is not None:
        state["starred"] = starred
    if state:
        patch["state"] = state
    if not patch:
        return json.dumps({"error": "nothing to change"})
    result = await _request("PATCH", "/v1/papers/bulk", json={"ids": paper_ids, "patch": patch})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def list_trash(limit: int = 20) -> str:
    """Papers in the recycle bin (soft-deleted, restorable)."""
    result = await _request("GET", "/v1/papers/trash", params={"limit": limit})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def restore_paper(paper_ref: str) -> str:
    """Restore a paper from the recycle bin."""
    result = await _request("POST", f"/v1/papers/{paper_ref}/restore")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def list_duplicates(paper_ref: str) -> str:
    """Candidate duplicates of one paper (same title/year/identifiers), each with
    the evidence — check before merging, and prefer merge_papers over deleting."""
    result = await _request("GET", f"/v1/papers/{paper_ref}/duplicates")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def enrich_paper(paper_ref: str) -> str:
    """Re-run metadata enrichment (abstract, venue, identifiers, citations) for a
    paper whose record looks thin. Queues a job; returns its id."""
    result = await _request("POST", f"/v1/papers/{paper_ref}/enrich")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def create_annotation(
    paper_ref: str,
    file_id: str,
    page: int,
    rects: list[dict[str, float]],
    quote: str = "",
    comment: str | None = None,
    color: str = "yellow",
    kind: str = "highlight",
) -> str:
    """Add a highlight/note to a stored PDF.

    ``rects`` are page-relative boxes ``{"x", "y", "w", "h"}`` with values in
    0..1 as the web reader stores them; ``quote`` is the highlighted text. This
    is how an agent leaves marks the user will see in the reader.
    """
    payload: dict[str, Any] = {
        "pdfFileId": file_id,
        "page": page,
        "rects": rects,
        "quote": quote,
        "color": color,
        "kind": kind,
    }
    if comment is not None:
        payload["comment"] = comment
    result = await _request("POST", f"/v1/papers/{paper_ref}/annotations", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def update_annotation(
    annotation_id: str,
    comment: str | None = None,
    color: str | None = None,
    rects: list[dict[str, float]] | None = None,
) -> str:
    """Edit an annotation's comment, colour, or geometry."""
    payload: dict[str, Any] = {}
    if comment is not None:
        payload["comment"] = comment
    if color is not None:
        payload["color"] = color
    if rects is not None:
        payload["rects"] = rects
    if not payload:
        return json.dumps({"error": "nothing to update"})
    result = await _request("PATCH", f"/v1/annotations/{annotation_id}", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def delete_annotation(annotation_id: str) -> str:
    """Delete one annotation (soft delete)."""
    await _request("DELETE", f"/v1/annotations/{annotation_id}")
    return json.dumps({"deleted": annotation_id, "result": "204 no content"})


@toolsets.tool(group="organize")
async def set_primary_file(file_id: str) -> str:
    """Make one PDF the paper's primary file (the one get_paper_text reads)."""
    result = await _request("POST", f"/v1/files/{file_id}/primary")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="organize")
async def delete_file(file_id: str, totp_code: str) -> str:
    """Delete one stored PDF. DANGEROUS, TOTP-gated.

    The paper survives; only the file (and its extracted text) goes away.
    """
    await _request("DELETE", f"/v1/files/{file_id}", headers={"X-TOTP-Code": totp_code.strip()})
    return json.dumps({"deleted": file_id, "result": "204 no content"})


@toolsets.tool(group="read")
async def whoami() -> str:
    """Which token this server is using, and the scopes it carries.

    Call it when a write unexpectedly returns 403: the answer says whether the
    token (not the tool) is the limit — e.g. citation sync needs "admin".
    """
    result = await _request("GET", "/v1/me")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def list_imports(limit: int = 20) -> str:
    """Recent import jobs (newest first) — what was imported, and how it went."""
    result = await _request("GET", "/v1/imports", params={"limit": limit})
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def cancel_import(import_id: str) -> str:
    """Cancel a running import job; finished items stay imported."""
    result = await _request("POST", f"/v1/imports/{import_id}/cancel")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def retry_import_item(import_id: str, item_id: str) -> str:
    """Retry one failed item of an import job (get_import lists the item ids)."""
    result = await _request("POST", f"/v1/imports/{import_id}/items/{item_id}/retry")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def get_job(job_id: str) -> str:
    """Status of any worker job (reindex, enrich, extract…) by id."""
    result = await _request("GET", f"/v1/jobs/{job_id}")
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Authors and venues
# ---------------------------------------------------------------------------


@toolsets.tool(group="library")
async def list_authors(q: str | None = None, limit: int = 20, cursor: str | None = None) -> str:
    """Browse the author records (with paper counts), optionally filtered by name."""
    params: dict[str, Any] = {"limit": limit}
    if q:
        params["q"] = q
    if cursor:
        params["cursor"] = cursor
    result = await _request("GET", "/v1/authors", params=params)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="library")
async def get_author(author_id: str) -> str:
    """One author record: aliases, identifiers, and paper count."""
    result = await _request("GET", f"/v1/authors/{author_id}")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="library")
async def create_author(
    display_name: str,
    family_name: str | None = None,
    given_name: str | None = None,
    orcid: str | None = None,
    homepage_url: str | None = None,
    is_me: bool = False,
) -> str:
    """Create an author record (for manual papers or "my" profile)."""
    payload: dict[str, Any] = {"displayName": display_name, "isMe": is_me}
    for key, value in (
        ("familyName", family_name),
        ("givenName", given_name),
        ("orcid", orcid),
        ("homepage_url", homepage_url),
    ):
        if value is not None:
            payload[key] = value
    result = await _request("POST", "/v1/authors", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="library")
async def update_author(
    author_id: str,
    display_name: str | None = None,
    orcid: str | None = None,
    homepage_url: str | None = None,
    is_me: bool | None = None,
) -> str:
    """Fix an author record (name spelling, ORCID, homepage, "this is me")."""
    payload: dict[str, Any] = {}
    for key, value in (
        ("displayName", display_name),
        ("orcid", orcid),
        ("homepage_url", homepage_url),
        ("isMe", is_me),
    ):
        if value is not None:
            payload[key] = value
    if not payload:
        return json.dumps({"error": "nothing to update"})
    result = await _request("PATCH", f"/v1/authors/{author_id}", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def get_venue(venue_id: str) -> str:
    """One venue with its editions, CCF field, and identifiers."""
    result = await _request("GET", f"/v1/venues/{venue_id}")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="import")
async def get_venue_ranks(venue_id: str) -> str:
    """The CCF/CAS evaluations attached to a venue, with dataset versions."""
    result = await _request("GET", f"/v1/venues/{venue_id}/ranks")
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="library")
async def create_venue(
    kind: str,
    full_name: str,
    abbrev: str | None = None,
    ccf_field: str | None = None,
    homepage_url: str | None = None,
) -> str:
    """Create a venue when normalization could not find it (provisional venues).

    ``kind`` is conference/journal/preprint/... — then re-point a paper at it
    with update_paper(venue_id=...) to restore its CCF evaluation.
    """
    payload: dict[str, Any] = {"kind": kind, "fullName": full_name}
    if abbrev is not None:
        payload["abbrev"] = abbrev
    if ccf_field is not None:
        payload["ccfField"] = ccf_field
    if homepage_url is not None:
        payload["homepage_url"] = homepage_url
    result = await _request("POST", "/v1/venues", json=payload)
    return json.dumps(result, ensure_ascii=False)


@toolsets.tool(group="library")
async def update_venue(
    venue_id: str,
    full_name: str | None = None,
    abbrev: str | None = None,
    homepage_url: str | None = None,
) -> str:
    """Fix a venue's name / abbreviation / homepage."""
    payload: dict[str, Any] = {}
    for key, value in (
        ("fullName", full_name),
        ("abbrev", abbrev),
        ("homepage_url", homepage_url),
    ):
        if value is not None:
            payload[key] = value
    if not payload:
        return json.dumps({"error": "nothing to update"})
    result = await _request("PATCH", f"/v1/venues/{venue_id}", json=payload)
    return json.dumps(result, ensure_ascii=False)


def main() -> None:
    if not _TOKEN:
        print(
            "paperdb-mcp: no token (set PAPERDB_MCP_TOKEN or PAPERDB_MCP_TOKEN_FILE)",
            file=sys.stderr,
        )
        raise SystemExit(1)
    mcp.run()


if __name__ == "__main__":
    main()
