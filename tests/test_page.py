from __future__ import annotations

from paperdb.ingest.page import extract_page

NDSS_URL = "https://www.ndss-symposium.org/ndss-paper/bleeding-pathways-vanishing-discriminability/"


def _ndss_like_html() -> str:
    """Mirrors the real NDSS paper pages: og: tags only, no citation_* meta."""
    og_title = (
        "Bleeding Pathways: Vanishing Discriminability in LLM Hidden States"
        " Fuels Jailbreak Attacks - NDSS Symposium"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta property="og:locale" content="en_US" />
<meta property="og:type" content="article" />
<meta property="og:title" content="{og_title}" />
<meta property="og:url" content="https://www.ndss-symposium.org/ndss-paper/bleeding-pathways/" />
<meta property="og:site_name" content="NDSS Symposium" />
<title>{og_title}</title>
</head>
<body>
<h1>Bleeding Pathways: Vanishing Discriminability in LLM Hidden States Fuels Jailbreak Attacks</h1>
<p><strong>Yingjie Zhang</strong>, Tong Liu, Zhe Zhao, Guozhu Meng, Kai Chen</p>
<p>Large Language Models (LLMs) remain vulnerable to jailbreak attacks.</p>
<a href="https://www.ndss-symposium.org/wp-content/uploads/2026-f4-paper.pdf">Paper</a>
</body>
</html>"""


def _publisher_html() -> str:
    """A publisher page with the full HighWire citation_* tag set."""
    conference = (
        "Proceedings of the 2019 CHI Conference on Human Factors in Computing Systems"
    )
    return f"""<!DOCTYPE html>
<html>
<head>
<meta name="citation_title" content="A Publisher Paper About Retrieval" />
<meta name="citation_author" content="Yu, Shaoyuan" />
<meta name="citation_author" content="Doe, Jane" />
<meta name="citation_doi" content="10.1145/3290605.3300233" />
<meta name="citation_publication_date" content="2019/05/04" />
<meta name="citation_conference" content="{conference}" />
<meta name="citation_pdf_url" content="https://dl.acm.org/doi/pdf/10.1145/3290605.3300233" />
<meta property="og:site_name" content="ACM Digital Library" />
<title>Not the real title | ACM Digital Library</title>
</head>
<body><h1>Wrong heading</h1></body>
</html>"""


def test_extract_ndss_like_page() -> None:
    page = extract_page(_ndss_like_html(), NDSS_URL)
    assert page.title == (
        "Bleeding Pathways: Vanishing Discriminability in LLM Hidden States Fuels Jailbreak Attacks"
    )
    assert page.venue_hint == "NDSS Symposium"
    assert page.pdf_urls == [
        "https://www.ndss-symposium.org/wp-content/uploads/2026-f4-paper.pdf"
    ]
    assert page.doi is None
    assert page.authors == []


def test_extract_publisher_citation_meta() -> None:
    page = extract_page(_publisher_html(), "https://dl.acm.org/doi/10.1145/3290605.3300233")
    assert page.title == "A Publisher Paper About Retrieval"
    assert page.authors == ["Yu, Shaoyuan", "Doe, Jane"]
    assert page.doi == "10.1145/3290605.3300233"
    assert page.year == 2019
    assert page.venue_hint == (
        "Proceedings of the 2019 CHI Conference on Human Factors in Computing Systems"
    )
    assert page.pdf_urls == ["https://dl.acm.org/doi/pdf/10.1145/3290605.3300233"]


def test_title_suffix_stripping_without_site_name() -> None:
    html = """<html><head><title>A Great Paper Title Here - IEEE Xplore</title></head>
    <body></body></html>"""
    page = extract_page(html, "https://ieeexplore.ieee.org/document/123")
    assert page.title == "A Great Paper Title Here"


def test_doi_and_arxiv_harvested_from_links() -> None:
    html = """<html><head><title>Some Repository Page</title></head><body>
    <a href="https://arxiv.org/abs/2601.01234">arXiv version</a>
    </body></html>"""
    page = extract_page(html, "https://example.org/repository/item/9")
    assert page.arxiv == "2601.01234"


def test_pdf_links_relativized_and_deduped() -> None:
    html = """<html><head></head><body>
    <a href="/files/paper.pdf">pdf</a>
    <a href="/files/paper.pdf">same pdf</a>
    <a href="https://mirror.example.org/other.pdf">mirror</a>
    <a href="/about">about</a>
    </body></html>"""
    page = extract_page(html, "https://example.org/paper/1")
    assert page.pdf_urls == [
        "https://example.org/files/paper.pdf",
        "https://mirror.example.org/other.pdf",
    ]


def test_short_og_description_ignored_as_abstract() -> None:
    html = """<html><head>
    <meta property="og:description" content="Welcome to our site!" />
    <title>Some Paper</title>
    </head><body></body></html>"""
    page = extract_page(html, "https://example.org/p/1")
    assert page.abstract is None


def test_venue_from_url_usenix() -> None:
    from paperdb.ingest.page import venue_from_url

    assert (
        venue_from_url("https://www.usenix.org/conference/usenixsecurity25/presentation/he-jinwen")
        == "USENIX Security Symposium"
    )
    assert venue_from_url("https://example.org/paper") is None
