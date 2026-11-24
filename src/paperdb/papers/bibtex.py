from __future__ import annotations

import re

from paperdb.db.models import IdentifierScheme, Paper, PaperKind


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\textbackslash{}")
        .replace("&", "\\&")
        .replace("%", "\\%")
        .replace("#", "\\#")
        .replace("_", "\\_")
    )


def _author_names(paper: Paper) -> str:
    names: list[str] = []
    for link in sorted(paper.authors, key=lambda item: item.position):
        author = link.author
        if author.family_name and author.given_name:
            names.append(f"{author.family_name}, {author.given_name}")
        elif author.family_name:
            names.append(author.family_name)
        else:
            names.append(author.display_name)
    return " and ".join(names)


def _venue_name(paper: Paper) -> str | None:
    if paper.venue is None:
        return None
    parts = [paper.venue.abbrev or paper.venue.full_name]
    if paper.venue_edition is not None:
        parts.append(str(paper.venue_edition.year))
        if paper.venue_edition.track.value != "main":
            parts.append(paper.venue_edition.track.value)
    return " ".join(parts)


def _entry_type(kind: PaperKind) -> str:
    return {
        PaperKind.conference: "inproceedings",
        PaperKind.journal: "article",
        PaperKind.preprint: "misc",
        PaperKind.thesis: "phdthesis",
        PaperKind.book_chapter: "incollection",
        PaperKind.tech_report: "techreport",
    }.get(kind, "misc")


def _identifier(paper: Paper, scheme: IdentifierScheme) -> str | None:
    for identifier in paper.identifiers:
        if identifier.scheme == scheme:
            return identifier.normalized
    return None


def generate_bibtex(paper: Paper, *, include_abstract: bool = False) -> str:
    fields: list[tuple[str, str]] = [("title", paper.title)]
    authors = _author_names(paper)
    if authors:
        fields.append(("author", authors))

    if paper.kind == PaperKind.conference:
        venue = _venue_name(paper)
        if venue:
            if paper.venue_edition is None:
                venue = f"{venue} {paper.year}"
            fields.append(("booktitle", venue))
    elif paper.kind == PaperKind.journal:
        if paper.venue:
            fields.append(("journal", paper.venue.full_name))
        if paper.volume:
            fields.append(("volume", paper.volume))
        if paper.issue:
            fields.append(("number", paper.issue))
    elif paper.kind == PaperKind.thesis and paper.institution:
        fields.append(("school", paper.institution))

    fields.append(("year", str(paper.year)))
    if paper.pages:
        fields.append(("pages", paper.pages.replace("-", "--")))

    doi = _identifier(paper, IdentifierScheme.doi)
    arxiv = _identifier(paper, IdentifierScheme.arxiv)
    if doi:
        fields.append(("doi", doi))
    if arxiv:
        fields.extend([("eprint", arxiv), ("archivePrefix", "arXiv")])
    if paper.urls.get("project"):
        fields.append(("url", str(paper.urls["project"])))
    elif doi:
        fields.append(("url", f"https://doi.org/{doi}"))
    if include_abstract and paper.abstract:
        fields.append(("abstract", paper.abstract))

    lines = [f"@{_entry_type(paper.kind)}{{{paper.citekey},"]
    for index, (name, value) in enumerate(fields):
        comma = "," if index < len(fields) - 1 else ""
        lines.append(f"  {name} = {{{_escape(value)}}}{comma}")
    lines.append("}")
    return "\n".join(lines)


def render_bibtex(papers: list[Paper], *, include_abstract: bool = False) -> str:
    entries = [
        generate_bibtex(paper, include_abstract=include_abstract)
        for paper in sorted(papers, key=lambda item: item.citekey)
    ]
    return "\n\n".join(entries) + ("\n" if entries else "")


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "paperdb"
