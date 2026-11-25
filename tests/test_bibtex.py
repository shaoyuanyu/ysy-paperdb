from __future__ import annotations

import uuid

import httpx

from paperdb.papers.bibtex_import import entry_to_candidate, parse_bibtex

SERVICE = {"Authorization": "Bearer pdb_svc_test0123456789abcdef0123456789abcdef"}

SAMPLE = """
% a comment
@inproceedings{vaswani2017attention,
  title = {Attention Is {All} You Need},
  author = {Ashish Vaswani and Noam Shazeer and Niki Parmar},
  booktitle = {NeurIPS},
  year = {2017},
  pages = {5998--6008},
  abstract = "A {quoted} abstract.",
}
"""


def test_parse_bibtex() -> None:
    entries = parse_bibtex(SAMPLE)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == "vaswani2017attention"
    assert entry.entry_type == "inproceedings"
    assert entry.value("title") == "Attention Is {All} You Need"
    candidate = entry_to_candidate(entry)
    assert candidate is not None
    assert candidate.title == "Attention Is All You Need"
    assert candidate.kind == "conference"
    assert candidate.year == 2017
    assert candidate.authors[0].family == "Vaswani"
    assert candidate.authors[0].given == "Ashish"
    assert candidate.citekey == "vaswani2017attention"
    assert candidate._venue_name == "NeurIPS"


async def test_bibtex_import_upload_and_worker(client: httpx.AsyncClient) -> None:
    unique = uuid.uuid4().hex[:8]
    bib = f"""
@inproceedings{{bibtest{unique},
  title = {{A BibTeX Imported Paper {unique}}},
  author = {{Shaoyuan Yu and Jane Doe}},
  booktitle = {{NeurIPS}},
  year = {{2026}},
  pages = {{1--12}},
}}
"""
    response = await client.post(
        "/v1/imports/bibtex",
        headers=SERVICE,
        files={"file": ("refs.bib", bib.encode(), "application/x-bibtex")},
    )
    assert response.status_code == 202, response.text
    import_id = response.json()["importId"]
    job_id = response.json()["jobId"]
    assert response.json()["items"] == 1

    from paperdb.jobs.worker import run_once

    assert await run_once() is True

    result = await client.get(f"/v1/imports/{import_id}", headers=SERVICE)
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "succeeded"
    assert body["stats"]["created"] == 1

    items = (await client.get(f"/v1/imports/{import_id}/items", headers=SERVICE)).json()
    assert items[0]["status"] == "created"
    paper_id = items[0]["paperId"]
    paper = (await client.get(f"/v1/papers/{paper_id}", headers=SERVICE)).json()
    assert paper["citekey"] == f"bibtest{unique}"
    assert paper["title"] == f"A BibTeX Imported Paper {unique}"
    assert paper["venue"]["fullName"] == "Conference on Neural Information Processing Systems"
    job = (await client.get(f"/v1/jobs/{job_id}", headers=SERVICE)).json()
    assert job["status"] == "succeeded"


async def test_bibtex_pasted_as_identifiers_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/imports/identifiers",
        headers=SERVICE,
        json={
            "items": [
                "@InProceedings{pmlr-v235-greenblatt24a,",
                "  title = {AI Control: Improving Safety Despite Intentional Subversion},",
                "}",
            ]
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "bibtex_in_identifiers"
