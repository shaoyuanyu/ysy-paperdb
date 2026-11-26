from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from paperdb.config import get_settings
from paperdb.db.session import get_sessionmaker
from paperdb.ingest.resolve import resolve_input
from paperdb.ingest.types import AuthorRecord, Candidate, VenueSuggestion

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}


def _doi() -> str:
    return f"10.9999/{uuid.uuid4().hex}"


def _crossref(doi: str, title: str) -> dict[str, Any]:
    return {
        "message": {
            "DOI": doi,
            "title": [title],
            "abstract": "A useful abstract.",
            "issued": {"date-parts": [[2026, 6, 1]]},
            "type": "proceedings-article",
            "container-title": ["AAAI Conference on Artificial Intelligence"],
            "volume": "38",
            "page": "1-10",
            "author": [
                {
                    "given": "Shaoyuan",
                    "family": "Yu",
                    "ORCID": "https://orcid.org/0000-0002-1234-5678",
                },
                {"given": "Jane", "family": "Doe"},
            ],
            "URL": f"https://doi.org/{doi}",
        }
    }


def _dblp(title: str) -> dict[str, Any]:
    return {
        "result": {
            "hits": {
                "hit": [
                    {
                        "info": {
                            "title": title,
                            "venue": "AAAI",
                            "year": "2026",
                            "key": "conf/aaai/Yu26",
                            "url": "https://dblp.org/rec/conf/aaai/Yu26",
                            "authors": {
                                "author": [
                                    {"text": "Shaoyuan Yu", "@pid": "Y/Shaoyuan_Yu"},
                                    {"text": "Jane Doe", "@pid": "D/Jane_Doe"},
                                ]
                            },
                        }
                    }
                ]
            }
        }
    }


def _mock_transport(doi: str, title: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "crossref.org" in url:
            return httpx.Response(200, json=_crossref(doi, title))
        if "dblp.org/search" in url:
            return httpx.Response(200, json=_dblp(title))
        if "openalex.org" in url:
            return httpx.Response(200, json={"results": []})
        if "semanticscholar.org" in url:
            return httpx.Response(404)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_resolve_doi_with_venue_and_ccf() -> None:
    doi = _doi()
    transport = _mock_transport(doi, "Resolved Paper About Retrieval")
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://mock") as client,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(
            session, f"https://doi.org/{doi}", get_settings(), client=client
        )
    assert candidate is not None, warnings
    assert candidate.title == "Resolved Paper About Retrieval"
    assert candidate.kind == "conference"
    assert candidate.year == 2026
    assert candidate.venue is not None
    assert candidate.venue.venue_id is not None
    assert candidate.evaluation["ccf"]["level"] == "A"
    assert candidate.evaluation["cas"] is None
    assert candidate.identifiers[0]["scheme"] == "doi"
    assert any(author.dblp_pid for author in candidate.authors)
    assert candidate.duplicate_of is None


async def test_resolve_arxiv_preprint() -> None:
    arxiv_xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2601.01234v1</id>
    <published>2026-01-05T00:00:00Z</published>
    <title>An arXiv Test Paper</title>
    <summary>Preprint abstract.</summary>
    <author><name>Shaoyuan Yu</name></author>
    <author><name>Jane Doe</name></author>
    <arxiv:doi>10.9999/arxiv-linked</arxiv:doi>
  </entry>
</feed>"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "export.arxiv.org" in url:
            return httpx.Response(200, text=arxiv_xml)
        if "dblp.org/search" in url:
            return httpx.Response(200, json={"result": {"hits": {"hit": []}}})
        if "semanticscholar.org" in url:
            return httpx.Response(404)
        return httpx.Response(404)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(
            session, "arXiv:2601.01234v1", get_settings(), client=client
        )
    assert candidate is not None, warnings
    assert candidate.kind == "preprint"
    assert candidate.title == "An arXiv Test Paper"
    assert candidate.venue is None
    assert candidate.identifiers[0]["scheme"] in {"doi", "arxiv"}


