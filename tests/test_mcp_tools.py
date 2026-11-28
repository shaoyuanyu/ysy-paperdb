"""MCP tool-surface tests.

Every test drives the real MCP tool functions (the ones the harness exposes)
against the in-process ASGI app, so the request shapes the agent actually sends
— path quoting, camelCase bodies, query aliases — are what gets validated here.

The point of this file is reachability: if a human can do it in the web UI, the
tool must be able to do it too, and the test proves the exact call works.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pymupdf
import pytest

from paperdb import mcp_server

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


def _load(raw: str) -> dict:
    return json.loads(raw)


async def _paper(client: httpx.AsyncClient, title: str | None = None) -> dict:
    marker = uuid.uuid4().hex[:8]
    response = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "preprint",
            "title": title or f"MCP Test {marker}",
            "year": 2026,
            "authors": [{"displayName": "Alice Example"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pdf(text: str = "A page of readable text.") -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontsize=11)
    data = document.tobytes()
    document.close()
    return data


async def test_tags_can_be_created_renamed_merged_and_removed(
    client: httpx.AsyncClient, mcp_api: None
) -> None:
    paper = await _paper(client)
    path = f"topic/{uuid.uuid4().hex[:6]}"

    created = _load(await mcp_server.create_tag(path))
    assert created["path"] == path

    assert _load(await mcp_server.add_tags(paper["id"], [path])) == {"added": 1}
    detail = _load(await mcp_server.get_paper(paper["id"]))
    assert path in detail["tags"]

    assert _load(await mcp_server.remove_tags(paper["id"], [path])) == {"removed": 1}
    detail = _load(await mcp_server.get_paper(paper["id"]))
    assert path not in detail["tags"]

    # ``name`` is the display name; ``path`` is the stable key and stays put.
    renamed = _load(await mcp_server.update_tag(created["id"], name="renamed"))
    assert renamed["name"] == "renamed"
    assert renamed["path"] == path

    target = _load(await mcp_server.create_tag(f"topic/{uuid.uuid4().hex[:6]}"))
    merged = _load(await mcp_server.merge_tags(renamed["id"], target["id"]))
    assert merged["id"] == target["id"]

    await mcp_server.delete_tag(target["id"], totp_code="000000")
    tree = _load(await mcp_server.list_tags())
    assert all(tag["id"] != target["id"] for tag in tree)


async def test_collections_membership_and_bibtex(client: httpx.AsyncClient, mcp_api: None) -> None:
    paper = await _paper(client)
    name = f"Shelf {uuid.uuid4().hex[:6]}"
    collection = _load(await mcp_server.create_collection(name))

    added = _load(await mcp_server.collection_papers(collection["id"], add=[paper["id"]]))
    assert added["added"] == {"added": 1}

    listed = _load(await mcp_server.list_papers(collection_id=collection["id"]))
    assert [item["id"] for item in listed["papers"]] == [paper["id"]]

    bibtex = _load(await mcp_server.get_collection_bibtex(collection["id"]))["bibtex"]
    assert paper["title"] in bibtex

    removed = _load(await mcp_server.collection_papers(collection["id"], remove=[paper["id"]]))
    assert removed["removed"] == {"removed": 1}
    assert _load(await mcp_server.list_papers(collection_id=collection["id"]))["papers"] == []

    renamed = _load(await mcp_server.update_collection(collection["id"], name=f"{name} v2"))
    assert renamed["name"] == f"{name} v2"

    nested = _load(await mcp_server.create_collection(f"Child {uuid.uuid4().hex[:6]}"))
    moved = _load(await mcp_server.move_collection(nested["id"], parent_id=collection["id"]))
    assert moved["parentId"] == collection["id"]

    await mcp_server.delete_collection(nested["id"], totp_code="000000")
    assert _load(await mcp_server.list_collections())


async def test_projects_bundle_collections_and_papers(
    client: httpx.AsyncClient, mcp_api: None
) -> None:
    paper = await _paper(client)
    collection = _load(await mcp_server.create_collection(f"Proj shelf {uuid.uuid4().hex[:6]}"))

    project = _load(await mcp_server.create_project(f"Proj {uuid.uuid4().hex[:6]}"))
    assert _load(await mcp_server.project_papers(project["id"], add=[paper["id"]]))["added"]
    assert _load(await mcp_server.project_collections(project["id"], add=[collection["id"]]))[
        "added"
    ]

    detail = _load(await mcp_server.get_project(project["id"]))
    assert detail["paperCount"] == 1

    bibtex = _load(await mcp_server.get_project_bibtex(project["id"]))["bibtex"]
    assert paper["title"] in bibtex

    assert _load(await mcp_server.project_papers(project["id"], remove=[paper["id"]]))["removed"]
    assert _load(await mcp_server.project_collections(project["id"], remove=[collection["id"]]))[
        "removed"
    ]

    renamed = _load(await mcp_server.update_project(project["id"], name="Renamed project"))
    assert renamed["name"] == "Renamed project"
    await mcp_server.delete_project(project["id"], totp_code="000000")
    assert all(item["id"] != project["id"] for item in _load(await mcp_server.list_projects()))


async def test_views_run_saved_filters_and_sort(client: httpx.AsyncClient, mcp_api: None) -> None:
    paper = await _paper(client)
    marker = paper["title"].split()[-1]

    validated = _load(
        await mcp_server.validate_filter(
            {
                "version": 1,
                "match": "all",
                "rules": [{"field": "q", "op": "contains", "value": marker}],
            }
        )
    )
    assert validated["valid"] is True

    view = _load(
        await mcp_server.create_view(
            f"View {uuid.uuid4().hex[:6]}",
            filter={
                "version": 1,
                "match": "all",
                "rules": [{"field": "q", "op": "contains", "value": marker}],
            },
            sort=[{"field": "year", "direction": "desc"}],
        )
    )
    listed = _load(await mcp_server.list_papers(view_id=view["id"]))
    assert [item["id"] for item in listed["papers"]] == [paper["id"]]

    by_filter = _load(
        await mcp_server.list_papers(
            filter={
                "version": 1,
                "match": "all",
                "rules": [{"field": "q", "op": "contains", "value": marker}],
            }
        )
    )
    assert [item["id"] for item in by_filter["papers"]] == [paper["id"]]

    by_sort = _load(await mcp_server.list_papers(q=marker, sort="year"))
    assert by_sort["papers"][0]["id"] == paper["id"]

    assert any(item["id"] == view["id"] for item in _load(await mcp_server.list_views()))
    assert _load(await mcp_server.update_view(view["id"], name="Renamed view"))["name"] == (
        "Renamed view"
    )
    await mcp_server.delete_view(view["id"], totp_code="000000")
    assert all(item["id"] != view["id"] for item in _load(await mcp_server.list_views()))


async def test_auto_rules_apply_tags_retroactively(
    client: httpx.AsyncClient, mcp_api: None
) -> None:
    paper = await _paper(client)
    marker = paper["title"].split()[-1]
    rule_tag = f"rule/{uuid.uuid4().hex[:6]}"

    rule = _load(
        await mcp_server.create_rule(
            f"Rule {uuid.uuid4().hex[:6]}",
            filter={
                "version": 1,
                "match": "all",
                "rules": [{"field": "q", "op": "contains", "value": marker}],
            },
            actions=[{"type": "add_tag", "path": rule_tag}],
            run_on=["manual"],
        )
    )
    applied = _load(await mcp_server.apply_rule(rule["id"]))
    assert applied["applied"] == 1
    assert rule_tag in _load(await mcp_server.get_paper(paper["id"]))["tags"]

    assert any(item["id"] == rule["id"] for item in _load(await mcp_server.list_rules()))
    updated = _load(await mcp_server.update_rule(rule["id"], enabled=False))
    assert updated["enabled"] is False
    await mcp_server.delete_rule(rule["id"])
    assert all(item["id"] != rule["id"] for item in _load(await mcp_server.list_rules()))


async def test_paper_lifecycle_create_bulk_trash_restore(
    client: httpx.AsyncClient, mcp_api: None
) -> None:
    title = f"Manual {uuid.uuid4().hex[:8]}"
    created = _load(
        await mcp_server.create_paper(
            kind="preprint",
            title=title,
            year=2026,
            abstract="Hand-written record.",
            authors=["Bob Example"],
        )
    )
    tag = f"bulk/{uuid.uuid4().hex[:6]}"
    collection = _load(await mcp_server.create_collection(f"Bulk {uuid.uuid4().hex[:6]}"))

    bulk = _load(
        await mcp_server.bulk_update_papers(
            [created["id"]],
            add_tags=[tag],
            add_collections=[collection["id"]],
            status="queued",
        )
    )
    assert bulk["updated"] == 1

    detail = _load(await mcp_server.get_paper(created["id"]))
    assert tag in detail["tags"]
    assert collection["id"] in detail["collections"]
    assert detail["state"]["status"] == "queued"

    assert _load(await mcp_server.list_duplicates(created["id"])) == []

    await mcp_server.delete_paper(created["id"], totp_code="000000")
    trash = _load(await mcp_server.list_trash())
    assert any(item["id"] == created["id"] for item in trash["items"])

    restored = _load(await mcp_server.restore_paper(created["id"]))
    assert restored["id"] == created["id"]

    enriched = _load(await mcp_server.enrich_paper(created["id"]))
    assert enriched.get("jobId") or enriched.get("job_id")


async def test_annotations_round_trip(
    client: httpx.AsyncClient, mcp_api: None, drain_queue: None
) -> None:
    paper = await _paper(client)
    upload = await client.post(
        f"/v1/papers/{paper['id']}/files",
        headers=SERVICE,
        files={"file": ("a.pdf", _pdf(), "application/pdf")},
    )
    file_id = upload.json()["file"]["id"]

    annotation = _load(
        await mcp_server.create_annotation(
            paper["id"],
            file_id,
            page=1,
            rects=[{"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1}],
            quote="A page of readable text.",
            comment="worth citing",
        )
    )
    assert annotation["comment"] == "worth citing"

    listed = _load(await mcp_server.get_annotations(paper["id"]))
    assert [item["id"] for item in listed] == [annotation["id"]]

    updated = _load(await mcp_server.update_annotation(annotation["id"], comment="even better"))
    assert updated["comment"] == "even better"

    await mcp_server.delete_annotation(annotation["id"])
    assert _load(await mcp_server.get_annotations(paper["id"])) == []


async def test_files_primary_delete_and_job_lookup(
    client: httpx.AsyncClient, mcp_api: None, drain_queue: None
) -> None:
    paper = await _paper(client)
    first = (
        await client.post(
            f"/v1/papers/{paper['id']}/files",
            headers=SERVICE,
            files={"file": ("first.pdf", _pdf("first"), "application/pdf")},
        )
    ).json()["file"]
    second = (
        await client.post(
            f"/v1/papers/{paper['id']}/files",
            headers=SERVICE,
            files={"file": ("second.pdf", _pdf("second"), "application/pdf")},
        )
    ).json()["file"]

    # Extraction jobs are queued by the uploads; run them before deleting a
    # file, otherwise the worker later tries to extract a deleted row.
    from paperdb.jobs.worker import run_once

    while await run_once():
        pass

    promoted = _load(await mcp_server.set_primary_file(first["id"]))
    assert promoted["isPrimary"] is True

    files = _load(await mcp_server.list_files(paper["id"]))
    assert [item["id"] for item in files if item["isPrimary"]] == [first["id"]]

    job = _load(await mcp_server.reindex_file(second["id"]))
    job_id = job.get("jobId") or job.get("job_id")
    status = _load(await mcp_server.get_job(job_id))
    assert status["id"] == job_id

    await mcp_server.delete_file(second["id"], totp_code="000000")
    assert [item["id"] for item in _load(await mcp_server.list_files(paper["id"]))] == [first["id"]]


async def test_imports_listing_and_job_inspection(client: httpx.AsyncClient, mcp_api: None) -> None:
    imports = _load(await mcp_server.list_imports())
    assert "items" in imports
    # get_import on an unknown id must surface the API error, not hang.
    try:
        await mcp_server.get_import(str(uuid.uuid4()))
    except RuntimeError as exc:
        assert "404" in str(exc)
    else:  # pragma: no cover - the call must fail
        raise AssertionError("get_import accepted an unknown id")


async def test_authors_and_venues_are_writable(client: httpx.AsyncClient, mcp_api: None) -> None:
    author = _load(
        await mcp_server.create_author(f"Carol {uuid.uuid4().hex[:6]}", orcid="0000-0002-1825-0097")
    )
    assert author["orcid"] == "0000-0002-1825-0097"
    updated = _load(await mcp_server.update_author(author["id"], homepage_url="https://e.g"))
    assert updated["homepageUrl"] == "https://e.g"
    listed = _load(await mcp_server.list_authors(q=author["displayName"]))
    assert any(item["id"] == author["id"] for item in listed["items"])

    venue = _load(
        await mcp_server.create_venue(
            kind="conference", full_name=f"Test Conf {uuid.uuid4().hex[:6]}", abbrev="TC"
        )
    )
    assert _load(await mcp_server.get_venue(venue["id"]))["id"] == venue["id"]
    assert isinstance(_load(await mcp_server.get_venue_ranks(venue["id"])), list)
    renamed = _load(await mcp_server.update_venue(venue["id"], abbrev="TC2"))
    assert renamed["abbrev"] == "TC2"


async def test_local_pdf_upload_and_import(
    client: httpx.AsyncClient, mcp_api: None, drain_queue: None, tmp_path
) -> None:
    """The drag-and-drop path: a PDF that only exists on the user's disk."""
    pdf_path = tmp_path / "downloaded.pdf"
    pdf_path.write_bytes(_pdf("Text that only exists in a local file."))

    paper = await _paper(client)
    uploaded = _load(await mcp_server.upload_pdf(paper["id"], str(pdf_path)))
    assert uploaded["file"]["isPrimary"] is True
    assert uploaded["file"]["textStatus"] == "pending"

    # The same bytes arriving through the import path must link, not duplicate.
    imported = _load(await mcp_server.import_pdf_file(str(pdf_path)))
    assert imported["paperId"] == paper["id"]
    assert imported["created"] is False