async def test_resolve_duplicate(client: httpx.AsyncClient) -> None:
    venue = (
        await client.get("/v1/venues", params={"q": "AAAI", "limit": 1}, headers=SERVICE)
    ).json()["items"][0]
    doi = _doi()
    created = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "conference",
            "title": "Already Stored Paper",
            "year": 2026,
            "venueId": venue["id"],
            "identifiers": [{"scheme": "doi", "value": doi}],
        },
    )
    assert created.status_code == 201, created.text

    transport = _mock_transport(doi, "Already Stored Paper")
    async with httpx.AsyncClient(transport=transport) as http, get_sessionmaker()() as session:
        candidate, _ = await resolve_input(session, doi, get_settings(), client=http)
    assert candidate is not None
    assert candidate.duplicate_of is not None
    assert candidate.duplicate_of["paperId"] == created.json()["id"]


async def test_import_job_worker_creates_paper(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    venue = (
        await client.get("/v1/venues", params={"q": "AAAI", "limit": 1}, headers=SERVICE)
    ).json()["items"][0]
    doi = _doi()
    title = f"Imported Paper {uuid.uuid4().hex[:8]}"

    async def fake_resolve(session, raw, settings, *, client=None):
        return (
            Candidate(
                confidence=0.9,
                sources=["crossref"],
                kind="conference",
                title=title,
                abstract="Imported abstract.",
                year=2026,
                publication_date=None,
                authors=[
                    AuthorRecord(
                        name="Shaoyuan Yu",
                        family="Yu",
                        given="Shaoyuan",
                        dblp_pid="Y/Shaoyuan_Yu",
                    )
                ],
                identifiers=[{"scheme": "doi", "value": doi, "isPrimary": True}],
                venue=VenueSuggestion(
                    venue_id=venue["id"],
                    full_name=venue["fullName"],
                    abbrev=venue["abbrev"],
                    kind="conference",
                    year=2026,
                    confidence=0.95,
                    evidence="dblp",
                ),
                evaluation={"ccf": None, "cas": None},
                _venue_name=venue["fullName"],
                _dblp_venue_key="conf/aaai",
            ),
            [],
        )

    monkeypatch.setattr("paperdb.imports.service.resolve_input", fake_resolve)

    response = await client.post(
        "/v1/imports/identifiers",
        headers=SERVICE,
        json={"items": [doi]},
    )
    assert response.status_code == 202, response.text
    import_id = response.json()["importId"]
    job_id = response.json()["jobId"]

    from paperdb.jobs.worker import run_once

    assert await run_once() is True

    import_job = await client.get(f"/v1/imports/{import_id}", headers=SERVICE)
    assert import_job.status_code == 200
    body = import_job.json()
    assert body["status"] == "succeeded"
    assert body["stats"]["created"] == 1
    items = await client.get(f"/v1/imports/{import_id}/items", headers=SERVICE)
    assert items.json()[0]["status"] == "created"
    job = await client.get(f"/v1/jobs/{job_id}", headers=SERVICE)
    assert job.json()["status"] == "succeeded"


async def test_resolve_acl_bibtex() -> None:
    bib = """@inproceedings{acl2024test,
  title = {An ACL Test Paper},
  author = {Alice Smith and Bob Jones},
  booktitle = {Proceedings of ACL 2024},
  year = {2024},
  pages = {1--10},
  url = {https://aclanthology.org/2024.acl-long.1/}
}"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "aclanthology.org" in str(request.url):
            return httpx.Response(200, text=bib)
        if "dblp.org" in str(request.url):
            return httpx.Response(200, json={"result": {"hits": {"hit": []}}})
        return httpx.Response(404)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(
            session,
            "https://aclanthology.org/2024.acl-long.1/",
            get_settings(),
            client=client,
        )
    assert candidate is not None, warnings
    assert candidate.title == "An ACL Test Paper"
    assert candidate.kind == "conference"
    assert candidate.year == 2024
    assert any(item["scheme"] == "acl" for item in candidate.identifiers)


async def test_resolve_openreview() -> None:
    payload = {
        "notes": [
            {
                "forum": "abc123",
                "content": {
                    "title": {"value": "An OpenReview Paper"},
                    "authors": {"value": ["Alice Smith", "Bob Jones"]},
                    "abstract": {"value": "OpenReview abstract."},
                    "venue": {"value": "ICLR 2024"},
                    "doi": {"value": "10.9999/openreview"},
                },
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "api2.openreview.net" in str(request.url):
            return httpx.Response(200, json=payload)
        if "dblp.org" in str(request.url):
            return httpx.Response(200, json={"result": {"hits": {"hit": []}}})
        return httpx.Response(404)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(
            session,
            "https://openreview.net/forum?id=abc123",
            get_settings(),
            client=client,
        )
    assert candidate is not None, warnings
    assert candidate.title == "An OpenReview Paper"
    assert candidate.year == 2024
    assert candidate.venue is not None
    assert candidate.identifiers


async def test_resolve_dblp_record() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<dblp>
  <record>
    <info>
      <title>A DBLP Test Paper.</title>
      <authors>
        <author pid="Y/Shaoyuan_Yu">Shaoyuan Yu</author>
        <author pid="D/Jane_Doe">Jane Doe</author>
      </authors>
      <venue>AAAI</venue>
      <year>2026</year>
      <ee>https://doi.org/10.9999/dblp-test</ee>
    </info>
  </record>
</dblp>
"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "dblp.org/rec/" in str(request.url):
            return httpx.Response(200, text=xml)
        if "dblp.org/search" in str(request.url):
            return httpx.Response(200, json={"result": {"hits": {"hit": []}}})
        return httpx.Response(404)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(
            session,
            "https://dblp.org/rec/conf/aaai/Yu26.html",
            get_settings(),
            client=client,
        )
    assert candidate is not None, warnings
    assert candidate.title == "A DBLP Test Paper"
    assert candidate.kind == "conference"
    assert candidate.venue is not None
    assert candidate.venue.venue_id is not None
    assert candidate.evaluation["ccf"]["level"] == "A"


async def test_suggest_acl_proceedings_venue() -> None:
    from paperdb.ingest.venue_resolve import suggest_venue

    async with get_sessionmaker()() as session:
        suggestion = await suggest_venue(
            session,
            raw_name=(
                "Proceedings of the 62nd Annual Meeting of the Association for "
                "Computational Linguistics (Volume 1: Long Papers)"
            ),
            kind="conference",
            year=2024,
        )
    assert suggestion is not None
    assert suggestion.venue_id is not None
    assert suggestion.abbrev == "ACL"
    assert suggestion.evidence == "proceedings"


async def test_suggest_icml_proceedings_venue() -> None:
    from paperdb.ingest.venue_resolve import suggest_venue

    async with get_sessionmaker()() as session:
        suggestion = await suggest_venue(
            session,
            raw_name="Proceedings of the 41st International Conference on Machine Learning",
            kind="conference",
            year=2024,
        )
    assert suggestion is not None
    assert suggestion.venue_id is not None
    assert suggestion.abbrev == "ICML"
    assert suggestion.evidence == "proceedings"


_NDSS_TITLE = (
    "Bleeding Pathways: Vanishing Discriminability in LLM Hidden States Fuels Jailbreak Attacks"
)
_NDSS_PAGE = f"""<!DOCTYPE html>
<html><head>
<meta property="og:title" content="{_NDSS_TITLE} - NDSS Symposium" />
<meta property="og:site_name" content="NDSS Symposium" />
<title>{_NDSS_TITLE} - NDSS Symposium</title>
</head><body>
<h1>{_NDSS_TITLE}</h1>
<a href="https://www.ndss-symposium.org/wp-content/uploads/2026-f4-paper.pdf">Paper</a>
</body></html>"""

_NDSS_OPENALEX = {
    "results": [
        {
            "title": _NDSS_TITLE,
            "publication_year": 2026,
            "type": "proceedings-article",
            "doi": "https://doi.org/10.14722/ndss.2026.240004",
            "ids": {"openalex": "W1"},
            "id": "https://openalex.org/W1",
            "primary_location": {
                "source": {"display_name": "Network and Distributed System Security Symposium"}
            },
            "authorships": [{"author": {"display_name": "Yingjie Zhang"}}],
        }
    ]
}


def _landing_handler(
    page_html: str, *, openalex: dict[str, Any] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ndss-symposium.org" in url and url.endswith(".pdf"):
            return httpx.Response(200, content=b"fake")
        if "ndss-symposium.org" in url:
            return httpx.Response(200, text=page_html)
        if "dblp.org/search" in url:
            return httpx.Response(200, json={"result": {"hits": {"hit": []}}})
        if "openalex.org" in url:
            return httpx.Response(200, json=openalex or {"results": []})
        if "semanticscholar.org" in url:
            return httpx.Response(404)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_resolve_landing_page_url() -> None:
    transport = _landing_handler(_NDSS_PAGE, openalex=_NDSS_OPENALEX)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://mock") as client,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(
            session,
            "https://www.ndss-symposium.org/ndss-paper/bleeding-pathways-vanishing-discriminability/",
            get_settings(),
            client=client,
        )
    assert candidate is not None, warnings
    assert candidate.title.startswith("Bleeding Pathways")
    assert "page" in candidate.sources
    assert "openalex" in candidate.sources
    assert candidate.venue is not None
    assert candidate.venue.abbrev == "NDSS"
    assert candidate.venue.venue_id is not None
    assert candidate.evaluation["ccf"] is not None
    assert candidate.pdf_urls == [
        "https://www.ndss-symposium.org/wp-content/uploads/2026-f4-paper.pdf"
    ]
    assert any(item["scheme"] == "doi" for item in candidate.identifiers)


async def test_resolve_landing_page_with_citation_doi() -> None:
    page = """<!DOCTYPE html>
<html><head>
<meta name="citation_title" content="A Publisher Paper About Retrieval" />
<meta name="citation_doi" content="10.9999/landing-doi" />
<meta name="citation_pdf_url" content="https://publisher.org/pdf/landing-doi.pdf" />
</head><body></body></html>"""
    doi = "10.9999/landing-doi"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "publisher.org" in url:
            return httpx.Response(200, text=page)
        if "crossref.org" in url:
            return httpx.Response(200, json=_crossref(doi, "A Publisher Paper About Retrieval"))
        if "dblp.org/search" in url:
            return httpx.Response(200, json={"result": {"hits": {"hit": []}}})
        if "openalex.org" in url:
            return httpx.Response(200, json={"results": []})
        if "semanticscholar.org" in url:
            return httpx.Response(404)
        return httpx.Response(404)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(
            session, "https://publisher.org/content/paper-123", get_settings(), client=client
        )
    assert candidate is not None, warnings
    assert "crossref" in candidate.sources
    assert "page" in candidate.sources
    assert candidate.identifiers[0]["scheme"] == "doi"
    assert candidate.pdf_urls == ["https://publisher.org/pdf/landing-doi.pdf"]


async def test_resolve_landing_page_unindexed_fallback() -> None:
    """Fresh accepted papers missing from every aggregator are still built
    from the landing page itself (title/venue-hint/pdf link)."""
    transport = _landing_handler(_NDSS_PAGE)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://mock") as client,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(
            session,
            "https://www.ndss-symposium.org/ndss-paper/bleeding-pathways-vanishing-discriminability/",
            get_settings(),
            client=client,
        )
    assert candidate is not None, warnings
    assert candidate.sources == ["page"]
    assert candidate.title.startswith("Bleeding Pathways")
    assert candidate.pdf_urls
    # Venue hint "NDSS Symposium" matches NDSS through the acronym branch
    # despite the page-only kind defaulting to journal.
    assert candidate.venue is not None
    assert candidate.venue.abbrev == "NDSS"
    assert candidate.venue.evidence == "acronym"


async def test_resolve_unreachable_landing_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(
            session, "https://unreachable.example.org/paper/1", get_settings(), client=client
        )
    assert candidate is None
    assert any("unreachable" in warning for warning in warnings)


async def test_title_search_rejects_unrelated_results() -> None:
    """Search engines answer any text with *some* paper; the title branch
    must drop results whose title does not plausibly answer the query."""
    unrelated = {
        "results": [
            {
                "title": "The Batiandi Family: 八天地",
                "publication_year": 2020,
                "type": "article-journal",
                "ids": {},
                "id": "https://openalex.org/W9",
                "authorships": [{"author": {"display_name": "Someone"}}],
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "dblp.org/search" in url:
            return httpx.Response(200, json={"result": {"hits": {"hit": []}}})
        if "openalex.org" in url:
            return httpx.Response(200, json=unrelated)
        return httpx.Response(404)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(
            session, "今天天气不错适合出去玩", get_settings(), client=client
        )
    assert candidate is None
    assert "no metadata source" in warnings[0]


async def test_resolve_fuzzy_suggestion_for_zero_identifier_entry(
    client: httpx.AsyncClient,
) -> None:
    """A library entry without identifiers (PDF fallback / legacy import)
    is invisible to identifier dedup; resolve must surface a fuzzy
    suggestion instead of silently creating a second entry."""
    title = f"Zero Identifier Paper {uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "preprint",
            "title": title,
            "year": 2026,
            "authors": [{"displayName": "Alice Example"}],
            "identifiers": [],
        },
    )
    assert created.status_code == 201, created.text
    paper_id = created.json()["id"]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "dblp.org/search" in url:
            return httpx.Response(200, json={"result": {"hits": {"hit": []}}})
        if "openalex.org" in url:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": title,
                            "publication_year": 2026,
                            "type": "proceedings-article",
                            "doi": f"https://doi.org/10.9999/{uuid.uuid4().hex}",
                            "ids": {"openalex": "W77"},
                            "id": "https://openalex.org/W77",
                            "primary_location": {"source": {"display_name": "Test Venue"}},
                            "authorships": [{"author": {"display_name": "Alice Example"}}],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client_,
        get_sessionmaker()() as session,
    ):
        candidate, warnings = await resolve_input(session, title, get_settings(), client=client_)
    assert candidate is not None, warnings
    assert candidate.duplicate_of is None
    assert candidate.suggested_duplicate_of is not None
    assert candidate.suggested_duplicate_of["paperId"] == paper_id
    assert candidate.suggested_duplicate_of["strong"] is True
    assert candidate.suggested_duplicate_of["authorMatch"] is True
    assert any("possible duplicate" in warning for warning in warnings)


async def _fuzzy_import_case(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_duplicate: str | None,
) -> dict:
    """Shared driver: library paper without identifiers; an import resolves
    to the same title with fresh identifiers."""
    title = f"Fuzzy Link Paper {uuid.uuid4().hex[:8]}"
    doi = f"10.9999/{uuid.uuid4().hex}"
    arxiv = f"2601.{uuid.uuid4().int % 90000 + 10000:05d}"
    created = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "preprint",
            "title": title,
            "year": 2026,
            "authors": [{"displayName": "Alice Example"}],
        },
    )
    assert created.status_code == 201, created.text
    existing_id = created.json()["id"]

    async def fake_resolve(session, raw, settings, *, client=None):
        candidate = Candidate(
            confidence=0.8,
            sources=["openalex"],
            kind="preprint",
            title=title,
            abstract="An abstract.",
            year=2026,
            publication_date=None,
            authors=[AuthorRecord(name="Alice Example", family="Example", given="Alice")],
            identifiers=[
                {"scheme": "doi", "value": doi, "isPrimary": True},
                {"scheme": "arxiv", "value": arxiv, "isPrimary": False},
            ],
            venue=None,
            evaluation={"ccf": None, "cas": None},
            warnings=[],
        )
        candidate.suggested_duplicate_of = {
            "paperId": existing_id,
            "citekey": created.json()["citekey"],
            "title": title,
            "year": 2026,
            "yearDelta": 0,
            "authorMatch": True,
            "strong": True,
            "evidence": "exact-title+year(0)+first-author",
        }
        return candidate, []

    monkeypatch.setattr("paperdb.imports.service.resolve_input", fake_resolve)

    options: dict = {"defaultTags": ["status/to-read"]}
    if on_duplicate is not None:
        options["onDuplicate"] = on_duplicate
    response = await client.post(
        "/v1/imports/identifiers",
        headers=SERVICE,
        json={"items": [f"title:{title}"], "options": options},
    )
    assert response.status_code == 202, response.text
    import_id = response.json()["importId"]

    from paperdb.jobs.worker import run_once

    assert await run_once() is True
    job = await client.get(f"/v1/imports/{import_id}", headers=SERVICE)
    items = await client.get(f"/v1/imports/{import_id}/items", headers=SERVICE)
    return {
        "job": job.json(),
        "items": items.json(),
        "existingId": existing_id,
        "doi": doi,
    }


async def test_import_fuzzy_autolink_and_identifier_merge(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default onDuplicate=link: a strong fuzzy match links to the existing
    entry and merges the newly resolved identifiers into it."""
    result = await _fuzzy_import_case(client, monkeypatch, on_duplicate=None)
    item = result["items"][0]
    assert item["status"] == "duplicate", item
    assert item["paperId"] == result["existingId"]
    assert item["error"] and "identifier(s) merged" in item["error"]
    assert result["job"]["stats"]["duplicates"] == 1

    paper = (await client.get(f"/v1/papers/{result['existingId']}", headers=SERVICE)).json()
    schemes = {identifier["scheme"] for identifier in paper["identifiers"]}
    assert schemes == {"doi", "arxiv"}, paper["identifiers"]
    # default tags apply to linked entries too
    assert "status/to-read" in paper.get("tags", [])


async def test_import_on_duplicate_create(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """onDuplicate=create deliberately keeps a separate entry."""
    result = await _fuzzy_import_case(client, monkeypatch, on_duplicate="create")
    item = result["items"][0]
    assert item["status"] == "created", item
    assert item["paperId"] != result["existingId"]
    assert result["job"]["stats"]["created"] == 1


async def test_import_on_duplicate_skip(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """onDuplicate=skip: fuzzy matches stay suggestion-only (new entry);
    the linked entry's identifiers are not touched."""
    result = await _fuzzy_import_case(client, monkeypatch, on_duplicate="skip")
    item = result["items"][0]
    assert item["status"] == "created", item
    paper = (await client.get(f"/v1/papers/{result['existingId']}", headers=SERVICE)).json()
    assert paper["identifiers"] == []


async def test_import_identifier_duplicate_merges_new_identifiers(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-version import: library holds the DOI version, the arXiv
    version resolves with both identifiers → linked, arXiv id merged in."""
    title = f"Cross Version Paper {uuid.uuid4().hex[:8]}"
    doi = f"10.9999/{uuid.uuid4().hex}"
    created = await client.post(
        "/v1/papers",
        headers=SERVICE,
        json={
            "kind": "preprint",
            "title": title,
            "year": 2026,
            "identifiers": [{"scheme": "doi", "value": doi}],
        },
    )
    assert created.status_code == 201, created.text
    existing_id = created.json()["id"]

    async def fake_resolve(session, raw, settings, *, client=None):
        candidate = Candidate(
            confidence=0.8,
            sources=["s2"],
            kind="preprint",
            title=title,
            abstract=None,
            year=2026,
            publication_date=None,
            authors=[],
            identifiers=[
                {"scheme": "arxiv", "value": "2601.08888", "isPrimary": False},
                {"scheme": "doi", "value": doi, "isPrimary": True},
            ],
            venue=None,
            evaluation={"ccf": None, "cas": None},
            warnings=[],
        )
        candidate.duplicate_of = {"paperId": existing_id, "citekey": created.json()["citekey"]}
        return candidate, []

    monkeypatch.setattr("paperdb.imports.service.resolve_input", fake_resolve)
    response = await client.post(
        "/v1/imports/identifiers",
        headers=SERVICE,
        json={"items": ["arXiv:2601.08888"]},
    )
    assert response.status_code == 202, response.text
    import_id = response.json()["importId"]

    from paperdb.jobs.worker import run_once

    assert await run_once() is True
    items = (await client.get(f"/v1/imports/{import_id}/items", headers=SERVICE)).json()
    assert items[0]["status"] == "duplicate"
    assert items[0]["paperId"] == existing_id

    paper = (await client.get(f"/v1/papers/{existing_id}", headers=SERVICE)).json()
    values = {(identifier["scheme"], identifier["value"]) for identifier in paper["identifiers"]}
    assert ("arxiv", "2601.08888") in values
    assert ("doi", doi) in values


async def test_suggest_venue_matches_proceedings_title(db_session) -> None:
    """Crossref/publisher proceedings titles must resolve to the canonical venue."""
    from paperdb.ingest.venue_resolve import suggest_venue

    ndss = await suggest_venue(
        db_session,
        raw_name="Proceedings 2026 Network and Distributed System Security Symposium",
        kind="conference",
        year=2026,
    )
    assert ndss is not None
    assert ndss.abbrev == "NDSS"

    ccs = await suggest_venue(
        db_session,
        raw_name=(
            "Proceedings of the 2024 on ACM SIGSAC Conference on Computer and "
            "Communications Security"
        ),
        kind="conference",
        year=2024,
    )
    assert ccs is not None
    assert ccs.abbrev == "CCS"


def test_candidate_prefers_real_venue_over_preprint() -> None:
    """An arXiv-twin record must not hide the published venue."""
    from paperdb.ingest.resolve import _candidate
    from paperdb.ingest.types import SourceRecord

    records = [
        SourceRecord(source="openalex", title="A Paper", year=2025, venue="arXiv"),
        SourceRecord(source="page", title="A Paper", year=2025, venue="USENIX Security Symposium"),
    ]
    candidate = _candidate(records, "url", "https://www.usenix.org/conference/x")
    assert candidate is not None
    assert candidate._venue_name == "USENIX Security Symposium"


def test_candidate_derives_pdf_url_from_identifier() -> None:
    """arXiv/ACL inputs have no landing page; the OA PDF must still be found."""
    from paperdb.ingest.resolve import _candidate
    from paperdb.ingest.types import SourceRecord

    arxiv = _candidate(
        [SourceRecord(source="arxiv", title="A Paper", year=2026, arxiv="2601.14340")],
        "arxiv",
        "arXiv:2601.14340",
    )
    assert arxiv is not None
    assert arxiv.pdf_urls == ["https://arxiv.org/pdf/2601.14340"]

    acl = _candidate(
        [SourceRecord(source="acl", title="A Paper", year=2026, acl_id="2026.acl-long.1")],
        "acl",
        "https://aclanthology.org/2026.acl-long.1/",
    )
    assert acl is not None
    assert acl.pdf_urls == ["https://aclanthology.org/2026.acl-long.1.pdf"]