async def test_local_pdf_validation_rejects_bad_input(
    client: httpx.AsyncClient, mcp_api: None, tmp_path
) -> None:
    with pytest.raises(ValueError, match="absolute"):
        await mcp_server.upload_pdf("whatever", "relative/paper.pdf")

    with pytest.raises(ValueError, match="not a file"):
        await mcp_server.upload_pdf("whatever", str(tmp_path / "missing.pdf"))

    text_file = tmp_path / "notes.txt"
    text_file.write_text("not a pdf")
    with pytest.raises(ValueError, match="not a PDF"):
        await mcp_server.upload_pdf("whatever", str(text_file))


async def test_mcp_page_image_returns_png(
    client: httpx.AsyncClient, mcp_api: None, drain_queue: None
) -> None:
    """The visual half of reading: figures come back as an image, not text."""
    import base64

    paper = await _paper(client)
    upload = await client.post(
        f"/v1/papers/{paper['id']}/files",
        headers=SERVICE,
        files={"file": ("fig.pdf", _pdf("Figure 1: a chart."), "application/pdf")},
    )
    file_id = upload.json()["file"]["id"]
    from paperdb.jobs.worker import run_once

    while await run_once():
        pass

    image = await mcp_server.get_page_image(file_id, 1)
    content = image.to_image_content()
    assert content.mimeType == "image/png"
    assert base64.b64decode(content.data).startswith(b"\x89PNG")


async def test_whoami_reports_token_scopes(client: httpx.AsyncClient, mcp_api: None) -> None:
    me = _load(await mcp_server.whoami())
    assert me["kind"] in {"service", "personal"}
    assert isinstance(me["scopes"], list) and me["scopes"]


async def test_mcp_library_stats_and_outline(
    client: httpx.AsyncClient, mcp_api: None, drain_queue: None
) -> None:
    stats = _load(await mcp_server.library_stats())
    assert stats["papers"] >= 1
    assert "byStatus" in stats and "withPdf" in stats

    paper = await _paper(client)
    document = pymupdf.open()
    for index in range(2):
        page = document.new_page()
        page.insert_text((72, 72), f"page {index + 1}", fontsize=11)
    document.set_toc([[1, "Intro", 1], [1, "Method", 2]])
    data = document.tobytes()
    document.close()
    upload = await client.post(
        f"/v1/papers/{paper['id']}/files",
        headers=SERVICE,
        files={"file": ("toc.pdf", data, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    from paperdb.jobs.worker import run_once

    while await run_once():
        pass

    outline = _load(await mcp_server.get_paper_outline(paper["id"]))
    assert [section["title"] for section in outline["sections"]] == ["Intro", "Method"]

    # A PDF without bookmarks gets an actionable hint, not an error.
    plain = await _paper(client)
    await client.post(
        f"/v1/papers/{plain['id']}/files",
        headers=SERVICE,
        files={"file": ("plain.pdf", _pdf("no toc"), "application/pdf")},
    )
    empty = _load(await mcp_server.get_paper_outline(plain["id"]))
    assert empty["sections"] == []
    assert "search_text" in empty["hint"]


async def test_mcp_totp_gate_with_personal_token(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The code really travels from the tool to the API's TOTP gate."""
    from paperdb.auth.totp import current_totp, reset_failures
    from paperdb.main import app

    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    reset_failures()
    created = await client.post(
        "/v1/tokens",
        headers=SERVICE,
        json={"name": f"mcp-totp-{uuid.uuid4().hex[:6]}", "scopes": ["organize:write"]},
    )
    assert created.status_code == 201, created.text
    personal = created.json()["token"]

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {personal}"},
            timeout=30.0,
        )

    monkeypatch.setattr(mcp_server, "_client", factory)
    tag = (
        await client.post(
            "/v1/tags", headers=SERVICE, json={"path": f"totp/{uuid.uuid4().hex[:6]}"}
        )
    ).json()

    with pytest.raises(RuntimeError, match="totp_required"):
        await mcp_server.delete_tag(tag["id"], totp_code="")

    with pytest.raises(RuntimeError, match="totp_invalid"):
        await mcp_server.delete_tag(tag["id"], totp_code="000000")

    await mcp_server.delete_tag(tag["id"], totp_code=current_totp(secret))
    reset_failures()
